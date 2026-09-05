"""OS isolation boundary for trusted test and command profiles."""

from .base import (
    ExecutionResult,
    ExecutionSpec,
    ResourceLimits,
    SandboxCapabilities,
    SandboxExecutor,
)
from .local_container import (
    FakeSandboxExecutor,
    FailClosedSandboxExecutor,
    LinuxNamespaceExecutor,
    build_default_sandbox_executor,
)
from .policy import SandboxPolicy, SandboxPolicyError

__all__ = [
    "ExecutionResult",
    "ExecutionSpec",
    "FakeSandboxExecutor",
    "FailClosedSandboxExecutor",
    "LinuxNamespaceExecutor",
    "ResourceLimits",
    "SandboxCapabilities",
    "SandboxExecutor",
    "SandboxPolicy",
    "SandboxPolicyError",
    "build_default_sandbox_executor",
]
