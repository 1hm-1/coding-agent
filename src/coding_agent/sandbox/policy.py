from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from coding_agent.sandbox.base import ExecutionSpec, SandboxCapabilities


class SandboxPolicyError(ValueError):
    """An execution request cannot be admitted to the OS isolation boundary."""

    def __init__(self, message: str, *, kind: str = "sandbox_policy_rejected"):
        super().__init__(message)
        self.kind = kind


_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "zsh", "fish", "csh", "tcsh"})
_SAFE_ENVIRONMENT = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
        "PYTHONHASHSEED",
        "PYTHONUNBUFFERED",
        "HOME",
        "CODING_AGENT_WORKSPACE",
        "PWD",
    }
)
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class SandboxPolicy:
    """Fail-closed policy shared by trusted profiles and executor backends."""

    allow_network: bool = False
    allowed_environment: frozenset[str] = _SAFE_ENVIRONMENT
    require_pinned_identity: bool = True

    REQUIRED_FEATURES = frozenset(
        {
            "mount_namespace",
            "pid_namespace",
            "network_namespace",
            "chroot_rootfs",
            "read_only_rootfs",
            "workspace_bind",
            "network_disabled",
            "env_allowlist",
            "process_cleanup",
            "resource_limits",
            "output_limits",
        }
    )

    def validate(self, spec: ExecutionSpec, capabilities: SandboxCapabilities) -> None:
        if not capabilities.available:
            raise SandboxPolicyError(
                "required sandbox capabilities are unavailable",
                kind="sandbox_capability_unavailable",
            )
        missing = self.REQUIRED_FEATURES - set(capabilities.features)
        if missing:
            raise SandboxPolicyError(
                "required sandbox capabilities are missing",
                kind="sandbox_capability_missing",
            )
        if spec.network != "none" and not self.allow_network:
            raise SandboxPolicyError(
                "network access is disabled by the M4.1 policy",
                kind="sandbox_network_denied",
            )
        if self.require_pinned_identity:
            if spec.image_digest is None or not _DIGEST_PATTERN.fullmatch(spec.image_digest):
                raise SandboxPolicyError(
                    "sandbox base identity must be digest pinned",
                    kind="sandbox_image_not_pinned",
                )
            expected = capabilities.metadata.get("image_digest")
            if expected is not None and spec.image_digest != expected:
                raise SandboxPolicyError(
                    "sandbox base identity does not match the probed backend",
                    kind="sandbox_image_mismatch",
                )
        workspace = Path(spec.workspace)
        if workspace.is_symlink():
            raise SandboxPolicyError(
                "sandbox workspace cannot be a symlink",
                kind="sandbox_workspace_symlink",
            )
        try:
            workspace = workspace.resolve(strict=True)
        except OSError as exc:
            raise SandboxPolicyError(
                "sandbox workspace is not available",
                kind="sandbox_workspace_unavailable",
            ) from exc
        if not workspace.is_dir() or workspace == Path(workspace.anchor):
            raise SandboxPolicyError(
                "sandbox workspace must be a non-root directory",
                kind="sandbox_workspace_invalid",
            )
        relative_workdir = Path(spec.working_directory)
        if relative_workdir.is_absolute() or any(
            part == ".." for part in relative_workdir.parts
        ):
            raise SandboxPolicyError(
                "sandbox working directory must stay inside workspace",
                kind="sandbox_working_directory_invalid",
            )
        try:
            resolved_workdir = (workspace / relative_workdir).resolve(strict=True)
            resolved_workdir.relative_to(workspace)
        except (OSError, ValueError) as exc:
            raise SandboxPolicyError(
                "sandbox working directory escapes workspace",
                kind="sandbox_working_directory_invalid",
            ) from exc
        if not resolved_workdir.is_dir():
            raise SandboxPolicyError(
                "sandbox working directory must be a directory",
                kind="sandbox_working_directory_invalid",
            )
        if any("\x00" in value for value in spec.argv):
            raise SandboxPolicyError(
                "sandbox argv contains NUL bytes",
                kind="sandbox_argv_invalid",
            )
        executable = Path(spec.argv[0]).name.lower()
        if executable in _SHELL_EXECUTABLES:
            raise SandboxPolicyError(
                "shell interpreters are not allowed by the sandbox profile boundary",
                kind="sandbox_shell_forbidden",
            )
        environment_keys = set(spec.environment)
        if not environment_keys <= self.allowed_environment:
            raise SandboxPolicyError(
                "sandbox environment contains a non-allowlisted key",
                kind="sandbox_environment_denied",
            )
        for key, value in spec.environment.items():
            if any(marker in key.upper() for marker in ("SECRET", "TOKEN", "PASSWORD", "API_KEY")):
                raise SandboxPolicyError(
                    "sandbox environment cannot carry credential-like keys",
                    kind="sandbox_secret_environment_denied",
                )
            if "\x00" in value:
                raise SandboxPolicyError(
                    "sandbox environment contains NUL bytes",
                    kind="sandbox_environment_invalid",
                )
        if spec.environment.get("CODING_AGENT_WORKSPACE") not in {None, "/workspace"}:
            raise SandboxPolicyError(
                "sandbox workspace environment must use the guest path",
                kind="sandbox_environment_invalid",
            )
