from __future__ import annotations

import os
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
import uuid

from coding_agent.command_profiles import (
    DEFAULT_COMMAND_LIMITS,
    CommandProfile,
    CommandProfileRegistry,
    default_command_profiles,
)
from coding_agent.domain import (
    Permission,
    RecoveryMode,
    ToolExecutionFailure,
    ToolStatus,
    WorkspaceViolation,
)
from coding_agent.sandbox.base import ExecutionSpec, ResourceLimits, SandboxExecutor
from coding_agent.sandbox.local_container import build_default_sandbox_executor
from coding_agent.sandbox.policy import SandboxPolicy, SandboxPolicyError
from coding_agent.test_profiles import TestProfileRegistry
from coding_agent.tools.base import ToolContext, ToolDefinition, ToolOutcome, ToolRegistry
from coding_agent.workspace import tree_fingerprint


def build_builtin_registry(
    test_profiles: TestProfileRegistry,
    *,
    command_profiles: CommandProfileRegistry | None = None,
    sandbox_executor: SandboxExecutor | None = None,
    sandbox_policy: SandboxPolicy | None = None,
) -> ToolRegistry:
    executor = sandbox_executor or build_default_sandbox_executor()
    policy = sandbox_policy or SandboxPolicy()
    registry = ToolRegistry()
    _register_read_file(registry)
    _register_edit_file(registry)
    _register_search_files(registry)
    _register_restricted_test(
        registry,
        test_profiles,
        sandbox_executor=executor,
        sandbox_policy=policy,
    )
    _register_run_command(
        registry,
        command_profiles or default_command_profiles(),
        sandbox_executor=executor,
        sandbox_policy=policy,
    )
    return registry


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_WORKSPACE_RELATIVE_PATH_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 4096,
    "description": (
        "Workspace-relative path. Never start with '/' or use '..'. Prefer an exact "
        "path from repository_snapshot.file_paths; for example, use 'formatting.py', "
        "not '/formatting.py'."
    ),
}


def _register_read_file(registry: ToolRegistry) -> None:
    definition = ToolDefinition(
        name="read_file",
        description=(
            "Read a UTF-8 text file inside the isolated workspace with line numbers. "
            "The path must be workspace-relative; prefer an exact path from "
            "repository_snapshot.file_paths."
        ),
        permission=Permission.READ,
        timeout_seconds=10.0,
        recovery_mode=RecoveryMode.READ_ONLY,
        input_schema=_object_schema(
            {
                "path": dict(_WORKSPACE_RELATIVE_PATH_SCHEMA),
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            ["path"],
        ),
    )

    def handler(arguments: dict[str, Any], context: ToolContext) -> ToolOutcome:
        context.check_deadline()
        path = context.workspace.resolve(arguments["path"])
        if not path.is_file():
            raise ToolExecutionFailure("path is not a regular file", kind="not_a_file")
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ToolExecutionFailure(
                "file exceeds the M1 2 MiB read limit",
                kind="file_too_large",
            )
        start = int(arguments.get("start_line", 1))
        end = arguments.get("end_line")
        if end is not None and int(end) < start:
            raise ToolExecutionFailure(
                "end_line must be greater than or equal to start_line",
                kind="invalid_line_range",
            )
        raw = path.read_text(encoding="utf-8")
        lines = raw.splitlines()
        selected = lines[start - 1 : int(end) if end is not None else None]
        rendered = "\n".join(
            f"{number}: {line}" for number, line in enumerate(selected, start=start)
        )
        truncated = len(rendered) > context.max_output_chars
        return ToolOutcome(
            data={
                "path": context.workspace.relative(path),
                "content": rendered[: context.max_output_chars],
                "total_lines": len(lines),
                "start_line": start,
                "end_line": min(len(lines), start + len(selected) - 1)
                if selected
                else start - 1,
            },
            truncated=truncated,
        )

    registry.register(definition, handler)


def _register_search_files(registry: ToolRegistry) -> None:
    definition = ToolDefinition(
        name="search_files",
        description=(
            "Find a case-sensitive literal string in regular UTF-8 workspace files. "
            "Use this to locate content before read_file; path must be workspace-relative. "
            "This tool does not accept regex, globs, shell commands, or Git metadata."
        ),
        permission=Permission.READ,
        timeout_seconds=10.0,
        recovery_mode=RecoveryMode.READ_ONLY,
        input_schema=_object_schema(
            {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "description": "Case-sensitive literal text to find; not a regex.",
                },
                "path": {
                    **_WORKSPACE_RELATIVE_PATH_SCHEMA,
                    "description": (
                        "Optional workspace-relative file or directory; defaults to '.'. "
                        "Never start with '/' or use '..'. Prefer a directory from "
                        "repository_snapshot.file_paths."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            ["query"],
        ),
    )

    def handler(arguments: dict[str, Any], context: ToolContext) -> ToolOutcome:
        context.check_deadline()
        relative_root = str(arguments.get("path", "."))
        lexical_root = context.workspace.root / relative_root
        if lexical_root.is_symlink():
            raise ToolExecutionFailure(
                "search root must not be a symlink",
                kind="invalid_search_root",
            )
        root = context.workspace.resolve(relative_root)
        if not root.is_file() and not root.is_dir():
            raise ToolExecutionFailure(
                "search root must be a regular file or directory",
                kind="invalid_search_root",
            )

        query = str(arguments["query"])
        max_results = int(arguments.get("max_results", 20))
        max_files = 1000
        max_bytes = 8 * 1024 * 1024
        candidates: list[Path] = []
        discovery_truncated = False
        if root.is_file():
            candidates.append(root)
        else:
            for current, dir_names, file_names in os.walk(root, followlinks=False):
                current_path = Path(current)
                dir_names[:] = sorted(
                    name
                    for name in dir_names
                    if name != ".git" and not (current_path / name).is_symlink()
                )
                for name in sorted(file_names):
                    if len(candidates) >= max_files:
                        discovery_truncated = True
                        break
                    candidate = current_path / name
                    if candidate.is_symlink() or ".git" in candidate.parts:
                        continue
                    candidates.append(candidate)
                if discovery_truncated:
                    break

        matches: list[dict[str, Any]] = []
        files_scanned = 0
        bytes_scanned = 0
        truncated = discovery_truncated
        for candidate in candidates:
            context.check_deadline()
            if files_scanned >= max_files or len(matches) >= max_results:
                truncated = True
                break
            if not candidate.is_file() or candidate.is_symlink():
                continue
            size = candidate.stat().st_size
            if size > 2 * 1024 * 1024 or bytes_scanned + size > max_bytes:
                truncated = True
                continue
            try:
                content = candidate.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            files_scanned += 1
            bytes_scanned += size
            for line_number, line in enumerate(content.splitlines(), start=1):
                if query not in line:
                    continue
                matches.append(
                    {
                        "path": context.workspace.relative(candidate),
                        "line": line_number,
                        "text": line[:500],
                    }
                )
                if len(matches) >= max_results:
                    truncated = True
                    break

        return ToolOutcome(
            data={
                "query": query,
                "path": relative_root,
                "matches": matches,
                "match_count": len(matches),
                "files_scanned": files_scanned,
                "bytes_scanned": bytes_scanned,
            },
            truncated=truncated,
        )

    registry.register(definition, handler)


def _register_edit_file(registry: ToolRegistry) -> None:
    definition = ToolDefinition(
        name="edit_file",
        description=(
            "Atomically replace one exact, unique text occurrence in a workspace file. "
            "The path must be workspace-relative; prefer an exact path from "
            "repository_snapshot.file_paths."
        ),
        permission=Permission.WRITE,
        timeout_seconds=10.0,
        recovery_mode=RecoveryMode.RECONCILABLE_WRITE,
        input_schema=_object_schema(
            {
                "path": dict(_WORKSPACE_RELATIVE_PATH_SCHEMA),
                "old_text": {"type": "string", "minLength": 1},
                "new_text": {"type": "string"},
            },
            ["path", "old_text", "new_text"],
        ),
    )

    def handler(arguments: dict[str, Any], context: ToolContext) -> ToolOutcome:
        context.check_deadline()
        path = context.workspace.resolve(arguments["path"])
        if not path.is_file() or path.is_symlink():
            raise ToolExecutionFailure(
                "edit target must be a regular non-symlink file",
                kind="invalid_edit_target",
            )
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ToolExecutionFailure(
                "file exceeds the M1 2 MiB edit limit",
                kind="file_too_large",
            )
        content = path.read_text(encoding="utf-8")
        old_text = arguments["old_text"]
        occurrences = content.count(old_text)
        if occurrences != 1:
            raise ToolExecutionFailure(
                f"old_text must occur exactly once; found {occurrences}",
                kind="non_unique_edit",
                data={"occurrences": occurrences},
            )
        updated = content.replace(old_text, arguments["new_text"], 1)
        context.check_deadline()
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.chmod(path.stat().st_mode)
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return ToolOutcome(
            data={
                "path": context.workspace.relative(path),
                "replacements": 1,
                "bytes_before": len(content.encode("utf-8")),
                "bytes_after": len(updated.encode("utf-8")),
            }
        )

    registry.register(definition, handler)


def _register_restricted_test(
    registry: ToolRegistry,
    test_profiles: TestProfileRegistry,
    *,
    sandbox_executor: SandboxExecutor,
    sandbox_policy: SandboxPolicy,
) -> None:
    if not test_profiles.names:
        raise ValueError("at least one trusted test profile is required")
    profile_timeouts = []
    for name in test_profiles.names:
        profile = test_profiles.get(name)
        assert profile is not None
        profile_timeouts.append(profile.timeout_seconds)
    definition = ToolDefinition(
        name="restricted_test",
        description="Run one trusted test profile in the isolated workspace.",
        permission=Permission.EXECUTE_TEST,
        timeout_seconds=max(profile_timeouts) + 1.0,
        recovery_mode=RecoveryMode.REPEATABLE_OBSERVATION,
        timeout_enforcement="sandbox",
        input_schema=_object_schema(
            {"profile": {"type": "string", "enum": list(test_profiles.names)}},
            ["profile"],
        ),
    )

    def handler(arguments: dict[str, Any], context: ToolContext) -> ToolOutcome:
        context.check_deadline()
        profile = test_profiles.get(arguments["profile"])
        if profile is None:
            raise ToolExecutionFailure("unknown trusted test profile", kind="unknown_profile")
        execution_id = context.execution_id or f"sandbox-{uuid.uuid4()}"
        timeout = min(profile.timeout_seconds, max(0.001, context.deadline - time.monotonic()))
        limits = replace(
            profile.limits,
            wall_seconds=min(profile.limits.wall_seconds, timeout),
        )
        return _execute_sandbox(
            context=context,
            sandbox_executor=sandbox_executor,
            sandbox_policy=sandbox_policy,
            argv=profile.argv,
            profile_name=profile.name,
            image=profile.image,
            working_directory=profile.working_directory,
            network=profile.network,
            limits=limits,
            environment=profile.environment,
            execution_id=execution_id,
            base_data={"profile": profile.name},
            success_field="passed",
        )

    registry.register(definition, handler)


def _register_run_command(
    registry: ToolRegistry,
    command_profiles: CommandProfileRegistry,
    *,
    sandbox_executor: SandboxExecutor,
    sandbox_policy: SandboxPolicy,
) -> None:
    if not command_profiles.names:
        raise ValueError("at least one trusted command profile is required")
    profile_timeouts = []
    for name in command_profiles.names:
        profile = command_profiles.get(name)
        assert profile is not None
        profile_timeouts.append(profile.timeout_seconds)
    definition = ToolDefinition(
        name="run_command",
        description=(
            "Run a structured argv in a trusted sandbox profile; shell strings and "
            "shell interpreters are not accepted."
        ),
        permission=Permission.EXECUTE_COMMAND,
        timeout_seconds=max(profile_timeouts) + 1.0,
        recovery_mode=RecoveryMode.NON_IDEMPOTENT,
        timeout_enforcement="sandbox",
        input_schema=_object_schema(
            {
                "profile": {"type": "string", "enum": list(command_profiles.names)},
                "argv": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                },
                "cwd": {"type": "string", "minLength": 1, "maxLength": 256},
            },
            ["profile", "argv"],
        ),
    )

    def handler(arguments: dict[str, Any], context: ToolContext) -> ToolOutcome:
        context.check_deadline()
        profile = command_profiles.get(arguments["profile"])
        if profile is None:
            raise ToolExecutionFailure("unknown trusted command profile", kind="unknown_profile")
        argv = tuple(arguments["argv"])
        if len(argv) > profile.max_argv_items:
            return _command_rejected(
                "command argv exceeds the profile item limit",
                kind="command_argv_too_long",
            )
        if any(len(argument) > profile.max_argument_length for argument in argv):
            return _command_rejected(
                "command argument exceeds the profile length limit",
                kind="command_argument_too_long",
            )
        if not profile.allows_executable(argv[0]):
            return _command_rejected(
                "executable is not allowlisted by the command profile",
                kind="command_executable_denied",
            )
        if Path(argv[0]).name.lower() in {"sh", "bash", "dash", "zsh", "fish", "csh", "tcsh"}:
            return _command_rejected(
                "shell interpreters are not permitted",
                kind="command_shell_forbidden",
            )
        working_directory = str(arguments.get("cwd", profile.working_directory))
        if len(working_directory) > profile.max_working_directory_length:
            return _command_rejected(
                "command working directory exceeds the profile length limit",
                kind="command_working_directory_too_long",
            )
        try:
            resolved_working_directory = context.workspace.resolve(working_directory)
        except (WorkspaceViolation, FileNotFoundError, OSError):
            return _command_rejected(
                "command working directory is not inside the workspace",
                kind="command_working_directory_invalid",
            )
        if not resolved_working_directory.is_dir():
            return _command_rejected(
                "command working directory must be a directory",
                kind="command_working_directory_invalid",
            )
        if profile.approval_required or profile.network != "none" or _limits_exceed_default(profile):
            return _command_rejected(
                "command profile requires an explicit one-time approval",
                kind="command_approval_required",
                status=ToolStatus.PERMISSION_DENIED,
            )
        execution_id = context.execution_id or f"sandbox-{uuid.uuid4()}"
        timeout = min(profile.timeout_seconds, max(0.001, context.deadline - time.monotonic()))
        limits = replace(
            profile.limits,
            wall_seconds=min(profile.limits.wall_seconds, timeout),
        )
        return _execute_sandbox(
            context=context,
            sandbox_executor=sandbox_executor,
            sandbox_policy=sandbox_policy,
            argv=argv,
            profile_name=profile.name,
            image=profile.image,
            working_directory=working_directory,
            network=profile.network,
            limits=limits,
            environment=profile.environment,
            execution_id=execution_id,
            base_data={
                "profile": profile.name,
                "argv": list(argv),
                "cwd": working_directory,
            },
            success_field="command_succeeded",
        )

    registry.register(definition, handler)


def _command_rejected(
    message: str,
    *,
    kind: str,
    status: ToolStatus = ToolStatus.INVALID_ARGUMENTS,
) -> ToolOutcome:
    return ToolOutcome(
        status=status,
        error={"kind": kind, "message": message},
    )


def _limits_exceed_default(profile: CommandProfile) -> bool:
    for name in (
        "wall_seconds",
        "cpu_seconds",
        "memory_bytes",
        "writable_bytes",
        "pids",
        "stdout_bytes",
        "stderr_bytes",
    ):
        if getattr(profile.limits, name) > getattr(DEFAULT_COMMAND_LIMITS, name):
            return True
    return profile.timeout_seconds > DEFAULT_COMMAND_LIMITS.wall_seconds


def _sandbox_environment(profile_environment: Any) -> dict[str, str]:
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": "",
        "HOME": "/tmp/home",
        "CODING_AGENT_WORKSPACE": "/workspace",
        "PWD": "/workspace",
        **dict(profile_environment),
    }


def _execute_sandbox(
    *,
    context: ToolContext,
    sandbox_executor: SandboxExecutor,
    sandbox_policy: SandboxPolicy,
    argv: tuple[str, ...],
    profile_name: str,
    image: str,
    working_directory: str,
    network: str,
    limits: ResourceLimits,
    environment: Any,
    execution_id: str,
    base_data: dict[str, Any],
    success_field: str,
) -> ToolOutcome:
    try:
        capabilities = sandbox_executor.capabilities()
    except Exception as exc:
        raise ToolExecutionFailure(
            "sandbox capability probe failed",
            kind="sandbox_capability_probe_failed",
        ) from exc
    try:
        spec = ExecutionSpec(
            argv=argv,
            workspace=context.workspace.root,
            working_directory=working_directory,
            environment=_sandbox_environment(environment),
            network=network,  # type: ignore[arg-type]
            limits=limits,
            profile_name=profile_name,
            image=image,
            image_digest=(
                str(capabilities.metadata["image_digest"])
                if capabilities.metadata.get("image_digest") is not None
                else None
            ),
            execution_id=execution_id,
        )
        sandbox_policy.validate(spec, capabilities)
    except SandboxPolicyError as exc:
        return ToolOutcome(
            data={
                **base_data,
                success_field: False,
                "execution_id": execution_id,
                "sandbox": capabilities.to_dict(),
            },
            status=ToolStatus.EXECUTION_ERROR,
            error={"kind": exc.kind, "message": str(exc)},
        )
    except (OSError, ValueError) as exc:
        raise ToolExecutionFailure(
            "sandbox execution specification could not be prepared",
            kind="sandbox_execution_failed",
        ) from exc
    try:
        workspace_revision_before = tree_fingerprint(context.workspace.root)
        execution = sandbox_executor.execute(spec)
        workspace_revision_after = tree_fingerprint(context.workspace.root)
    except OSError as exc:
        raise ToolExecutionFailure(
            "sandbox execution or workspace observation failed",
            kind="sandbox_execution_failed",
        ) from exc
    data = {
        **base_data,
        success_field: execution.status == "exited" and execution.exit_code == 0,
        "exit_code": execution.exit_code,
        "stdout": execution.stdout,
        "stderr": execution.stderr,
        "process_group_terminated": execution.status in {"timeout", "resource_exhausted"},
        "execution_id": execution_id,
        "limit_hit": execution.limit_hit,
        "workspace_revision_before": workspace_revision_before,
        "workspace_revision_after": workspace_revision_after,
        "sandbox": {
            "status": execution.status,
            "duration_ms": execution.duration_ms,
            **execution.backend_metadata,
        },
    }
    truncated = bool(
        execution.backend_metadata.get("stdout_truncated")
        or execution.backend_metadata.get("stderr_truncated")
    )
    if execution.status == "timeout":
        return ToolOutcome(
            data=data,
            status=ToolStatus.TIMEOUT,
            error={"kind": "sandbox_timeout", "message": "sandbox execution exceeded its wall limit"},
            truncated=truncated,
        )
    if execution.status == "resource_exhausted":
        return ToolOutcome(
            data=data,
            status=ToolStatus.EXECUTION_ERROR,
            error={
                "kind": "sandbox_resource_exhausted",
                "message": execution.limit_hit or "sandbox resource limit exceeded",
            },
            truncated=truncated,
        )
    if execution.status == "sandbox_error":
        return ToolOutcome(
            data=data,
            status=ToolStatus.EXECUTION_ERROR,
            error={
                "kind": str(
                    execution.backend_metadata.get(
                        "error_kind", "sandbox_execution_failed"
                    )
                ),
                "message": "sandbox backend rejected the execution",
            },
            truncated=truncated,
        )
    return ToolOutcome(data=data, truncated=truncated)
