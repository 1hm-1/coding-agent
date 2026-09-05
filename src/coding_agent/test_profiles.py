from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Mapping

from coding_agent.sandbox.base import ResourceLimits


DEFAULT_TEST_LIMITS = ResourceLimits(
    wall_seconds=30.0,
    cpu_seconds=20.0,
    memory_bytes=512 * 1024 * 1024,
    writable_bytes=128 * 1024 * 1024,
    pids=64,
    stdout_bytes=10_000,
    stderr_bytes=10_000,
)


@dataclass(frozen=True)
class TestProfile:
    """A trusted test command. Models select a name and never provide argv."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float = 30.0
    image: str = "linux-namespace-rootfs"
    working_directory: str = "."
    network: str = "none"
    limits: ResourceLimits = field(default_factory=lambda: DEFAULT_TEST_LIMITS)
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError(f"invalid test profile name: {self.name!r}")
        if not self.argv or any(not isinstance(value, str) or not value for value in self.argv):
            raise ValueError("test profile argv cannot be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("test profile timeout must be positive")
        if not self.image:
            raise ValueError("test profile image identity is required")
        if not self.working_directory or self.working_directory.startswith("/"):
            raise ValueError("test profile working directory must be relative")
        if any(part == ".." for part in self.working_directory.split("/")):
            raise ValueError("test profile working directory cannot traverse parents")
        if self.network not in {"none", "approved"}:
            raise ValueError("test profile network must be none or approved")
        if any(not isinstance(key, str) or not key for key in self.environment):
            raise ValueError("test profile environment keys must be non-empty strings")


class TestProfileRegistry:
    def __init__(self) -> None:
        self._profiles: dict[str, TestProfile] = {}

    def register(self, profile: TestProfile) -> None:
        if not profile.name or not profile.name.replace("_", "").isalnum():
            raise ValueError(f"invalid test profile name: {profile.name!r}")
        if not profile.argv:
            raise ValueError("test profile argv cannot be empty")
        if profile.timeout_seconds <= 0:
            raise ValueError("test profile timeout must be positive")
        if profile.name in self._profiles:
            raise ValueError(f"duplicate test profile: {profile.name}")
        self._profiles[profile.name] = profile

    def get(self, name: str) -> TestProfile | None:
        return self._profiles.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))


def default_test_profiles() -> TestProfileRegistry:
    registry = TestProfileRegistry()
    runtime_python = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable
    registry.register(
        TestProfile(
            name="python_unittest",
            argv=(runtime_python, "-m", "unittest", "discover", "-v"),
            timeout_seconds=30.0,
        )
    )
    return registry
