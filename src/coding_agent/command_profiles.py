from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Mapping

from coding_agent.sandbox.base import ResourceLimits


DEFAULT_COMMAND_LIMITS = ResourceLimits(
    wall_seconds=30.0,
    cpu_seconds=20.0,
    memory_bytes=256 * 1024 * 1024,
    writable_bytes=64 * 1024 * 1024,
    pids=32,
    stdout_bytes=20_000,
    stderr_bytes=20_000,
)

_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "zsh", "fish", "csh", "tcsh"})


@dataclass(frozen=True)
class CommandProfile:
    """Trusted policy for one structured command execution.

    The model supplies argv, but this profile remains the authority for the
    executable allowlist, cwd defaults, environment, network mode, image and
    resource limits.  Profiles requesting network or elevated limits remain
    admission-denied until an explicit approval path exists.
    """

    name: str
    executable_allowlist: tuple[str, ...]
    timeout_seconds: float = 30.0
    image: str = "linux-namespace-rootfs"
    working_directory: str = "."
    network: str = "none"
    limits: ResourceLimits = field(default_factory=lambda: DEFAULT_COMMAND_LIMITS)
    environment: Mapping[str, str] = field(default_factory=dict)
    max_argv_items: int = 32
    max_argument_length: int = 4096
    max_working_directory_length: int = 256
    approval_required: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError(f"invalid command profile name: {self.name!r}")
        if not self.executable_allowlist:
            raise ValueError("command profile executable allowlist cannot be empty")
        if any(
            not isinstance(value, str) or not value or "\x00" in value
            for value in self.executable_allowlist
        ):
            raise ValueError("command profile executable allowlist contains an invalid value")
        if any(Path(value).name.lower() in _SHELL_EXECUTABLES for value in self.executable_allowlist):
            raise ValueError("shell interpreters cannot be command profile executables")
        if self.timeout_seconds <= 0:
            raise ValueError("command profile timeout must be positive")
        if not self.image:
            raise ValueError("command profile image identity is required")
        if (
            not self.working_directory
            or self.working_directory.startswith("/")
            or "\x00" in self.working_directory
            or any(part == ".." for part in Path(self.working_directory).parts)
        ):
            raise ValueError("command profile working directory must stay inside workspace")
        if self.network not in {"none", "approved"}:
            raise ValueError("command profile network must be none or approved")
        for name, limit in (
            ("max_argv_items", self.max_argv_items),
            ("max_argument_length", self.max_argument_length),
            ("max_working_directory_length", self.max_working_directory_length),
        ):
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError(f"command profile {name} must be a positive integer")
        for key, environment_value in self.environment.items():
            if (
                not isinstance(key, str)
                or not key
                or not isinstance(environment_value, str)
                or "\x00" in key
                or "\x00" in environment_value
            ):
                raise ValueError("command profile environment must contain safe strings")
            if any(marker in key.upper() for marker in ("SECRET", "TOKEN", "PASSWORD", "API_KEY")):
                raise ValueError("command profile environment cannot carry credentials")

    def allows_executable(self, executable: str) -> bool:
        return executable in self.executable_allowlist


class CommandProfileRegistry:
    """Explicit registry of commands that may cross the ToolHarness boundary."""

    def __init__(self) -> None:
        self._profiles: dict[str, CommandProfile] = {}

    def register(self, profile: CommandProfile) -> None:
        if profile.name in self._profiles:
            raise ValueError(f"duplicate command profile: {profile.name}")
        self._profiles[profile.name] = profile

    def get(self, name: str) -> CommandProfile | None:
        return self._profiles.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))


def default_command_profiles() -> CommandProfileRegistry:
    """Return the small, offline, no-network command profile set."""

    registry = CommandProfileRegistry()
    runtime_python = ["python3", "/usr/bin/python3", "/bin/python3"]
    if sys.executable and Path(sys.executable).name == "python3":
        runtime_python.append(sys.executable)
    registry.register(
        CommandProfile(
            name="python_project",
            executable_allowlist=tuple(dict.fromkeys(runtime_python)),
        )
    )
    return registry
