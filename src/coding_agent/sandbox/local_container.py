from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping

from coding_agent.domain import redact_sensitive_text
from coding_agent.sandbox.base import (
    ExecutionResult,
    ExecutionSpec,
    SandboxCapabilities,
)
from coding_agent.sandbox.policy import SandboxPolicy, SandboxPolicyError


_FEATURES = tuple(sorted(SandboxPolicy.REQUIRED_FEATURES))
_SAFE_PARENT_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONPATH": "",
}


def _default_runtime_python() -> str:
    """Return an interpreter that is part of the mounted base runtime."""

    for candidate in ("/usr/bin/python3", "/bin/python3"):
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return candidate
    return sys.executable


def _runtime_sample_fingerprint() -> str:
    """Fingerprint a declared host-runtime sample, not the complete mounted rootfs."""

    digest = hashlib.sha256()
    digest.update(platform.system().encode("utf-8"))
    digest.update(platform.release().encode("utf-8"))
    for path in ("/etc/os-release", "/usr/bin/python3", "/bin/sh"):
        digest.update(path.encode("utf-8"))
        try:
            digest.update(Path(path).read_bytes())
        except OSError:
            digest.update(b"missing")
    return f"sha256:{digest.hexdigest()}"


def _bounded_read(handle: Any, limit: int) -> tuple[str, bool]:
    handle.seek(0)
    value = handle.read(limit + 1)
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    return redact_sensitive_text(text[:limit]), len(text) > limit


def _process_group_exists(process_id: int) -> bool:
    try:
        os.killpg(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_process_group(process_id: int, sig: signal.Signals) -> None:
    try:
        os.killpg(process_id, sig)
    except (OSError, ProcessLookupError):
        pass


def _base_metadata(
    capabilities: SandboxCapabilities,
    spec: ExecutionSpec,
    *,
    cleanup_verified: bool,
    stdout_truncated: bool,
    stderr_truncated: bool,
) -> dict[str, Any]:
    return {
        "backend": capabilities.backend,
        "backend_version": capabilities.version,
        "execution_id": spec.execution_id,
        "profile_name": spec.profile_name,
        "image": spec.image,
        "image_digest": spec.image_digest,
        "network": spec.network,
        "workspace_mount": "/workspace:rw",
        "rootfs_read_only": True,
        "tmpfs_bytes": spec.limits.writable_bytes,
        "cleanup_verified": cleanup_verified,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "capability_snapshot": capabilities.to_dict(),
        "limits": spec.limits.to_dict(),
    }


class LinuxNamespaceExecutor:
    """Rootless Linux namespace executor with a private minimal rootfs.

    This backend does not fall back to host ``subprocess`` execution. It uses
    ``unshare`` only to create user/mount/PID/network namespaces, then the
    private runner constructs a read-only chroot and bind-mounts one workspace.
    """

    def __init__(
        self,
        *,
        policy: SandboxPolicy | None = None,
        unshare_path: str | None = None,
        python_executable: str | None = None,
        runner_path: str | Path | None = None,
        probe_timeout: float = 5.0,
    ):
        self.policy = policy or SandboxPolicy()
        self.unshare_path = unshare_path
        self.python_executable = python_executable or _default_runtime_python()
        self.runner_path = Path(runner_path or Path(__file__).with_name("runner.py")).resolve(
            strict=True
        )
        if probe_timeout <= 0:
            raise ValueError("sandbox capability probe timeout must be positive")
        self.probe_timeout = probe_timeout
        self._capability_snapshot: SandboxCapabilities | None = None

    def capabilities(self) -> SandboxCapabilities:
        if self._capability_snapshot is not None:
            return self._capability_snapshot
        backend = "linux_user_mount_pid_net"
        version = platform.release()
        if platform.system() != "Linux":
            return self._remember(
                SandboxCapabilities(
                    backend=backend,
                    version=version,
                    available=False,
                    missing=_FEATURES,
                    metadata={"reason": "linux_backend_required"},
                )
            )
        unshare = self.unshare_path or shutil.which("unshare")
        if not unshare or not os.access(unshare, os.X_OK) or not self.runner_path.is_file():
            return self._remember(
                SandboxCapabilities(
                    backend=backend,
                    version=version,
                    available=False,
                    missing=_FEATURES,
                    metadata={"reason": "unshare_or_runner_missing"},
                )
            )
        root_dir = Path(tempfile.mkdtemp(prefix="coding-agent-sandbox-probe-"))
        try:
            completed = subprocess.run(
                [
                    unshare,
                    "--user",
                    "--map-root-user",
                    "--mount",
                    "--pid",
                    "--fork",
                    "--mount-proc",
                    "--net",
                    "--ipc",
                    "--uts",
                    self.python_executable,
                    str(self.runner_path),
                    "--probe",
                    "--root-dir",
                    str(root_dir),
                ],
                env=dict(_SAFE_PARENT_ENV),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.probe_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        finally:
            shutil.rmtree(root_dir, ignore_errors=True)
        digest = _runtime_sample_fingerprint()
        if completed is not None and completed.returncode == 0:
            return self._remember(
                SandboxCapabilities(
                    backend=backend,
                    version=version,
                    available=True,
                    features=_FEATURES,
                    metadata={
                        "image_digest": digest,
                        "identity_kind": "native_runtime_sample_fingerprint",
                        "network_default": "none",
                    },
                )
            )
        return self._remember(
            SandboxCapabilities(
                backend=backend,
                version=version,
                available=False,
                missing=_FEATURES,
                metadata={
                    "reason": "capability_probe_failed",
                    "image_digest": digest,
                    "identity_kind": "native_runtime_sample_fingerprint",
                },
            )
        )

    def _remember(self, capabilities: SandboxCapabilities) -> SandboxCapabilities:
        self._capability_snapshot = capabilities
        return capabilities

    def execute(self, spec: ExecutionSpec) -> ExecutionResult:
        started = time.monotonic()
        capabilities = self.capabilities()
        try:
            self.policy.validate(spec, capabilities)
        except SandboxPolicyError as exc:
            return ExecutionResult(
                status="sandbox_error",
                exit_code=None,
                stdout="",
                stderr="",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                limit_hit=None,
                backend_metadata={
                    "backend": capabilities.backend,
                    "backend_version": capabilities.version,
                    "execution_id": spec.execution_id,
                    "error_kind": exc.kind,
                    "capability_snapshot": capabilities.to_dict(),
                    "cleanup_verified": True,
                },
            )
        unshare = self.unshare_path or shutil.which("unshare")
        if not unshare:
            return self._sandbox_error(
                capabilities,
                spec,
                started,
                "sandbox_capability_unavailable",
            )
        root_dir = Path(tempfile.mkdtemp(prefix="coding-agent-sandbox-root-"))
        status_read, status_write = os.pipe()
        process: subprocess.Popen[bytes] | None = None
        timed_out = False
        stdout_file = tempfile.TemporaryFile(mode="w+b")
        stderr_file = tempfile.TemporaryFile(mode="w+b")
        try:
            payload = json.dumps({"spec": spec.to_wire()}, ensure_ascii=False).encode("utf-8")
            process = subprocess.Popen(
                [
                    unshare,
                    "--user",
                    "--map-root-user",
                    "--mount",
                    "--pid",
                    "--fork",
                    "--mount-proc",
                    "--net",
                    "--ipc",
                    "--uts",
                    "--kill-child",
                    self.python_executable,
                    str(self.runner_path),
                    "--run",
                    "--root-dir",
                    str(root_dir),
                    "--result-fd",
                    str(status_write),
                ],
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                env=dict(_SAFE_PARENT_ENV),
                pass_fds=(status_write,),
                start_new_session=True,
                close_fds=True,
            )
            os.close(status_write)
            status_write = -1
            assert process.stdin is not None
            process.stdin.write(payload)
            process.stdin.close()
            try:
                process.wait(timeout=max(0.1, spec.limits.wall_seconds + 2.0))
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    _terminate_process_group(process.pid, signal.SIGKILL)
                    process.wait(timeout=2.0)
        except (OSError, ValueError):
            if process is not None and process.poll() is None:
                _terminate_process_group(process.pid, signal.SIGKILL)
                process.wait(timeout=2.0)
        finally:
            if status_write >= 0:
                os.close(status_write)
        try:
            status_payload = os.read(status_read, 64 * 1024)
        except OSError:
            status_payload = b""
        finally:
            os.close(status_read)
        stdout, stdout_truncated = _bounded_read(stdout_file, spec.limits.stdout_bytes)
        stderr, stderr_truncated = _bounded_read(stderr_file, spec.limits.stderr_bytes)
        stdout_file.close()
        stderr_file.close()
        cleanup_verified = process is not None and process.poll() is not None
        if process is not None:
            cleanup_verified = cleanup_verified and not _process_group_exists(process.pid)
        try:
            raw_status = json.loads(status_payload.decode("utf-8")) if status_payload else {}
        except (TypeError, ValueError):
            raw_status = {}
        if timed_out:
            status = "timeout"
            exit_code = process.returncode if process is not None else None
            limit_hit = "wall_seconds"
        elif not isinstance(raw_status, Mapping):
            status = "sandbox_error"
            exit_code = process.returncode if process is not None else None
            limit_hit = None
        else:
            status = str(raw_status.get("status", "sandbox_error"))
            if status not in {"exited", "timeout", "resource_exhausted", "sandbox_error"}:
                status = "sandbox_error"
            exit_code = raw_status.get("exit_code")
            if not isinstance(exit_code, int):
                exit_code = process.returncode if process is not None else None
            limit_hit = raw_status.get("limit_hit")
            if not isinstance(limit_hit, str):
                limit_hit = None
        if status == "exited" and (stdout_truncated or stderr_truncated):
            status = "resource_exhausted"
            limit_hit = "stdout_bytes" if stdout_truncated else "stderr_bytes"
        metadata = _base_metadata(
            capabilities,
            spec,
            cleanup_verified=cleanup_verified,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )
        if isinstance(raw_status, Mapping):
            metadata["runner_cleanup_verified"] = bool(raw_status.get("cleanup_verified", False))
        if process is None:
            status = "sandbox_error"
        return ExecutionResult(
            status=status,  # type: ignore[arg-type]
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            limit_hit=limit_hit,
            backend_metadata=metadata,
        )

    @staticmethod
    def _sandbox_error(
        capabilities: SandboxCapabilities,
        spec: ExecutionSpec,
        started: float,
        kind: str,
    ) -> ExecutionResult:
        return ExecutionResult(
            status="sandbox_error",
            exit_code=None,
            stdout="",
            stderr="",
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            backend_metadata={
                "backend": capabilities.backend,
                "backend_version": capabilities.version,
                "execution_id": spec.execution_id,
                "error_kind": kind,
                "capability_snapshot": capabilities.to_dict(),
                "cleanup_verified": True,
            },
        )


class FailClosedSandboxExecutor:
    """Explicit unavailable executor used when no safe backend is configured."""

    def __init__(self, reason: str = "sandbox_backend_unavailable"):
        self.reason = reason

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            backend="fail_closed",
            version="unavailable",
            available=False,
            missing=_FEATURES,
            metadata={"reason": self.reason},
        )

    def execute(self, spec: ExecutionSpec) -> ExecutionResult:
        capabilities = self.capabilities()
        return ExecutionResult(
            status="sandbox_error",
            exit_code=None,
            stdout="",
            stderr="",
            duration_ms=0.0,
            backend_metadata={
                "backend": capabilities.backend,
                "execution_id": spec.execution_id,
                "error_kind": "sandbox_capability_unavailable",
                "capability_snapshot": capabilities.to_dict(),
                "cleanup_verified": True,
            },
        )


@dataclass
class FakeSandboxExecutor:
    """Deterministic unit-test executor; it is never the application default."""

    result_factory: Callable[[ExecutionSpec], ExecutionResult] | None = None

    def __post_init__(self) -> None:
        self.calls: list[ExecutionSpec] = []
        self._capabilities = SandboxCapabilities(
            backend="fake_sandbox",
            version="test",
            available=True,
            features=_FEATURES,
            metadata={
                "image_digest": "sha256:" + "0" * 64,
                "identity_kind": "test_fixture",
                "network_default": "none",
            },
        )

    def capabilities(self) -> SandboxCapabilities:
        return self._capabilities

    def execute(self, spec: ExecutionSpec) -> ExecutionResult:
        self.calls.append(spec)
        if self.result_factory is not None:
            return self.result_factory(spec)
        return ExecutionResult(
            status="exited",
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=0.0,
            backend_metadata={
                "backend": "fake_sandbox",
                "execution_id": spec.execution_id,
                "image_digest": spec.image_digest,
                "cleanup_verified": True,
            },
        )


def build_default_sandbox_executor() -> LinuxNamespaceExecutor | FailClosedSandboxExecutor:
    """Choose the Linux backend; an unavailable backend remains fail closed."""

    if platform.system() != "Linux":
        return FailClosedSandboxExecutor("linux_backend_required")
    return LinuxNamespaceExecutor()
