from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import random
import time
from typing import Callable, Mapping, Sequence

from coding_agent.compression import (
    CompressionEngine,
    SummaryValidationError,
    select_event_range,
    stale_summary,
)
from coding_agent.context import ContextBuilder
from coding_agent.domain import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    BackendError,
    BuiltContext,
    ContextBuildInput,
    ContextBudgetError,
    EventType,
    InvariantViolation,
    Message,
    ModelRequest,
    ModelResponse,
    RecoveryMode,
    RunResult,
    RuntimeSnapshot,
    RuntimeState,
    Session,
    SummaryRecord,
    TestFact,
    ToolCall,
    ToolCallState,
    ToolResult,
    ToolStatus,
    apply_snapshot,
)
from coding_agent.models.base import ModelBackend
from coding_agent.models.retry import bounded_exponential_backoff
from coding_agent.persistence import ModelCallMutation, SummaryMutation, ToolCallMutation
from coding_agent.tools.base import ToolContext, ToolRegistry
from coding_agent.tools.harness import ToolHarness
from coding_agent.trajectory import TrajectoryRecorder
from coding_agent.workspace import WorkspaceManager, tree_fingerprint


class StateMachine:
    def __init__(self, recorder: TrajectoryRecorder):
        self.recorder = recorder

    def transition(
        self,
        session: Session,
        target: RuntimeState,
        *,
        reason: str,
        payload: dict[str, object] | None = None,
        snapshot_after: RuntimeSnapshot | None = None,
        clear_interrupt: bool = False,
    ) -> None:
        source = session.state
        if target not in ALLOWED_TRANSITIONS[source]:
            raise InvariantViolation(
                f"illegal state transition {source.value} -> {target.value}"
            )
        event_payload: dict[str, object] = {
            "from": source.value,
            "to": target.value,
            "reason": reason,
        }
        if payload:
            event_payload.update(payload)
        candidate = snapshot_after or session.to_snapshot(state=target)
        if candidate.state is not target:
            raise InvariantViolation("transition snapshot must contain the target state")
        event = self.recorder.emit(
            EventType.STATE_TRANSITION,
            target,
            event_payload,
            snapshot_after=candidate,
            expected_state=source,
            clear_interrupt=clear_interrupt,
        )
        apply_snapshot(
            session,
            replace(candidate, version=self.recorder.current_version, updated_at=event.timestamp),
        )


class AgentRuntime:
    """Explicit M1 state machine for one deterministic coding-agent run."""

    def __init__(
        self,
        *,
        session: Session,
        backend: ModelBackend,
        context_builder: ContextBuilder,
        registry: ToolRegistry,
        harness: ToolHarness,
        workspace_manager: WorkspaceManager,
        recorder: TrajectoryRecorder,
        compression_engine: CompressionEngine | None = None,
        max_compression_calls: int = 1,
        lease_owner: str | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        random_source: Callable[[], float] | None = None,
        lease_seconds: float = 60.0,
        fault_injector: Callable[[str], None] | None = None,
    ):
        self.session = session
        self.backend = backend
        self.context_builder = context_builder
        self.registry = registry
        self.harness = harness
        self.workspace_manager = workspace_manager
        self.recorder = recorder
        self.compression_engine = compression_engine
        if max_compression_calls < 0:
            raise ValueError("max_compression_calls cannot be negative")
        self.max_compression_calls = max_compression_calls
        self.lease_owner = lease_owner
        self.clock = clock or time.time
        self.sleeper = sleeper or time.sleep
        self.random_source = random_source or random.random
        self.lease_seconds = lease_seconds
        self.fault_injector = fault_injector
        self._resumed = False
        self.machine = StateMachine(recorder)
        self._model_input: tuple[Message, ...] = ()
        self._seen_tool_call_ids: set[str] = set()
        self._latest_summary = None
        self._compression_attempts: set[tuple[int, int, str]] = set()
        self._handlers: dict[RuntimeState, Callable[[], None]] = {
            RuntimeState.CREATED: self._on_created,
            RuntimeState.PREPARING_WORKSPACE: self._on_preparing_workspace,
            RuntimeState.BUILDING_CONTEXT: self._on_building_context,
            RuntimeState.CALLING_MODEL: self._on_calling_model,
            RuntimeState.DISPATCHING_TOOL: self._on_dispatching_tool,
            RuntimeState.RECORDING_OBSERVATION: self._on_recording_observation,
            RuntimeState.RETRY_WAIT: self._on_retry_wait,
        }

    def initialize(self) -> None:
        if self.session.messages:
            raise InvariantViolation("a new M1 session must not contain messages")
        initial_message = Message(role="user", content=self.session.task)
        if self.recorder.journal is not None:
            events = self.recorder.create_session(
                self.session.to_snapshot(),
                initial_message,
                session_created_payload={
                    "task": self.session.task,
                    "source_name": Path(self.session.source_path).name,
                    "backend": self.backend.name,
                },
            )
            self.session.messages.append(initial_message)
            if events:
                self.session.updated_at = events[-1].timestamp
            self.session.version = self.recorder.current_version
            return
        self.recorder.emit(
            EventType.SESSION_CREATED,
            self.session.state,
            {
                "task": self.session.task,
                "source_name": Path(self.session.source_path).name,
                "backend": self.backend.name,
            },
            snapshot_after=self.session.to_snapshot(),
        )
        self._append_message(initial_message)

    def resume(self) -> None:
        """Prepare an already persisted session for another bounded run."""

        if self.session.state in TERMINAL_STATES:
            raise InvariantViolation("terminal sessions cannot be resumed")
        if self.session.state is RuntimeState.WAITING_APPROVAL:
            raise InvariantViolation("resolve the pending call before resuming")
        resumed_from = self.session.state.value
        self._resumed = True
        self._seen_tool_call_ids = {
            str(call["id"])
            for message in self.session.messages
            if message.role == "assistant"
            for call in _assistant_tool_calls(message)
            if isinstance(call, Mapping) and call.get("id") is not None
        }
        if self.session.state is RuntimeState.INTERRUPTED:
            target = self.session.resume_target_state
            if target is None:
                raise InvariantViolation("interrupted session has no resume target")
            candidate = replace(
                self.session.to_snapshot(),
                state=target,
                resume_target_state=None,
                interrupt_requested_at=None,
            )
            self.machine.transition(
                self.session,
                target,
                reason="resume_requested",
                payload={"from": RuntimeState.INTERRUPTED.value},
                snapshot_after=candidate,
                clear_interrupt=True,
            )
        self._commit_event(
            EventType.RESUME_STARTED,
            self.session.state,
            {
                "session_id": self.session.id,
                "state": self.session.state.value,
                "resumed_from": resumed_from,
            },
            snapshot_after=self.session.to_snapshot(),
        )
        if self.session.state in {
            RuntimeState.BUILDING_CONTEXT,
            RuntimeState.CALLING_MODEL,
            RuntimeState.RETRY_WAIT,
        }:
            self._model_input = self._build_context(emit_event=False).messages

    def step(self) -> RuntimeState:
        if self.session.state in TERMINAL_STATES:
            return self.session.state
        if self.session.state is RuntimeState.WAITING_APPROVAL:
            return self.session.state
        self._renew_lease()
        if self._interrupt_requested():
            self._interrupt()
            return self.session.state
        if self.session.step_count >= self.session.policy.max_steps:
            self._fail("step_budget_exhausted", "maximum runtime steps exceeded")
            return self.session.state
        handler = self._handlers.get(self.session.state)
        if handler is None:
            self._fail(
                "missing_state_handler",
                f"no handler for state {self.session.state.value}",
            )
            return self.session.state
        self.session.step_count += 1
        handler()
        if self.session.state not in TERMINAL_STATES and self._interrupt_requested():
            self._interrupt()
        if self.session.state not in TERMINAL_STATES:
            self._renew_lease()
        return self.session.state

    def run(self) -> RunResult:
        try:
            while self.session.state not in TERMINAL_STATES:
                if self.session.state in {
                    RuntimeState.INTERRUPTED,
                    RuntimeState.WAITING_APPROVAL,
                }:
                    break
                self.step()
        except ContextBudgetError as exc:
            if self.session.state not in TERMINAL_STATES:
                self._fail(
                    exc.kind,
                    str(exc),
                    details=exc.details,
                )
            else:
                raise
        except Exception as exc:
            if self.session.state not in TERMINAL_STATES:
                self._fail(
                    "runtime_exception",
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                raise
        return RunResult(
            session_id=self.session.id,
            state=self.session.state,
            final_answer=self.session.final_answer,
            failure=self.session.failure,
            workspace_path=self.session.workspace_path,
            trace_path=str(self.recorder.path),
            step_count=self.session.step_count,
            model_calls=self.session.model_calls,
            tool_calls=self.session.tool_calls,
        )

    def _interrupt_requested(self) -> bool:
        if self.recorder.journal is not None:
            requested = self.recorder.journal.interrupt_requested_at(self.session.id)
            self.session.interrupt_requested_at = requested
            return requested is not None
        return self.session.interrupt_requested_at is not None

    def _fault(self, stage: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(stage)

    def _renew_lease(self) -> None:
        if self.recorder.journal is not None and self.lease_owner is not None:
            self.recorder.journal.renew_lease(
                self.session.id,
                self.lease_owner,
                lease_seconds=self.lease_seconds,
            )

    def _interrupt(self) -> None:
        if self.session.state in TERMINAL_STATES or self.session.state is RuntimeState.INTERRUPTED:
            return
        target = self.session.state
        candidate = replace(
            self.session.to_snapshot(),
            state=RuntimeState.INTERRUPTED,
            resume_target_state=target,
            interrupt_requested_at=None,
        )
        self.machine.transition(
            self.session,
            RuntimeState.INTERRUPTED,
            reason="interrupt_requested_at_safe_boundary",
            payload={"resume_target_state": target.value},
            snapshot_after=candidate,
            clear_interrupt=True,
        )

    def _on_created(self) -> None:
        self.machine.transition(
            self.session,
            RuntimeState.PREPARING_WORKSPACE,
            reason="task_accepted",
        )

    def _on_preparing_workspace(self) -> None:
        manifest = self.workspace_manager.create(
            self.session.source_path, self.session.id
        )
        candidate = replace(
            self.session.to_snapshot(),
            workspace_path=manifest.workspace_path,
        )
        self._commit_event(
            EventType.WORKSPACE_CREATED,
            self.session.state,
            {
                "workspace_path": manifest.workspace_path,
                "removed_external_symlinks": list(manifest.removed_external_symlinks),
            },
            snapshot_after=candidate,
        )
        self._fault("after_workspace")
        self.machine.transition(
            self.session,
            RuntimeState.BUILDING_CONTEXT,
            reason="workspace_ready",
        )

    def _on_building_context(self) -> None:
        built = self._build_context(emit_event=True)
        self._model_input = built.messages
        self._commit_event(
            EventType.CONTEXT_BUILT,
            self.session.state,
            built.manifest(),
            snapshot_after=self.session.to_snapshot(),
        )
        self.machine.transition(
            self.session,
            RuntimeState.CALLING_MODEL,
            reason="context_ready",
        )

    def _build_context(self, *, emit_event: bool) -> BuiltContext:
        request = self._context_request()
        unbounded_builder = getattr(self.context_builder, "build_unbounded", None)
        if callable(unbounded_builder):
            built = unbounded_builder(request)
        else:
            built = self._call_context_builder(request)
        if built.needs_compression:
            compressed = self._try_compress(request, built)
            if compressed is not None:
                request = self._context_request()
        if callable(unbounded_builder):
            built = self._call_context_builder(request)
        if emit_event and built.counter == "":
            raise InvariantViolation("context builder returned no token counter")
        return built

    def _call_context_builder(self, request: ContextBuildInput) -> BuiltContext:
        try:
            built = self.context_builder.build(request)
        except TypeError as new_api_error:
            # A small compatibility bridge for pre-M3 custom builders. The
            # built-in M3 builders never take this path.
            try:
                legacy = self.context_builder.build(request.task, tuple(request.messages))  # type: ignore[arg-type]
            except TypeError:
                raise new_api_error
            if isinstance(legacy, BuiltContext):
                return legacy
            built = legacy
        if isinstance(built, BuiltContext):
            return built
        if isinstance(built, Sequence):
            messages = tuple(built)
            estimated = max(0, sum(len(message.content.encode("utf-8")) for message in messages) // 4)
            return BuiltContext(
                messages=messages,
                sections=(),
                total_input_tokens=estimated,
                budget_tokens=2**31 - 1,
                provider=request.provider,
                model=request.model,
                workspace_revision=request.repository_snapshot.workspace_revision,
                counter="legacy_estimator",
                pre_compression_input_tokens=estimated,
            )
        raise InvariantViolation("context builder must return BuiltContext or messages")

    def _context_request(self) -> ContextBuildInput:
        snapshot = self.workspace_manager.repository_snapshot(
            self.session.id,
            read_paths=self._read_paths(),
            last_test=self._last_test_fact(),
        )
        summary = None
        if self.recorder.journal is not None:
            summary = self.recorder.journal.get_latest_summary(self.session.id)
            if summary is not None:
                checked = stale_summary(summary, snapshot)
                if checked.to_dict() != summary.to_dict():
                    self._commit_event(
                        EventType.SUMMARY_INVALIDATED,
                        self.session.state,
                        {
                            "summary_id": summary.summary_id,
                            "workspace_revision": snapshot.workspace_revision,
                            "reason": "workspace_revision_or_file_hash_changed",
                        },
                        snapshot_after=self.session.to_snapshot(),
                        summary=SummaryMutation(record=checked),
                    )
                    summary = checked
        provider = self.backend.name
        model = str(getattr(self.backend, "model", self.backend.name))
        return ContextBuildInput(
            session_id=self.session.id,
            task=self.session.task,
            messages=tuple(self.session.messages),
            runtime_state=self.session.state,
            policy=self.session.policy,
            repository_snapshot=snapshot,
            latest_summary=summary,
            provider=provider,
            model=model,
            pending_tool_calls=tuple(self.session.pending_tool_calls),
            active_call_id=self.session.active_call_id,
            active_call_kind=self.session.active_call_kind,
        )

    def _try_compress(
        self,
        request: ContextBuildInput,
        built: BuiltContext,
    ) -> SummaryRecord | None:
        events = self._events()
        latest = request.latest_summary
        after_sequence = latest.source_event_end if latest is not None else 2
        selected = select_event_range(events, after_sequence=after_sequence)
        marker = (
            selected.start,
            selected.end,
            selected.source_event_hash,
        ) if selected is not None else (0, 0, "none")
        if marker in self._compression_attempts:
            return None
        self._compression_attempts.add(marker)
        started_count = sum(
            event.event_type is EventType.COMPRESSION_STARTED for event in events
        )
        if started_count >= self.max_compression_calls:
            self._commit_event(
                EventType.COMPRESSION_REJECTED,
                self.session.state,
                {
                    "reason": "compression_budget_exhausted",
                    "max_compression_calls": self.max_compression_calls,
                },
                snapshot_after=self.session.to_snapshot(),
            )
            return None
        if self.compression_engine is None:
            self._commit_event(
                EventType.COMPRESSION_REJECTED,
                self.session.state,
                {
                    "reason": "summarizer_unconfigured",
                    "source_event_start": selected.start if selected else None,
                    "source_event_end": selected.end if selected else None,
                },
                snapshot_after=self.session.to_snapshot(),
            )
            return None
        if selected is None:
            self._commit_event(
                EventType.COMPRESSION_REJECTED,
                self.session.state,
                {"reason": "no_eligible_event_range"},
                snapshot_after=self.session.to_snapshot(),
            )
            return None
        self._commit_event(
            EventType.COMPRESSION_STARTED,
            self.session.state,
            {
                "source_event_start": selected.start,
                "source_event_end": selected.end,
                "source_event_hash": selected.source_event_hash,
                "workspace_revision": request.repository_snapshot.workspace_revision,
                "target_tokens": built.target_after_compression_tokens,
            },
            snapshot_after=self.session.to_snapshot(),
        )
        try:
            result = self.compression_engine.compress(
                request,
                events=events,
                event_range=selected,
            )
        except SummaryValidationError as exc:
            self._commit_event(
                EventType.COMPRESSION_REJECTED,
                self.session.state,
                {
                    "reason": exc.kind,
                    "message": str(exc),
                    "source_event_start": selected.start,
                    "source_event_end": selected.end,
                },
                snapshot_after=self.session.to_snapshot(),
            )
            return None
        except BackendError as exc:
            self._commit_event(
                EventType.COMPRESSION_REJECTED,
                self.session.state,
                {
                    "reason": exc.kind,
                    "message": str(exc),
                    "source_event_start": selected.start,
                    "source_event_end": selected.end,
                },
                snapshot_after=self.session.to_snapshot(),
            )
            return None
        except Exception as exc:
            self._commit_event(
                EventType.COMPRESSION_REJECTED,
                self.session.state,
                {
                    "reason": "compression_exception",
                    "message": f"{type(exc).__name__}: {exc}",
                    "source_event_start": selected.start,
                    "source_event_end": selected.end,
                },
                snapshot_after=self.session.to_snapshot(),
            )
            return None
        payload = {
            "summary_id": result.summary.summary_id,
            "schema_version": result.summary.schema_version,
            "source_event_start": selected.start,
            "source_event_end": selected.end,
            "source_event_hash": selected.source_event_hash,
            "workspace_revision": request.repository_snapshot.workspace_revision,
            "usage": result.response.usage.to_dict(),
        }
        self._commit_event(
            EventType.COMPRESSION_FINISHED,
            self.session.state,
            payload,
            snapshot_after=self.session.to_snapshot(),
            summary=SummaryMutation(
                record=result.summary,
                supersedes=(latest.summary_id if latest is not None else None),
            ),
        )
        self._latest_summary = result.summary
        return result.summary

    def _events(self) -> list:
        if self.recorder.journal is not None:
            return self.recorder.journal.list_events(self.session.id)
        return self.recorder.store.load(self.session.id)

    def _read_paths(self) -> tuple[str, ...]:
        paths: list[str] = []
        for message in self.session.messages:
            if message.role != "tool":
                continue
            try:
                payload = json.loads(message.content)
            except (TypeError, ValueError):
                continue
            data = payload.get("data") if isinstance(payload, Mapping) else None
            if isinstance(data, Mapping) and data.get("path") is not None:
                paths.append(str(data["path"]))
        return tuple(dict.fromkeys(paths))

    def _last_test_fact(self) -> TestFact | None:
        for message in reversed(self.session.messages):
            if message.role != "tool":
                continue
            try:
                payload = json.loads(message.content)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, Mapping) or payload.get("tool_name") != "restricted_test":
                continue
            data = payload.get("data")
            if not isinstance(data, Mapping):
                continue
            passed = data.get("passed")
            if passed is not None and not isinstance(passed, bool):
                continue
            return TestFact(
                profile=str(data.get("profile", "unknown")),
                passed=passed,
                summary=str(data.get("stdout", ""))[:1000],
                stderr_summary=str(data.get("stderr", ""))[:1000],
            )
        return None

    def _on_calling_model(self) -> None:
        existing = self._model_call_row(self.session.active_call_id)
        if existing is not None and existing.get("response") is not None:
            self._consume_model_response(ModelResponse.from_dict(existing["response"]))
            return

        if existing is not None and str(existing.get("status")) == "running":
            request_payload = existing.get("request")
            if not isinstance(request_payload, Mapping):
                raise InvariantViolation("persisted model request is not an object")
            uncertain = BackendError(
                "previous model call ended without a persisted response",
                kind="provider_unavailable",
            )
            self._commit_event(
                EventType.MODEL_CALL_UNCERTAIN,
                self.session.state,
                {
                    "request_id": existing["request_id"],
                    "attempt": int(existing["attempt"]),
                    "kind": "missing_response",
                },
                snapshot_after=self.session.to_snapshot(),
                model_call=ModelCallMutation(
                    request_id=str(existing["request_id"]),
                    ordinal=int(existing["ordinal"]),
                    attempt=int(existing["attempt"]),
                    backend=str(existing["backend"]),
                    status="uncertain",
                    request=dict(request_payload),
                    error={"kind": "missing_response", "message": str(uncertain)},
                ),
            )
            if self._can_retry(uncertain):
                self._schedule_retry(uncertain)
            else:
                self._fail("model_call_uncertain", str(uncertain))
            return

        if self.session.model_calls >= self.session.policy.max_model_calls:
            self._fail("model_budget_exhausted", "maximum model calls exceeded")
            return

        request_id = self.session.active_call_id
        if self.session.active_call_kind not in (None, "model"):
            raise InvariantViolation("active call kind is not a model call")
        if request_id is None:
            request_id = f"{self.session.id}:model:{self._next_model_ordinal()}"
        ordinal = int(existing["ordinal"]) if existing is not None else self._next_model_ordinal()
        attempt = int(existing.get("attempt", 0)) + 1 if existing is not None else 1
        request = ModelRequest(
            request_id=request_id,
            messages=self._model_input,
            tools=tuple(
                self.registry.schemas_for(self.session.policy.allowed_permissions)
            ),
            max_output_tokens=self.session.policy.max_output_tokens,
            metadata={"session_id": self.session.id},
        )
        next_model_calls = self.session.model_calls + 1
        self._commit_event(
            EventType.MODEL_CALL_STARTED,
            self.session.state,
            {
                "request_id": request_id,
                "backend": self.backend.name,
                "message_count": len(request.messages),
                "tool_count": len(request.tools),
                "attempt": attempt,
            },
            snapshot_after=replace(
                self.session.to_snapshot(),
                model_calls=next_model_calls,
                active_call_id=request_id,
                active_call_kind="model",
            ),
            model_call=ModelCallMutation(
                request_id=request_id,
                ordinal=ordinal,
                attempt=attempt,
                backend=self.backend.name,
                status="running",
                request=request.to_dict(),
            ),
        )
        self._fault("after_model_start")
        try:
            response = self.backend.complete(request)
        except BackendError as exc:
            self._commit_event(
                EventType.MODEL_CALL_FAILED,
                self.session.state,
                {
                    "request_id": request_id,
                    "kind": exc.kind,
                    "message": str(exc),
                    "retryable": exc.retryable,
                    "retry_after": exc.retry_after,
                    "provider_metadata": exc.provider_metadata,
                },
                snapshot_after=self.session.to_snapshot(),
                model_call=ModelCallMutation(
                    request_id=request_id,
                    ordinal=ordinal,
                    attempt=attempt,
                    backend=self.backend.name,
                    status="failed",
                    request=request.to_dict(),
                    error={
                        "kind": exc.kind,
                        "message": str(exc),
                        "retry_after": exc.retry_after,
                        "provider_metadata": exc.provider_metadata,
                    },
                ),
            )
            if self._can_retry(exc):
                self._schedule_retry(exc)
            else:
                self._fail(exc.kind, str(exc))
            return

        # Persist the normalized response before consuming tool calls or final text.
        self._commit_event(
            EventType.MODEL_CALL_SUCCEEDED,
            self.session.state,
            {
                "request_id": request_id,
                "finish_reason": response.finish_reason,
                "tool_call_count": len(response.tool_calls),
                "usage": response.usage.to_dict(),
                "provider_metadata": response.provider_metadata,
            },
            snapshot_after=self.session.to_snapshot(),
            model_call=ModelCallMutation(
                request_id=request_id,
                ordinal=ordinal,
                attempt=attempt,
                backend=self.backend.name,
                status="succeeded",
                request=request.to_dict(),
                response=response.to_dict(),
            ),
        )
        self._fault("after_model_response")
        if self._interrupt_requested():
            self._interrupt()
            return
        self._consume_model_response(response)

    def _consume_model_response(self, response: ModelResponse) -> None:
        request_id = self.session.active_call_id
        already_recorded = any(
            message.role == "assistant"
            and message.metadata.get("request_id") == request_id
            for message in self.session.messages
        )
        duplicate_ids = [
            call.id for call in response.tool_calls if call.id in self._seen_tool_call_ids
        ]
        response_ids = [call.id for call in response.tool_calls]
        # A crash can happen after MESSAGE_ADDED committed but before the
        # state transition.  In that case this is the same persisted model
        # response, so its tool-call ids are expected to be in the seen set.
        if len(response_ids) != len(set(response_ids)) or (
            duplicate_ids and not already_recorded
        ):
            self._fail("duplicate_tool_call_id", "tool call ids must be unique")
            return

        assistant = Message(
            role="assistant",
            content=response.text,
            metadata={
                "request_id": request_id,
                "tool_calls": [call.to_dict() for call in response.tool_calls],
                "finish_reason": response.finish_reason,
                "usage": response.usage.to_dict(),
            },
        )
        if not already_recorded:
            self._append_message(
                assistant,
                snapshot_after=replace(
                    self.session.to_snapshot(),
                    active_call_id=request_id,
                    active_call_kind="model",
                ),
            )
            self._fault("after_model_message")

        if response.tool_calls:
            self._seen_tool_call_ids.update(response_ids)
            candidate = replace(
                self.session.to_snapshot(),
                pending_tool_calls=(
                    tuple(self.session.pending_tool_calls) + tuple(response.tool_calls)
                ),
                state=RuntimeState.DISPATCHING_TOOL,
                active_call_id=None,
                active_call_kind=None,
                retry_metadata={},
            )
            self.machine.transition(
                self.session,
                RuntimeState.DISPATCHING_TOOL,
                reason="model_requested_tools",
                payload={"count": len(response.tool_calls)},
                snapshot_after=candidate,
            )
            return
        if not response.text.strip():
            self._fail("empty_model_response", "model returned neither text nor tool calls")
            return
        if not self._source_is_unchanged():
            self._fail(
                "source_repository_modified",
                "source repository fingerprint changed during isolated execution",
            )
            return
        candidate = replace(
            self.session.to_snapshot(),
            state=RuntimeState.COMPLETED,
            final_answer=response.text,
            active_call_id=None,
            active_call_kind=None,
            retry_metadata={},
        )
        self.machine.transition(
            self.session,
            RuntimeState.COMPLETED,
            reason="model_returned_final_answer",
            snapshot_after=candidate,
        )
        self._emit_run_finished()

    def _on_dispatching_tool(self) -> None:
        if self.session.tool_calls >= self.session.policy.max_tool_calls:
            self._fail("tool_budget_exhausted", "maximum tool calls exceeded")
            return
        if self.session.workspace_path is None:
            raise InvariantViolation("tool dispatch requires a workspace")

        if self.recorder.journal is None:
            if not self.session.pending_tool_calls:
                raise InvariantViolation("dispatch state has no pending tool call")
            call = self.session.pending_tool_calls[0]
            next_tool_calls = self.session.tool_calls + 1
            self._commit_event(
                EventType.TOOL_CALL_STARTED,
                self.session.state,
                {"call": call.to_dict(), "ordinal": next_tool_calls},
                snapshot_after=replace(
                    self.session.to_snapshot(),
                    pending_tool_calls=tuple(self.session.pending_tool_calls[1:]),
                    tool_calls=next_tool_calls,
                ),
            )
            result = self.harness.execute(
                call,
                self._tool_context(
                    execution_id=(
                        self._sandbox_execution_id(next_tool_calls, 1)
                        if self._uses_sandbox(call.name)
                        else None
                    )
                ),
            )
            self._commit_event(
                EventType.TOOL_CALL_FINISHED,
                self.session.state,
                {"result": result.to_dict()},
                snapshot_after=replace(
                    self.session.to_snapshot(), active_tool_result=result
                ),
            )
            self.machine.transition(
                self.session,
                RuntimeState.RECORDING_OBSERVATION,
                reason="tool_result_available",
                payload={"call_id": result.call_id, "status": result.status.value},
            )
            return

        call = self._active_tool_call()
        if call is None:
            if not self.session.pending_tool_calls:
                raise InvariantViolation("dispatch state has no pending tool call")
            call = self.session.pending_tool_calls[0]
            next_tool_calls = self.session.tool_calls + 1
            context = self._tool_context()
            preparation = self.harness.prepare(call, context)
            candidate = replace(
                self.session.to_snapshot(),
                pending_tool_calls=tuple(self.session.pending_tool_calls[1:]),
                tool_calls=next_tool_calls,
                active_call_id=call.id,
                active_call_kind="tool",
            )
            self._commit_event(
                EventType.TOOL_CALL_PREPARED,
                self.session.state,
                {
                    "call": call.to_dict(),
                    "ordinal": next_tool_calls,
                    "recovery_mode": preparation.recovery_mode.value,
                    "pre_revision": preparation.pre_revision,
                    "planned_post_revision": preparation.planned_post_revision,
                },
                snapshot_after=candidate,
                tool_call=ToolCallMutation(
                    call_id=call.id,
                    ordinal=next_tool_calls,
                    tool_name=call.name,
                    arguments=call.arguments,
                    recovery_mode=preparation.recovery_mode,
                    status=(
                        ToolCallState.FAILED
                        if preparation.error is not None
                        else ToolCallState.PREPARED
                    ),
                    pre_revision=preparation.pre_revision,
                    planned_post_revision=preparation.planned_post_revision,
                    result=(preparation.error.to_dict() if preparation.error else None),
                    error=preparation.error.error if preparation.error else None,
                ),
            )
            self._fault("after_tool_prepare")
            if preparation.error is not None:
                self._finish_tool(preparation.error, attempt=1)
                return
            self._execute_tool_attempt(call, attempt=1, recovery="initial")
            return

        row = self._tool_call_row(call.id)
        if row is None:
            raise InvariantViolation(f"active tool call is missing from journal: {call.id}")
        if row.get("result") is not None:
            self._attach_tool_result(call, ToolResult.from_dict(row["result"]))
            return
        status = str(row["status"])
        if status == ToolCallState.PREPARED.value:
            self._execute_tool_attempt(call, attempt=int(row["attempt"]), recovery="prepared")
        elif status in {ToolCallState.RUNNING.value, ToolCallState.UNCERTAIN.value}:
            self._recover_running_tool(call, row)
        else:
            raise InvariantViolation(f"unsupported persisted tool status: {status}")

    def _tool_context(self, *, execution_id: str | None = None) -> ToolContext:
        return ToolContext(
            workspace=self.workspace_manager.get(self.session.id),
            allowed_permissions=self.session.policy.allowed_permissions,
            execution_id=execution_id,
        )

    def _sandbox_execution_id(self, ordinal: int, attempt: int) -> str:
        return f"{self.session.id}:sandbox:{ordinal}:{attempt}"

    @staticmethod
    def _uses_sandbox(tool_name: str) -> bool:
        return tool_name in {"restricted_test", "run_command"}

    def _active_tool_call(self) -> ToolCall | None:
        if self.session.active_call_kind != "tool" or self.session.active_call_id is None:
            return None
        row = self._tool_call_row(self.session.active_call_id)
        if row is None:
            return None
        arguments = row.get("arguments")
        if not isinstance(arguments, Mapping):
            raise InvariantViolation("persisted tool arguments must be an object")
        return ToolCall(
            id=str(row["call_id"]),
            name=str(row["tool_name"]),
            arguments=dict(arguments),
        )

    def _execute_tool_attempt(self, call: ToolCall, *, attempt: int, recovery: str) -> None:
        row = self._tool_call_row(call.id)
        if row is None:
            raise InvariantViolation(f"tool journal row not found: {call.id}")
        mode = RecoveryMode(str(row["recovery_mode"]))
        execution_id = (
            self._sandbox_execution_id(int(row["ordinal"]), attempt)
            if self._uses_sandbox(call.name)
            else None
        )
        running_payload = {
            "call": call.to_dict(),
            "ordinal": int(row["ordinal"]),
            "attempt": attempt,
            "recovery": recovery,
        }
        if execution_id is not None:
            running_payload["execution_id"] = execution_id
        self._commit_event(
            EventType.TOOL_CALL_RUNNING,
            self.session.state,
            running_payload,
            snapshot_after=self.session.to_snapshot(),
            tool_call=ToolCallMutation(
                call_id=call.id,
                ordinal=int(row["ordinal"]),
                tool_name=call.name,
                arguments=call.arguments,
                recovery_mode=mode,
                status=ToolCallState.RUNNING,
                attempt=attempt,
                pre_revision=row.get("pre_revision"),
                planned_post_revision=row.get("planned_post_revision"),
            ),
        )
        self._fault("after_tool_running")
        result = self.harness.execute(
            call,
            self._tool_context(execution_id=execution_id),
        )
        self._finish_tool(result, attempt=attempt)

    def _finish_tool(self, result: ToolResult, *, attempt: int) -> None:
        call_id = result.call_id
        row = self._tool_call_row(call_id)
        if row is None:
            raise InvariantViolation(f"tool journal row not found: {call_id}")
        mode = RecoveryMode(str(row["recovery_mode"]))
        self._commit_event(
            EventType.TOOL_CALL_FINISHED,
            self.session.state,
            {"result": result.to_dict(), "attempt": attempt},
            snapshot_after=replace(self.session.to_snapshot(), active_tool_result=result),
            tool_call=ToolCallMutation(
                call_id=call_id,
                ordinal=int(row["ordinal"]),
                tool_name=str(row["tool_name"]),
                arguments=dict(row["arguments"]),
                recovery_mode=mode,
                status=(ToolCallState.SUCCEEDED if result.ok else ToolCallState.FAILED),
                attempt=attempt,
                pre_revision=row.get("pre_revision"),
                planned_post_revision=row.get("planned_post_revision"),
                result=result.to_dict(),
                error=result.error,
            ),
        )
        self._fault("after_tool_result")
        self.machine.transition(
            self.session,
            RuntimeState.RECORDING_OBSERVATION,
            reason="tool_result_available",
            payload={"call_id": result.call_id, "status": result.status.value},
        )

    def _attach_tool_result(self, call: ToolCall, result: ToolResult) -> None:
        if self.session.active_tool_result is None:
            self._commit_event(
                EventType.TOOL_RESULT_REATTACHED,
                self.session.state,
                {"call_id": call.id, "recovered": True},
                snapshot_after=replace(
                    self.session.to_snapshot(), active_tool_result=result
                ),
            )
        if self.session.state is RuntimeState.DISPATCHING_TOOL:
            self.machine.transition(
                self.session,
                RuntimeState.RECORDING_OBSERVATION,
                reason="persisted_tool_result_reused",
                payload={"call_id": call.id},
            )

    def _recover_running_tool(self, call: ToolCall, row: dict[str, object]) -> None:
        mode = RecoveryMode(str(row["recovery_mode"]))
        if mode is RecoveryMode.NON_IDEMPOTENT:
            self._enter_waiting_approval(call, row, None)
            return
        if mode is RecoveryMode.RECONCILABLE_WRITE:
            current = None
            try:
                current = self.harness.current_revision(call, self._tool_context())
            except Exception:
                current = None
            if current == row.get("planned_post_revision"):
                recovered = ToolResult(
                    call_id=call.id,
                    tool_name=call.name,
                    status=ToolStatus.SUCCESS,
                    data={
                        "recovered": True,
                        "reconciliation": "effect_applied",
                        "pre_revision": row.get("pre_revision"),
                        "post_revision": current,
                    },
                )
                self._finish_tool(recovered, attempt=int(row["attempt"]))
                return
            if current != row.get("pre_revision"):
                self._enter_waiting_approval(call, row, current)
                return
        self._execute_tool_attempt(
            call,
            attempt=int(row["attempt"]) + 1,
            recovery="restarted_after_uncertain_attempt",
        )

    def _enter_waiting_approval(
        self, call: ToolCall, row: dict[str, object], current_revision: str | None
    ) -> None:
        candidate = replace(
            self.session.to_snapshot(),
            state=RuntimeState.WAITING_APPROVAL,
            resume_target_state=RuntimeState.DISPATCHING_TOOL,
            retry_metadata={},
        )
        self.machine.transition(
            self.session,
            RuntimeState.WAITING_APPROVAL,
            reason="uncertain_tool_side_effect",
            payload={
                "call_id": call.id,
                "recovery_mode": str(row["recovery_mode"]),
                "pre_revision": row.get("pre_revision"),
                "planned_post_revision": row.get("planned_post_revision"),
                "current_revision": current_revision,
            },
            snapshot_after=candidate,
        )
        self._commit_event(
            EventType.TOOL_CALL_UNCERTAIN,
            self.session.state,
            {
                "call_id": call.id,
                "recovery_mode": str(row["recovery_mode"]),
                "current_revision": current_revision,
            },
            snapshot_after=self.session.to_snapshot(),
            tool_call=ToolCallMutation(
                call_id=call.id,
                ordinal=int(row["ordinal"]),
                tool_name=call.name,
                arguments=dict(row["arguments"]),
                recovery_mode=RecoveryMode(str(row["recovery_mode"])),
                status=ToolCallState.UNCERTAIN,
                attempt=int(row["attempt"]),
                pre_revision=row.get("pre_revision"),
                planned_post_revision=row.get("planned_post_revision"),
                error={
                    "kind": "uncertain_side_effect",
                    "message": "tool effect cannot be reconciled automatically",
                },
            ),
        )
        self._commit_event(
            EventType.APPROVAL_REQUESTED,
            self.session.state,
            {"call_id": call.id, "reason": "uncertain_tool_side_effect"},
            snapshot_after=self.session.to_snapshot(),
        )

    def _model_call_row(self, request_id: str | None) -> dict[str, object] | None:
        if request_id is None or self.recorder.journal is None:
            return None
        return self.recorder.journal.get_model_call(self.session.id, request_id)

    def _tool_call_row(self, call_id: str | None) -> dict[str, object] | None:
        if call_id is None or self.recorder.journal is None:
            return None
        return self.recorder.journal.get_tool_call(self.session.id, call_id)

    def _next_model_ordinal(self) -> int:
        if self.recorder.journal is None:
            return self.session.model_calls + 1
        rows = self.recorder.journal.list_model_calls(self.session.id)
        return max((int(row["ordinal"]) for row in rows), default=0) + 1

    def _can_retry(self, error: BackendError) -> bool:
        if not error.retryable:
            return False
        retry_count = int(self.session.retry_metadata.get("retry_count", 0)) + 1
        if retry_count > self.session.policy.max_retries:
            return False
        if self.session.model_calls >= self.session.policy.max_model_calls:
            return False
        started_at = self.session.retry_metadata.get("retry_started_at")
        if started_at is not None:
            try:
                if self.clock() - float(started_at) >= self.session.policy.max_retry_wait_seconds:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def _schedule_retry(self, error: BackendError) -> None:
        retry_count = int(self.session.retry_metadata.get("retry_count", 0)) + 1
        now = self.clock()
        started_at = self.session.retry_metadata.get("retry_started_at", now)
        try:
            elapsed = now - float(started_at)
        except (TypeError, ValueError):
            elapsed = 0.0
            started_at = now
        if elapsed >= self.session.policy.max_retry_wait_seconds:
            self._fail("retry_budget_exhausted", "maximum retry wait exceeded")
            return
        delay = error.retry_after
        if delay is None:
            delay = bounded_exponential_backoff(
                retry_count,
                base_seconds=self.session.policy.retry_base_delay_seconds,
                max_seconds=self.session.policy.retry_max_delay_seconds,
                jitter_seconds=self.session.policy.retry_jitter_seconds,
                random_source=self.random_source,
            )
            jitter_already_applied = True
        else:
            # Retry-After is an explicit server floor; do not subtract jitter
            # from it or turn a provider-directed wait into an early retry.
            jitter_already_applied = True
        delay = min(float(delay), self.session.policy.retry_max_delay_seconds)
        if self.session.policy.retry_jitter_seconds and not jitter_already_applied:
            delay += (
                (self.random_source() * 2.0) - 1.0
            ) * self.session.policy.retry_jitter_seconds
            delay = max(0.0, delay)
        if elapsed + delay > self.session.policy.max_retry_wait_seconds:
            self._fail("retry_budget_exhausted", "maximum retry wait exceeded")
            return

        fallback_from = self.backend.name
        fallback = None
        fallback_selector = getattr(self.backend, "fallback_for", None)
        if callable(fallback_selector):
            fallback = fallback_selector(error)
            if fallback is not None:
                self.backend = fallback

        metadata = {
            "retry_count": retry_count,
            "retry_started_at": float(started_at),
            "next_retry_at": now + delay,
            "last_error": {
                "kind": error.kind,
                "message": str(error),
                "provider_metadata": dict(error.provider_metadata),
                "retry_after": error.retry_after,
            },
            "backend": self.backend.name,
        }
        if error.provider_metadata:
            metadata["provider_metadata"] = dict(error.provider_metadata)
        if error.retry_after is not None:
            metadata["retry_after"] = float(error.retry_after)
        if fallback is not None:
            metadata["fallback_from"] = fallback_from
            metadata["fallback_to"] = self.backend.name
        candidate = replace(
            self.session.to_snapshot(),
            state=RuntimeState.RETRY_WAIT,
            resume_target_state=RuntimeState.CALLING_MODEL,
            retry_metadata=metadata,
        )
        self.machine.transition(
            self.session,
            RuntimeState.RETRY_WAIT,
            reason="retryable_backend_failure",
            payload={
                "kind": error.kind,
                "retry_count": retry_count,
                "delay_seconds": delay,
                "next_retry_at": now + delay,
                "backend": self.backend.name,
                "provider_metadata": dict(error.provider_metadata),
                "retry_after": error.retry_after,
            },
            snapshot_after=candidate,
        )
        self._commit_event(
            EventType.RETRY_SCHEDULED,
            self.session.state,
            {
                "kind": error.kind,
                "retry_count": retry_count,
                "next_retry_at": now + delay,
                "backend": self.backend.name,
            },
            snapshot_after=self.session.to_snapshot(),
        )
        if fallback is not None:
            self._commit_event(
                EventType.FALLBACK_SELECTED,
                self.session.state,
                {"from": fallback_from, "to": self.backend.name},
                snapshot_after=self.session.to_snapshot(),
            )

    def _on_retry_wait(self) -> None:
        next_retry_at = self.session.retry_metadata.get("next_retry_at")
        if next_retry_at is None:
            raise InvariantViolation("retry_wait state has no next_retry_at")
        try:
            remaining = float(next_retry_at) - self.clock()
        except (TypeError, ValueError) as exc:
            raise InvariantViolation("retry_wait next_retry_at is invalid") from exc
        if remaining > 0:
            self.sleeper(remaining)
            return
        candidate = replace(
            self.session.to_snapshot(),
            state=RuntimeState.CALLING_MODEL,
            resume_target_state=None,
        )
        self.machine.transition(
            self.session,
            RuntimeState.CALLING_MODEL,
            reason="retry_backoff_elapsed",
            snapshot_after=candidate,
        )

    def _on_recording_observation(self) -> None:
        result = self.session.active_tool_result
        if result is None:
            if self._last_observation_call_id() is None:
                raise InvariantViolation("observation state has no tool result")
        else:
            self._append_message(
                Message(
                    role="tool",
                    tool_call_id=result.call_id,
                    content=json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True),
                ),
                snapshot_after=replace(
                    self.session.to_snapshot(),
                    active_tool_result=None,
                    active_call_id=None,
                    active_call_kind=None,
                ),
            )
            self._fault("after_tool_observation")
        if self.session.pending_tool_calls:
            self.machine.transition(
                self.session,
                RuntimeState.DISPATCHING_TOOL,
                reason="more_tool_calls_pending",
            )
        else:
            self.machine.transition(
                self.session,
                RuntimeState.BUILDING_CONTEXT,
                reason="observation_recorded",
            )

    def _last_observation_call_id(self) -> str | None:
        for message in reversed(self.session.messages):
            if message.role == "tool" and message.tool_call_id:
                return message.tool_call_id
        return None

    def _append_message(
        self,
        message: Message,
        *,
        snapshot_after: RuntimeSnapshot | None = None,
    ) -> None:
        self._commit_event(
            EventType.MESSAGE_ADDED,
            self.session.state,
            {"message": message.to_dict(), "message_index": len(self.session.messages)},
            snapshot_after=snapshot_after or self.session.to_snapshot(),
            message_to_append=message,
        )
        self.session.messages.append(message)

    def _fail(
        self,
        kind: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        failure = {"kind": kind, "message": message}
        if details:
            failure["details"] = dict(details)
        candidate = replace(
            self.session.to_snapshot(),
            state=RuntimeState.FAILED,
            failure=failure,
            active_call_id=None,
            active_call_kind=None,
            active_tool_result=None,
            retry_metadata={},
            resume_target_state=None,
        )
        self.machine.transition(
            self.session,
            RuntimeState.FAILED,
            reason=kind,
            payload={"error": failure},
            snapshot_after=candidate,
        )
        self._emit_run_finished()

    def _source_is_unchanged(self) -> bool:
        return tree_fingerprint(self.session.source_path) == self.session.source_fingerprint

    def _emit_run_finished(self) -> None:
        self._commit_event(
            EventType.RUN_FINISHED,
            self.session.state,
            {
                "final_state": self.session.state.value,
                "steps": self.session.step_count,
                "model_calls": self.session.model_calls,
                "tool_calls": self.session.tool_calls,
                "source_unchanged": self._source_is_unchanged(),
                "failure": self.session.failure,
            },
            snapshot_after=self.session.to_snapshot(),
        )

    def _commit_event(
        self,
        event_type: EventType,
        state: RuntimeState,
        payload: dict[str, object],
        *,
        snapshot_after: RuntimeSnapshot,
        message_to_append: Message | None = None,
        expected_state: RuntimeState | None = None,
        model_call: ModelCallMutation | None = None,
        tool_call: ToolCallMutation | None = None,
        summary: SummaryMutation | None = None,
        clear_interrupt: bool = False,
    ) -> None:
        event = self.recorder.emit(
            event_type,
            state,
            payload,
            snapshot_after=snapshot_after,
            expected_state=expected_state,
            message_to_append=message_to_append,
            model_call=model_call,
            tool_call=tool_call,
            summary=summary,
            clear_interrupt=clear_interrupt,
        )
        apply_snapshot(
            self.session,
            replace(
                snapshot_after,
                version=self.recorder.current_version,
                updated_at=event.timestamp,
            ),
        )


def _assistant_tool_calls(message: Message) -> list[object]:
    raw = message.metadata.get("tool_calls", [])
    return raw if isinstance(raw, list) else []
