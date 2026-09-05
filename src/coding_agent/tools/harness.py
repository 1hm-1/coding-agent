from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import signal
import time
from dataclasses import dataclass
from dataclasses import replace
from typing import Any

from coding_agent.domain import (
    SchemaValidationError,
    RecoveryMode,
    ToolCall,
    ToolDeadlineExceeded,
    ToolExecutionFailure,
    ToolResult,
    ToolStatus,
    WorkspaceViolation,
)
from coding_agent.tools.base import (
    AuditSink,
    ToolContext,
    ToolHandler,
    ToolOutcome,
    ToolRegistry,
    validate_schema,
)


@dataclass(frozen=True)
class ToolPreparation:
    """Side-effect-free intent information persisted before tool execution."""

    recovery_mode: RecoveryMode
    pre_revision: str | None = None
    planned_post_revision: str | None = None
    error: ToolResult | None = None


class ToolHarness:
    """Single validation, authorization, timeout, and error boundary for tools."""

    def __init__(self, registry: ToolRegistry, audit_sink: AuditSink | None = None):
        self.registry = registry
        self.audit_sink = audit_sink or (lambda _: None)

    def prepare(self, call: ToolCall, context: ToolContext) -> ToolPreparation:
        """Validate an intent and calculate reconciliation hashes without writing."""

        registered = self.registry.get(call.name)
        if registered is None:
            return ToolPreparation(
                recovery_mode=RecoveryMode.READ_ONLY,
                error=ToolResult(
                    call_id=call.id,
                    tool_name=call.name,
                    status=ToolStatus.NOT_FOUND,
                    error={"kind": "unknown_tool", "message": f"unknown tool: {call.name}"},
                ),
            )
        definition, _ = registered
        try:
            validate_schema(call.arguments, definition.input_schema)
            if definition.permission not in context.allowed_permissions:
                raise PermissionError(f"{definition.permission.value} permission is required")
            if definition.recovery_mode is RecoveryMode.RECONCILABLE_WRITE:
                pre_revision, planned_post_revision = self._edit_revisions(call, context)
                return ToolPreparation(
                    recovery_mode=definition.recovery_mode,
                    pre_revision=pre_revision,
                    planned_post_revision=planned_post_revision,
                )
            return ToolPreparation(recovery_mode=definition.recovery_mode)
        except SchemaValidationError as exc:
            return self._preparation_error(
                call,
                definition.recovery_mode,
                ToolStatus.INVALID_ARGUMENTS,
                "schema_validation",
                str(exc),
            )
        except PermissionError as exc:
            return self._preparation_error(
                call,
                definition.recovery_mode,
                ToolStatus.PERMISSION_DENIED,
                "permission_denied",
                str(exc),
            )
        except (WorkspaceViolation, FileNotFoundError) as exc:
            return self._preparation_error(
                call,
                definition.recovery_mode,
                ToolStatus.INVALID_ARGUMENTS,
                "workspace_path",
                str(exc),
            )
        except ToolExecutionFailure as exc:
            return self._preparation_error(
                call,
                definition.recovery_mode,
                ToolStatus.EXECUTION_ERROR,
                exc.kind,
                str(exc),
                data=exc.data,
            )
        except Exception as exc:
            return self._preparation_error(
                call,
                definition.recovery_mode,
                ToolStatus.EXECUTION_ERROR,
                "prepare_error",
                f"{type(exc).__name__}: {exc}",
            )

    def current_revision(self, call: ToolCall, context: ToolContext) -> str | None:
        """Return the current revision used by reconciliable write recovery."""

        registered = self.registry.get(call.name)
        if registered is None or registered[0].recovery_mode is not RecoveryMode.RECONCILABLE_WRITE:
            return None
        path = context.workspace.resolve(call.arguments["path"])
        if not path.is_file() or path.is_symlink():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _edit_revisions(call: ToolCall, context: ToolContext) -> tuple[str, str]:
        path = context.workspace.resolve(call.arguments["path"])
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
        old_text = call.arguments["old_text"]
        occurrences = content.count(old_text)
        if occurrences != 1:
            raise ToolExecutionFailure(
                f"old_text must occur exactly once; found {occurrences}",
                kind="non_unique_edit",
                data={"occurrences": occurrences},
            )
        updated = content.replace(old_text, call.arguments["new_text"], 1)
        return (
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
            hashlib.sha256(updated.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _preparation_error(
        call: ToolCall,
        recovery_mode: RecoveryMode,
        status: ToolStatus,
        kind: str,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> ToolPreparation:
        return ToolPreparation(
            recovery_mode=recovery_mode,
            error=ToolResult(
                call_id=call.id,
                tool_name=call.name,
                status=status,
                data=data or {},
                error={"kind": kind, "message": message},
            ),
        )

    def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        started = time.monotonic()
        self._audit("tool_harness_started", call, {"arguments": _redact_args(call.arguments)})
        registered = self.registry.get(call.name)
        if registered is None:
            return self._finish(
                call,
                started,
                ToolOutcome(
                    status=ToolStatus.NOT_FOUND,
                    error={"kind": "unknown_tool", "message": f"unknown tool: {call.name}"},
                ),
            )

        definition, handler = registered
        try:
            validate_schema(call.arguments, definition.input_schema)
            if definition.permission not in context.allowed_permissions:
                return self._finish(
                    call,
                    started,
                    ToolOutcome(
                        status=ToolStatus.PERMISSION_DENIED,
                        error={
                            "kind": "permission_denied",
                            "message": f"{definition.permission.value} permission is required",
                        },
                    ),
                )
            deadline = min(context.deadline, started + definition.timeout_seconds)
            bounded_context = replace(context, deadline=deadline)
            if definition.timeout_enforcement == "sandbox":
                outcome = handler(call.arguments, bounded_context)
            else:
                outcome = self._execute_in_worker(
                    handler,
                    call.arguments,
                    bounded_context,
                    deadline,
                )
            if time.monotonic() > deadline:
                raise ToolDeadlineExceeded("tool exceeded its configured timeout")
            outcome = self._limit_output(outcome, context.max_output_chars)
        except SchemaValidationError as exc:
            outcome = ToolOutcome(
                status=ToolStatus.INVALID_ARGUMENTS,
                error={"kind": "schema_validation", "message": str(exc)},
            )
        except PermissionError as exc:
            outcome = ToolOutcome(
                status=ToolStatus.PERMISSION_DENIED,
                error={"kind": "permission_denied", "message": str(exc)},
            )
        except ToolDeadlineExceeded as exc:
            outcome = ToolOutcome(
                status=ToolStatus.TIMEOUT,
                error={"kind": "timeout", "message": str(exc)},
            )
        except (WorkspaceViolation, FileNotFoundError) as exc:
            outcome = ToolOutcome(
                status=ToolStatus.INVALID_ARGUMENTS,
                error={"kind": "workspace_path", "message": str(exc)},
            )
        except ToolExecutionFailure as exc:
            outcome = ToolOutcome(
                data=exc.data,
                status=ToolStatus.EXECUTION_ERROR,
                error={"kind": exc.kind, "message": str(exc)},
            )
        except Exception as exc:  # The harness must not leak handler exceptions into the runtime.
            outcome = ToolOutcome(
                status=ToolStatus.EXECUTION_ERROR,
                error={"kind": "unhandled_tool_error", "message": f"{type(exc).__name__}: {exc}"},
            )
        return self._finish(call, started, outcome)

    @staticmethod
    def _execute_in_worker(
        handler: ToolHandler,
        arguments: dict[str, Any],
        context: ToolContext,
        deadline: float,
    ) -> ToolOutcome:
        """Run ordinary handlers behind a killable Linux process boundary."""

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ToolDeadlineExceeded("tool deadline exceeded before handler start")
        try:
            process_context = multiprocessing.get_context("fork")
        except ValueError as exc:
            raise ToolExecutionFailure(
                "a fork process boundary is required for tool timeout enforcement",
                kind="timeout_boundary_unavailable",
            ) from exc
        parent, child = process_context.Pipe(duplex=False)
        process = process_context.Process(
            target=_handler_worker,
            args=(child, handler, arguments, context),
            daemon=False,
        )
        process.start()
        child.close()
        try:
            remaining = max(0.0, deadline - time.monotonic())
            if not parent.poll(remaining):
                _kill_worker_process_group(process)
                raise ToolDeadlineExceeded("tool exceeded its configured timeout")
            try:
                payload = parent.recv()
            except EOFError as exc:
                raise ToolExecutionFailure(
                    "tool worker exited without returning a result",
                    kind="tool_worker_failure",
                ) from exc
            process.join(timeout=1.0)
            if not isinstance(payload, dict):
                raise ToolExecutionFailure(
                    "tool worker returned a malformed result",
                    kind="tool_worker_failure",
                )
            if payload.get("kind") == "outcome" and isinstance(
                payload.get("outcome"), ToolOutcome
            ):
                return payload["outcome"]
            error_kind = str(payload.get("error_kind", "unhandled_tool_error"))
            message = str(payload.get("message", "tool worker failed"))
            data = payload.get("data")
            if error_kind == "deadline":
                raise ToolDeadlineExceeded(message)
            if error_kind == "permission":
                raise PermissionError(message)
            if error_kind == "workspace":
                raise WorkspaceViolation(message)
            if error_kind == "file_not_found":
                raise FileNotFoundError(message)
            if error_kind == "tool_execution":
                raise ToolExecutionFailure(
                    message,
                    kind=str(payload.get("tool_kind", "tool_execution_failure")),
                    data=dict(data) if isinstance(data, dict) else {},
                )
            raise RuntimeError(message)
        finally:
            parent.close()
            if process.is_alive():
                _kill_worker_process_group(process)

    def _finish(self, call: ToolCall, started: float, outcome: ToolOutcome) -> ToolResult:
        result = ToolResult(
            call_id=call.id,
            tool_name=call.name,
            status=outcome.status,
            data=outcome.data,
            error=outcome.error,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            truncated=outcome.truncated,
        )
        self._audit(
            "tool_harness_finished",
            call,
            {
                "status": result.status.value,
                "duration_ms": result.duration_ms,
                "truncated": result.truncated,
                "error_kind": result.error.get("kind") if result.error else None,
            },
        )
        return result

    @staticmethod
    def _limit_output(outcome: ToolOutcome, limit: int) -> ToolOutcome:
        serialized = json.dumps(outcome.data, ensure_ascii=False, sort_keys=True)
        if len(serialized) <= limit:
            return outcome
        return ToolOutcome(
            data={
                "preview": serialized[:limit],
                "original_chars": len(serialized),
            },
            status=outcome.status,
            error=outcome.error,
            truncated=True,
        )

    def _audit(self, event: str, call: ToolCall, payload: dict[str, Any]) -> None:
        self.audit_sink({"event": event, "call_id": call.id, "tool_name": call.name, **payload})


def _handler_worker(
    connection: Any,
    handler: ToolHandler,
    arguments: dict[str, Any],
    context: ToolContext,
) -> None:
    try:
        os.setsid()
        try:
            outcome = handler(arguments, context)
            if not isinstance(outcome, ToolOutcome):
                raise TypeError("tool handler must return ToolOutcome")
            payload: dict[str, Any] = {"kind": "outcome", "outcome": outcome}
        except ToolDeadlineExceeded as exc:
            payload = {"kind": "error", "error_kind": "deadline", "message": str(exc)}
        except PermissionError as exc:
            payload = {"kind": "error", "error_kind": "permission", "message": str(exc)}
        except WorkspaceViolation as exc:
            payload = {"kind": "error", "error_kind": "workspace", "message": str(exc)}
        except FileNotFoundError as exc:
            payload = {"kind": "error", "error_kind": "file_not_found", "message": str(exc)}
        except ToolExecutionFailure as exc:
            payload = {
                "kind": "error",
                "error_kind": "tool_execution",
                "tool_kind": exc.kind,
                "message": str(exc),
                "data": exc.data,
            }
        except Exception as exc:
            payload = {
                "kind": "error",
                "error_kind": "unhandled",
                "message": f"{type(exc).__name__}: {exc}",
            }
        connection.send(payload)
    finally:
        connection.close()


def _kill_worker_process_group(process: Any) -> None:
    pid = process.pid
    if pid is not None:
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
    process.join(timeout=1.0)
    if process.is_alive():
        process.kill()
        process.join(timeout=1.0)


def _redact_args(arguments: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in arguments.items():
        if any(marker in key.lower() for marker in ("token", "password", "secret", "api_key")):
            redacted[key] = "[REDACTED]"
        else:
            redacted[key] = value
    return redacted
