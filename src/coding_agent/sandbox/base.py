from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Literal, Mapping, Protocol

from coding_agent.domain import JsonObject


NetworkMode = Literal["none", "approved"]
ExecutionStatus = Literal[
    "exited",
    "timeout",
    "resource_exhausted",
    "sandbox_error",
]


@dataclass(frozen=True)
class ResourceLimits:
    """Hard limits requested for one isolated execution."""

    wall_seconds: float
    cpu_seconds: float
    memory_bytes: int
    writable_bytes: int
    pids: int
    stdout_bytes: int
    stderr_bytes: int

    def __post_init__(self) -> None:
        for name in ("wall_seconds", "cpu_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite positive number")
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        for name in (
            "memory_bytes",
            "writable_bytes",
            "pids",
            "stdout_bytes",
            "stderr_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    def to_dict(self) -> JsonObject:
        return {
            "wall_seconds": self.wall_seconds,
            "cpu_seconds": self.cpu_seconds,
            "memory_bytes": self.memory_bytes,
            "writable_bytes": self.writable_bytes,
            "pids": self.pids,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
        }


@dataclass(frozen=True)
class ExecutionSpec:
    """Provider/tool-neutral description of one sandbox execution."""

    argv: tuple[str, ...]
    workspace: Path
    working_directory: str
    environment: Mapping[str, str]
    network: NetworkMode
    limits: ResourceLimits
    profile_name: str
    image: str = "linux-namespace-rootfs"
    image_digest: str | None = None
    execution_id: str = ""

    def __post_init__(self) -> None:
        if not self.argv or any(not isinstance(item, str) or not item for item in self.argv):
            raise ValueError("execution argv cannot be empty")
        if self.network not in {"none", "approved"}:
            raise ValueError("execution network must be none or approved")
        if not self.profile_name:
            raise ValueError("execution profile name is required")
        if not self.image:
            raise ValueError("execution image identity is required")
        if any("\x00" in item for item in self.argv):
            raise ValueError("execution argv cannot contain NUL bytes")
        if "\x00" in self.working_directory:
            raise ValueError("execution working directory cannot contain NUL bytes")
        if any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or "\x00" in key
            or "\x00" in value
            for key, value in self.environment.items()
        ):
            raise ValueError("execution environment must contain safe strings")

    def to_dict(self) -> JsonObject:
        """Return an event-safe projection without environment values."""

        return {
            "argv": list(self.argv),
            "working_directory": self.working_directory,
            "environment_keys": sorted(self.environment),
            "network": self.network,
            "limits": self.limits.to_dict(),
            "profile_name": self.profile_name,
            "image": self.image,
            "image_digest": self.image_digest,
            "execution_id": self.execution_id,
        }

    def to_wire(self) -> JsonObject:
        """Return the private runner payload; it never enters the journal."""

        return {
            **self.to_dict(),
            "workspace": str(self.workspace),
            "environment": dict(self.environment),
        }


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    limit_hit: str | None = None
    backend_metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {
            "exited",
            "timeout",
            "resource_exhausted",
            "sandbox_error",
        }:
            raise ValueError(f"unknown execution status: {self.status}")
        if self.duration_ms < 0:
            raise ValueError("execution duration cannot be negative")

    def to_dict(self) -> JsonObject:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "limit_hit": self.limit_hit,
            "backend_metadata": dict(self.backend_metadata),
        }


@dataclass(frozen=True)
class SandboxCapabilities:
    """Capability probe snapshot used by policy and observability."""

    backend: str
    version: str
    available: bool
    features: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    metadata: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "backend": self.backend,
            "version": self.version,
            "available": self.available,
            "features": list(self.features),
            "missing": list(self.missing),
            "metadata": dict(self.metadata),
        }


class SandboxExecutor(Protocol):
    def capabilities(self) -> SandboxCapabilities:
        ...

    def execute(self, spec: ExecutionSpec) -> ExecutionResult:
        ...
