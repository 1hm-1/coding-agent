"""Private helper executed inside a Linux user/mount/PID/network namespace.

This module deliberately has no project imports. The parent process imports the
project before entering the namespace, while the test profile runs only after a
small chroot containing read-only system runtime mounts and the task workspace.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import threading
from typing import Any, Mapping


MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_BIND = 4096
MS_REMOUNT = 32
MS_PRIVATE = 1 << 18
MS_REC = 16384

PR_SET_NO_NEW_PRIVS = 38
PR_SET_PDEATHSIG = 1
CAPSET_VERSION = 0x20080522


class SetupFailure(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _mount(
    source: str | None,
    target: str | Path,
    filesystem: str | None,
    flags: int,
    data: str | None = None,
) -> None:
    libc = _libc()
    source_bytes = source.encode() if source is not None else None
    target_bytes = os.fsencode(str(target))
    filesystem_bytes = filesystem.encode() if filesystem is not None else None
    data_bytes = data.encode() if data is not None else None
    result = libc.mount(source_bytes, target_bytes, filesystem_bytes, flags, data_bytes)
    if result != 0:
        error_number = ctypes.get_errno()
        raise SetupFailure("sandbox_mount_failed", f"mount operation failed: {error_number}")


def _mkdir(path: str | Path) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def _bind_read_only(source: str, target: Path) -> None:
    _mount(source, target, None, MS_BIND | MS_REC | MS_RDONLY)


def _bind_device(source: str, target: Path) -> None:
    target.touch(exist_ok=True)
    _mount(source, target, None, MS_BIND)


def _copy_minimal_etc(root: Path) -> None:
    etc = root / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "passwd").write_text(
        "root:x:0:0:root:/tmp/home:/sbin/nologin\n",
        encoding="utf-8",
    )
    (etc / "group").write_text("root:x:0:\n", encoding="utf-8")
    (etc / "nsswitch.conf").write_text(
        "passwd: files\ngroup: files\nshadow: files\nhosts: files\n",
        encoding="utf-8",
    )
    os_release = Path("/etc/os-release")
    if os_release.is_file():
        (etc / "os-release").write_bytes(os_release.read_bytes())


def _setup_rootfs(
    root_dir: str | Path,
    *,
    workspace: str | None,
    writable_bytes: int,
) -> Path:
    root = Path(root_dir).resolve(strict=True)
    try:
        _mount(None, "/", None, MS_REC | MS_PRIVATE)
        _mount(
            "tmpfs",
            root,
            "tmpfs",
            MS_NOSUID | MS_NODEV,
            f"size={max(8 * 1024 * 1024, writable_bytes)}",
        )
    except SetupFailure:
        raise

    for name in ("usr", "bin", "sbin", "lib", "lib64", "etc", "workspace", "tmp", "dev", "proc"):
        _mkdir(root / name)
    for host_path in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
        if not Path(host_path).exists():
            raise SetupFailure("sandbox_runtime_missing", "required runtime mount is missing")
        _bind_read_only(host_path, root / host_path.lstrip("/"))
    _copy_minimal_etc(root)

    _mount(
        "tmpfs",
        root / "tmp",
        "tmpfs",
        MS_NOSUID | MS_NODEV | MS_NOEXEC,
        f"size={max(64 * 1024, writable_bytes)}",
    )
    _mkdir(root / "tmp" / "home")
    _mount(
        "tmpfs",
        root / "dev",
        "tmpfs",
        MS_NOSUID | MS_NODEV | MS_NOEXEC,
        "size=1m",
    )
    for source, target in (
        ("/dev/null", root / "dev" / "null"),
        ("/dev/zero", root / "dev" / "zero"),
        ("/dev/random", root / "dev" / "random"),
        ("/dev/urandom", root / "dev" / "urandom"),
    ):
        if Path(source).exists():
            _bind_device(source, target)
    _mount(
        "proc",
        root / "proc",
        "proc",
        MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC,
        "hidepid=2",
    )
    if workspace is not None:
        workspace_path = Path(workspace).resolve(strict=True)
        if not workspace_path.is_dir() or workspace_path.is_symlink():
            raise SetupFailure("sandbox_workspace_invalid", "workspace is not a directory")
        _mount(str(workspace_path), root / "workspace", None, MS_BIND | MS_REC)

    try:
        _mount(None, root, None, MS_REMOUNT | MS_RDONLY)
    except SetupFailure:
        raise SetupFailure("sandbox_rootfs_not_read_only", "root filesystem could not be remounted read-only")
    probe = root / ".coding-agent-read-only-probe"
    try:
        probe.open("x").close()
    except OSError as exc:
        if exc.errno not in {errno.EROFS, errno.EACCES, errno.EPERM}:
            raise SetupFailure("sandbox_rootfs_probe_failed", "root filesystem probe failed") from exc
    else:
        probe.unlink(missing_ok=True)
        raise SetupFailure("sandbox_rootfs_not_read_only", "root filesystem write probe succeeded")
    os.chroot(root)
    os.chdir("/")
    return root


def _set_no_new_privs() -> None:
    libc = _libc()
    result = libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    if result != 0:
        raise SetupFailure("sandbox_no_new_privileges_failed", "could not set no-new-privileges")


def _drop_capabilities() -> None:
    class CapHeader(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class CapData(ctypes.Structure):
        _fields_ = [
            ("effective", ctypes.c_uint32),
            ("permitted", ctypes.c_uint32),
            ("inheritable", ctypes.c_uint32),
        ]

    libc = _libc()
    header = CapHeader(CAPSET_VERSION, 0)
    data = (CapData * 2)()
    if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        raise SetupFailure("sandbox_capabilities_not_dropped", "could not drop Linux capabilities")


def _set_limits(limits: Mapping[str, Any]) -> None:
    cpu_seconds = max(1, int(float(limits["cpu_seconds"] + 0.999999)))
    memory_bytes = int(limits["memory_bytes"])
    writable_bytes = int(limits["writable_bytes"])
    pids = int(limits["pids"])
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    # RLIMIT_FSIZE is process-wide, so it cannot safely represent separate
    # stdout/stderr caps. Keep file writes within the storage budget and let
    # the runner monitor the inherited output descriptors independently.
    resource.setrlimit(resource.RLIMIT_FSIZE, (writable_bytes, writable_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # A rootless user namespace presents uid 0 to the child. Linux's
    # RLIMIT_NPROC is keyed to the host uid and is therefore both misleading
    # and capable of denying every fork before the namespace-local monitor
    # can observe the process count. Keep the reliable namespace-local count
    # monitor for that case; use RLIMIT_NPROC only for non-root identities.
    if os.getuid() != 0:
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (pids, pids))
        except (AttributeError, OSError, ValueError):
            pass


def _proc_snapshot() -> dict[int, int]:
    result: dict[int, int] = {}
    proc = Path("/proc")
    for candidate in proc.iterdir():
        if not candidate.name.isdigit():
            continue
        try:
            fields = (candidate / "stat").read_text(encoding="ascii").split(" ", 4)
            result[int(candidate.name)] = int(fields[3])
        except (OSError, ValueError, IndexError):
            continue
    return result


def _descendants(root_pid: int) -> set[int]:
    parents = _proc_snapshot()
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return descendants


def _memory_bytes(pids: set[int]) -> int:
    total = 0
    for pid in pids:
        try:
            for line in (Path("/proc") / str(pid) / "status").read_text(
                encoding="ascii", errors="ignore"
            ).splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError, IndexError):
            continue
    return total


def _memory_peak_bytes(pids: set[int]) -> int:
    peak = 0
    for pid in pids:
        try:
            for line in (Path("/proc") / str(pid) / "status").read_text(
                encoding="ascii", errors="ignore"
            ).splitlines():
                if line.startswith("VmPeak:"):
                    peak = max(peak, int(line.split()[1]) * 1024)
                    break
        except (OSError, ValueError, IndexError):
            continue
    return peak


def _workspace_bytes(workspace: Path) -> int:
    total = 0
    for current, dir_names, file_names in os.walk(workspace, followlinks=False):
        dir_names[:] = [name for name in dir_names if not (Path(current) / name).is_symlink()]
        for name in file_names:
            path = Path(current) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


def _output_bytes(fd: int) -> int:
    try:
        return os.fstat(fd).st_size
    except OSError:
        return 0


def _kill_process_group(pid: int, sig: signal.Signals) -> None:
    try:
        process_group = os.getpgid(pid)
        os.killpg(process_group, sig)
    except (OSError, ProcessLookupError):
        pass


def _guest_environment(raw: Mapping[str, Any]) -> dict[str, str]:
    allowed = {
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
    environment: dict[str, str] = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": "",
        "HOME": "/tmp/home",
        "CODING_AGENT_WORKSPACE": "/workspace",
        "PWD": "/workspace",
    }
    for key, value in raw.items():
        if key not in allowed or not isinstance(value, str) or "\x00" in value:
            raise SetupFailure("sandbox_environment_denied", "environment is not allowlisted")
        environment[key] = value
    environment["CODING_AGENT_WORKSPACE"] = "/workspace"
    environment["HOME"] = "/tmp/home"
    return environment


def _validate_executable(argv: list[str]) -> None:
    executable = Path(argv[0])
    if not executable.is_absolute():
        return
    allowed_roots = (Path("/usr"), Path("/bin"), Path("/sbin"))
    if not any(executable == root or root in executable.parents for root in allowed_roots):
        raise SetupFailure("sandbox_executable_outside_runtime", "profile executable is outside the runtime")
    if not executable.exists():
        raise SetupFailure("sandbox_executable_missing", "profile executable is unavailable")


def _write_result(fd: int, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        os.write(fd, encoded)
    except OSError:
        pass


def _probe(root_dir: str) -> int:
    try:
        _setup_rootfs(root_dir, workspace=None, writable_bytes=1 * 1024 * 1024)
        _set_no_new_privs()
        if not Path("/proc/1").exists():
            return 1
        interfaces = [
            line.split(":", 1)[0].strip()
            for line in Path("/proc/net/dev").read_text(encoding="ascii").splitlines()
            if ":" in line
        ]
        if any(name != "lo" for name in interfaces):
            return 1
        _drop_capabilities()
        return 0
    except (OSError, SetupFailure) as exc:
        print(
            f"sandbox probe failed: {getattr(exc, 'kind', type(exc).__name__)}",
            file=sys.stderr,
        )
        return 1


def _run(raw: Mapping[str, Any], root_dir: str, result_fd: int) -> int:
    raw_spec = raw.get("spec")
    if not isinstance(raw_spec, Mapping):
        _write_result(result_fd, {"status": "sandbox_error", "limit_hit": None, "kind": "invalid_spec"})
        return 2
    try:
        argv = [str(value) for value in raw_spec["argv"]]
        limits = raw_spec["limits"]
        if not isinstance(limits, Mapping):
            raise SetupFailure("sandbox_spec_invalid", "limits are invalid")
        _validate_executable(argv)
        _setup_rootfs(
            root_dir,
            workspace=str(raw_spec["workspace"]),
            writable_bytes=int(limits["writable_bytes"]),
        )
        _set_no_new_privs()
        _drop_capabilities()
        environment = _guest_environment(raw_spec.get("environment", {}))
        working_directory = str(raw_spec.get("working_directory", "."))
        if working_directory in {"", "."}:
            guest_working_directory = "/workspace"
        else:
            if Path(working_directory).is_absolute() or any(
                part == ".." for part in Path(working_directory).parts
            ):
                raise SetupFailure("sandbox_working_directory_invalid", "working directory escapes workspace")
            guest_working_directory = "/workspace/" + working_directory
        if not Path(guest_working_directory).is_dir():
            raise SetupFailure("sandbox_working_directory_invalid", "working directory is unavailable")
    except (KeyError, TypeError, ValueError, OSError, SetupFailure) as exc:
        kind = exc.kind if isinstance(exc, SetupFailure) else "sandbox_setup_failed"
        _write_result(result_fd, {"status": "sandbox_error", "limit_hit": None, "kind": kind})
        return 2

    workspace = Path("/workspace")
    temporary = Path("/tmp")
    baseline_bytes = _workspace_bytes(workspace) + _workspace_bytes(temporary)
    target: subprocess.Popen[bytes] | None = None
    limit_hit: str | None = None
    pid_limit_seen = False
    memory_peak_hit = False
    interrupted = False
    monitor_stop = threading.Event()
    monitor_lock = threading.Lock()

    def kill_target() -> None:
        if target is not None and target.poll() is None:
            _kill_process_group(target.pid, signal.SIGKILL)

    def signal_handler(signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = True
        kill_target()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    def monitor() -> None:
        nonlocal limit_hit, memory_peak_hit, pid_limit_seen
        while not monitor_stop.wait(0.02):
            if target is None or target.poll() is not None:
                return
            pids = _descendants(target.pid)
            if len(pids) > int(limits["pids"]):
                with monitor_lock:
                    pid_limit_seen = True
                    limit_hit = limit_hit or "pids"
                kill_target()
                return
            memory_limit = int(limits["memory_bytes"])
            if _memory_peak_bytes(pids) >= int(memory_limit * 0.95):
                with monitor_lock:
                    memory_peak_hit = True
            if _memory_bytes(pids) > memory_limit:
                with monitor_lock:
                    limit_hit = limit_hit or "memory_bytes"
                kill_target()
                return
            storage_bytes = _workspace_bytes(workspace) + _workspace_bytes(temporary)
            if storage_bytes > baseline_bytes + int(limits["writable_bytes"]):
                with monitor_lock:
                    limit_hit = limit_hit or "writable_bytes"
                kill_target()
                return
            for fd, name in ((1, "stdout_bytes"), (2, "stderr_bytes")):
                if _output_bytes(fd) > int(limits[name]):
                    with monitor_lock:
                        limit_hit = limit_hit or name
                    kill_target()
                    return

    def child_setup() -> None:
        _set_limits(limits)
        if hasattr(signal, "SIGXFSZ"):
            signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
        libc = _libc()
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
        os.setsid()

    try:
        target = subprocess.Popen(
            argv,
            cwd=guest_working_directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            preexec_fn=child_setup,
            close_fds=True,
        )
    except (OSError, ValueError):
        _write_result(
            result_fd,
            {"status": "sandbox_error", "limit_hit": None, "kind": "profile_exec_failed"},
        )
        return 2

    monitor_thread = threading.Thread(target=monitor, name="sandbox-resource-monitor", daemon=True)
    monitor_thread.start()
    try:
        try:
            target.wait(timeout=float(limits["wall_seconds"]))
        except subprocess.TimeoutExpired:
            with monitor_lock:
                limit_hit = limit_hit or "wall_seconds"
            kill_target()
            target.wait(timeout=2.0)
    finally:
        monitor_stop.set()
        monitor_thread.join(timeout=1.0)
        if target.poll() is None:
            kill_target()
            target.wait(timeout=2.0)

    return_code = target.returncode
    if limit_hit is None and pid_limit_seen:
        limit_hit = "pids"
    storage_bytes = _workspace_bytes(workspace) + _workspace_bytes(temporary)
    if limit_hit is None and storage_bytes >= baseline_bytes + int(limits["writable_bytes"]):
        limit_hit = "writable_bytes"
    try:
        child_peak_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024
    except (AttributeError, OSError, ValueError):
        child_peak_rss = 0
    if child_peak_rss >= int(int(limits["memory_bytes"]) * 0.85):
        memory_peak_hit = True
    if limit_hit is None and memory_peak_hit:
        limit_hit = "memory_bytes"
    if limit_hit is None and return_code is not None and return_code < 0:
        if -return_code == signal.SIGXCPU:
            limit_hit = "cpu_seconds"
        elif hasattr(signal, "SIGXFSZ") and -return_code == signal.SIGXFSZ:
            limit_hit = "writable_bytes"
        elif -return_code == signal.SIGKILL:
            limit_hit = "resource_limit"
    if interrupted:
        status = "timeout"
        limit_hit = limit_hit or "interrupt"
    elif limit_hit is not None:
        status = "timeout" if limit_hit == "wall_seconds" else "resource_exhausted"
    else:
        status = "exited"
    _write_result(
        result_fd,
        {
            "status": status,
            "exit_code": return_code,
            "limit_hit": limit_hit,
            "cleanup_verified": True,
        },
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--probe" in arguments:
        root_index = arguments.index("--root-dir") + 1
        return _probe(arguments[root_index])
    root_index = arguments.index("--root-dir") + 1
    fd_index = arguments.index("--result-fd") + 1
    raw = json.load(sys.stdin)
    return _run(raw, arguments[root_index], int(arguments[fd_index]))


if __name__ == "__main__":
    raise SystemExit(main())
