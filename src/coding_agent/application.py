from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
import signal
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from coding_agent.command_profiles import CommandProfileRegistry, default_command_profiles
from coding_agent.compression import CompressionEngine
from coding_agent.context import BudgetedContextBuilder, ContextBuilder
from coding_agent.domain import (
    EventType,
    InvariantViolation,
    RecoveryMode,
    RunPolicy,
    RunResult,
    RuntimeSnapshot,
    RuntimeState,
    Session,
    ToolCallState,
    ToolResult,
    redact_sensitive_text,
)
from coding_agent.export import export_trace
from coding_agent.models.base import ModelBackend
from coding_agent.persistence import (
    ResumeRejected,
    RunJournal,
    SQLiteRunJournal,
    ToolCallMutation,
)
from coding_agent.runtime import AgentRuntime
from coding_agent.sandbox.base import SandboxExecutor
from coding_agent.sandbox.local_container import build_default_sandbox_executor
from coding_agent.sandbox.policy import SandboxPolicy
from coding_agent.test_profiles import TestProfileRegistry, default_test_profiles
from coding_agent.tools.builtin import build_builtin_registry
from coding_agent.tools.harness import ToolHarness
from coding_agent.trajectory import EventStore, JsonlEventStore, ReplayResult, TrajectoryRecorder, replay
from coding_agent.workspace import WorkspaceManager, tree_fingerprint


class AgentApplication:
    """Composition root for the M1 vertical slice."""

    def __init__(
        self,
        agent_home: str | Path,
        *,
        test_profiles: TestProfileRegistry | None = None,
        command_profiles: CommandProfileRegistry | None = None,
        context_builder: ContextBuilder | None = None,
        compression_engine: CompressionEngine | None = None,
        max_compression_calls: int = 1,
        sandbox_executor: SandboxExecutor | None = None,
        sandbox_policy: SandboxPolicy | None = None,
        event_store: EventStore | None = None,
        journal: RunJournal | None = None,
    ):
        self.agent_home = Path(agent_home).resolve()
        self.agent_home.mkdir(parents=True, exist_ok=True)
        self.test_profiles = test_profiles or default_test_profiles()
        self.command_profiles = command_profiles or default_command_profiles()
        self.context_builder = context_builder or BudgetedContextBuilder()
        self.compression_engine = compression_engine
        if max_compression_calls < 0:
            raise ValueError("max_compression_calls cannot be negative")
        self.max_compression_calls = max_compression_calls
        self.sandbox_executor = sandbox_executor or build_default_sandbox_executor()
        self.sandbox_policy = sandbox_policy or SandboxPolicy()
        if journal is None and event_store is not None and all(
            hasattr(event_store, attribute)
            for attribute in ("commit", "create_session", "session_version")
        ):
            journal = event_store  # type: ignore[assignment]
        if journal is None and event_store is None:
            journal = SQLiteRunJournal(self.agent_home / "state.db")
        self.journal = journal
        self.event_store = event_store or journal or JsonlEventStore(self.agent_home / "traces")
        self.workspace_manager = WorkspaceManager(self.agent_home)
        self.registry = build_builtin_registry(
            self.test_profiles,
            command_profiles=self.command_profiles,
            sandbox_executor=self.sandbox_executor,
            sandbox_policy=self.sandbox_policy,
        )
        self.harness = ToolHarness(self.registry)

    def run_task(
        self,
        *,
        source: str | Path,
        task: str,
        backend: ModelBackend,
        policy: RunPolicy | None = None,
        session_id: str | None = None,
        fault_injector: Callable[[str], None] | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        random_source: Callable[[], float] | None = None,
    ) -> RunResult:
        source_path = Path(source).resolve(strict=True)
        if not source_path.is_dir():
            raise ValueError("source must be a directory")
        self._validate_non_overlapping_paths(source_path)
        if not task.strip():
            raise ValueError("task cannot be empty")

        session = Session(
            id=session_id or str(uuid.uuid4()),
            task=task,
            source_path=str(source_path),
            state=RuntimeState.CREATED,
            policy=policy or RunPolicy(),
            source_fingerprint=tree_fingerprint(source_path),
        )
        owner = f"run:{session.id}:{uuid.uuid4()}"
        recorder = TrajectoryRecorder(
            self.event_store,
            session.id,
            journal=self.journal,
            snapshot_provider=session.to_snapshot,
            lease_owner=owner,
        )
        runtime = AgentRuntime(
            session=session,
            backend=backend,
            context_builder=self.context_builder,
            registry=self.registry,
            harness=self.harness,
            workspace_manager=self.workspace_manager,
            recorder=recorder,
            compression_engine=self.compression_engine,
            max_compression_calls=self.max_compression_calls,
            lease_owner=owner,
            fault_injector=fault_injector,
            clock=clock,
            sleeper=sleeper,
            random_source=random_source,
        )
        runtime.initialize()
        owner = runtime.lease_owner
        if self.journal is not None and owner is not None:
            self.journal.acquire_lease(session.id, owner, lease_seconds=runtime.lease_seconds)
        try:
            with self._interrupt_signal_handler(session.id):
                result = runtime.run()
            self._export_after_run(result)
            return result
        finally:
            if self.journal is not None and owner is not None:
                self.journal.release_lease(session.id, owner)

    def resume_session(
        self,
        session_id: str,
        *,
        backend: ModelBackend,
        lease_owner: str | None = None,
        lease_seconds: float = 60.0,
        clock: Any | None = None,
        sleeper: Any | None = None,
        random_source: Any | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ) -> RunResult:
        if self.journal is None:
            raise ResumeRejected("resume requires the SQLite journal")
        session = self.journal.load_session(session_id)
        self._validate_resume(session)
        owner = lease_owner or f"resume:{session_id}:{uuid.uuid4()}"
        self.journal.acquire_lease(session_id, owner, lease_seconds=lease_seconds)
        try:
            self._restore_backend(backend, session_id)
            recorder = TrajectoryRecorder(
                self.event_store,
                session.id,
                journal=self.journal,
                snapshot_provider=session.to_snapshot,
                lease_owner=owner,
            )
            runtime = AgentRuntime(
                session=session,
                backend=backend,
                context_builder=self.context_builder,
                registry=self.registry,
                harness=self.harness,
                workspace_manager=self.workspace_manager,
                recorder=recorder,
                compression_engine=self.compression_engine,
                max_compression_calls=self.max_compression_calls,
                lease_owner=owner,
                clock=clock,
                sleeper=sleeper,
                random_source=random_source,
                lease_seconds=lease_seconds,
                fault_injector=fault_injector,
            )
            runtime.resume()
            with self._interrupt_signal_handler(session.id):
                result = runtime.run()
            self._export_after_run(result)
            return result
        finally:
            self.journal.release_lease(session_id, owner)

    def interrupt_session(self, session_id: str) -> str:
        if self.journal is None:
            raise ResumeRejected("interrupt requires the SQLite journal")
        return self.journal.request_interrupt(session_id)

    def resolve_call(
        self,
        session_id: str,
        call_id: str,
        resolution: str,
        *,
        actor: str = "local-operator",
        reason: str = "operator_resolution",
        result: ToolResult | Mapping[str, Any] | None = None,
        lease_owner: str | None = None,
        lease_seconds: float = 60.0,
    ) -> RuntimeSnapshot:
        """Persist an explicit decision for an uncertain tool side effect."""

        if self.journal is None:
            raise ResumeRejected("call resolution requires the SQLite journal")
        if resolution not in {"effect-not-applied", "effect-applied", "abort"}:
            raise ValueError("resolution must be effect-not-applied, effect-applied, or abort")
        session = self.journal.load_session(session_id)
        if session.state is not RuntimeState.WAITING_APPROVAL:
            raise InvariantViolation("call resolution requires WAITING_APPROVAL state")
        if session.active_call_id != call_id:
            raise InvariantViolation("call resolution does not match the active call")
        row = self.journal.get_tool_call(session_id, call_id)
        if row is None:
            raise InvariantViolation("uncertain call is missing from the tool journal")
        resolved_result = (
            self._coerce_tool_result(result, call_id, row)
            if resolution == "effect-applied"
            else None
        )
        safe_actor = redact_sensitive_text(actor)
        safe_reason = redact_sensitive_text(reason)
        owner = lease_owner or f"resolve:{session_id}:{uuid.uuid4()}"
        self.journal.acquire_lease(session_id, owner, lease_seconds=lease_seconds)
        try:
            recorder = TrajectoryRecorder(
                self.event_store,
                session.id,
                journal=self.journal,
                snapshot_provider=session.to_snapshot,
                lease_owner=owner,
            )
            resolution_payload: dict[str, object] = {
                "call_id": call_id,
                "resolution": resolution,
                "actor": safe_actor,
                "reason": safe_reason,
            }
            recorder.emit(
                EventType.CALL_RESOLVED,
                session.state,
                resolution_payload,
                snapshot_after=session.to_snapshot(),
            )
            session.version = recorder.current_version
            if resolution == "effect-applied":
                assert resolved_result is not None
                target = RuntimeState.RECORDING_OBSERVATION
                snapshot_after = replace(
                    session.to_snapshot(),
                    state=target,
                    active_tool_result=resolved_result,
                    resume_target_state=None,
                )
                tool_status = ToolCallState.SUCCEEDED if resolved_result.ok else ToolCallState.FAILED
                tool_mutation = ToolCallMutation(
                    call_id=call_id,
                    ordinal=int(row["ordinal"]),
                    tool_name=str(row["tool_name"]),
                    arguments=dict(row["arguments"]),
                    recovery_mode=RecoveryMode(str(row["recovery_mode"])),
                    status=tool_status,
                    attempt=int(row["attempt"]),
                    pre_revision=row.get("pre_revision"),
                    planned_post_revision=row.get("planned_post_revision"),
                    result=resolved_result.to_dict(),
                    error=resolved_result.error,
                )
            elif resolution == "effect-not-applied":
                target = RuntimeState.DISPATCHING_TOOL
                snapshot_after = replace(
                    session.to_snapshot(),
                    state=target,
                    active_tool_result=None,
                    resume_target_state=None,
                )
                tool_mutation = ToolCallMutation(
                    call_id=call_id,
                    ordinal=int(row["ordinal"]),
                    tool_name=str(row["tool_name"]),
                    arguments=dict(row["arguments"]),
                    recovery_mode=RecoveryMode(str(row["recovery_mode"])),
                    status=ToolCallState.PREPARED,
                    attempt=int(row["attempt"]) + 1,
                    pre_revision=row.get("pre_revision"),
                    planned_post_revision=row.get("planned_post_revision"),
                )
            else:
                target = RuntimeState.FAILED
                snapshot_after = replace(
                    session.to_snapshot(),
                    state=target,
                    failure={
                        "kind": "uncertain_side_effect_aborted",
                        "message": "operator aborted an uncertain tool side effect",
                        "call_id": call_id,
                        "actor": safe_actor,
                    },
                    active_call_id=None,
                    active_call_kind=None,
                    active_tool_result=None,
                    resume_target_state=None,
                )
                tool_mutation = None
            event_payload = {
                **resolution_payload,
                "from": RuntimeState.WAITING_APPROVAL.value,
                "to": target.value,
            }
            recorder.emit(
                EventType.STATE_TRANSITION,
                target,
                event_payload,
                snapshot_after=snapshot_after,
                expected_state=RuntimeState.WAITING_APPROVAL,
                tool_call=tool_mutation,
            )
            committed = self.journal.load_snapshot(session_id)
            if target is RuntimeState.FAILED:
                try:
                    source_unchanged = (
                        tree_fingerprint(committed.source_path)
                        == committed.source_fingerprint
                    )
                except (OSError, ValueError):
                    source_unchanged = False
                recorder.emit(
                    EventType.RUN_FINISHED,
                    target,
                    {
                        "final_state": target.value,
                        "steps": committed.step_count,
                        "model_calls": committed.model_calls,
                        "tool_calls": committed.tool_calls,
                        "source_unchanged": source_unchanged,
                        "failure": committed.failure,
                    },
                    snapshot_after=committed,
                )
            return self.journal.load_snapshot(session_id)
        finally:
            self.journal.release_lease(session_id, owner)

    def list_sessions(self) -> list[dict[str, object]]:
        if self.journal is None:
            return []
        return self.journal.list_sessions()

    def show_session(self, session_id: str) -> dict[str, object]:
        if self.journal is None:
            raise ResumeRejected("session inspection requires the SQLite journal")
        snapshot = self.journal.load_snapshot(session_id)
        return {
            **snapshot.to_dict(),
            "message_count": len(self.journal.list_messages(session_id)),
            "model_call_journal": self.journal.list_model_calls(session_id),
            "tool_call_journal": self.journal.list_tool_calls(session_id),
        }

    def replay_session(self, session_id: str) -> ReplayResult:
        events = (
            self.journal.list_events(session_id)
            if self.journal is not None
            else self.event_store.load(session_id)
        )
        return replay(events)

    def export_trace(self, session_id: str, destination: str | Path | None = None) -> Path:
        if self.journal is None:
            if destination is not None:
                raise ValueError("a JSONL compatibility store already owns its trace path")
            return self.event_store.trace_path(session_id)
        return export_trace(self.journal, session_id, destination)

    def close(self) -> None:
        if isinstance(self.journal, SQLiteRunJournal):
            self.journal.close()

    def _export_after_run(self, result: RunResult) -> None:
        if self.journal is not None:
            try:
                export_trace(self.journal, result.session_id, result.trace_path)
            except OSError:
                # SQLite remains authoritative; a later export-trace can rebuild it.
                pass

    @contextmanager
    def _interrupt_signal_handler(self, session_id: str):
        if self.journal is None or threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = signal.getsignal(signal.SIGINT)

        def handle_interrupt(signum: int, frame: object) -> None:
            del signum, frame
            self.journal.request_interrupt(session_id)

        signal.signal(signal.SIGINT, handle_interrupt)
        try:
            yield
        finally:
            signal.signal(signal.SIGINT, previous)

    def _restore_backend(self, backend: ModelBackend, session_id: str) -> None:
        if self.journal is None:
            return
        snapshot = self.journal.load_snapshot(session_id)
        restore_state = getattr(backend, "restore_state", None)
        if callable(restore_state):
            restore_state(dict(snapshot.retry_metadata))
        restore = getattr(backend, "restore", None)
        if callable(restore):
            restore(self.journal.completed_model_call_count(session_id))
        restore_session = getattr(backend, "restore_session", None)
        if callable(restore_session):
            restore_session(self.journal.list_model_calls(session_id))

    def _validate_resume(self, session: Session) -> None:
        if session.state in {RuntimeState.COMPLETED, RuntimeState.FAILED}:
            raise ResumeRejected("terminal session cannot be resumed")
        source = Path(session.source_path)
        if not source.is_dir():
            raise ResumeRejected("source repository is missing; refusing to resume")
        if tree_fingerprint(source) != session.source_fingerprint:
            raise ResumeRejected("source repository fingerprint changed; refusing to resume")
        needs_workspace = session.state not in {
            RuntimeState.CREATED,
            RuntimeState.PREPARING_WORKSPACE,
        }
        if needs_workspace:
            if not session.workspace_path or not Path(session.workspace_path).is_dir():
                raise ResumeRejected("isolated workspace is missing; refusing to resume")
            expected = (self.workspace_manager.workspaces_root / session.id / "repo").resolve()
            if Path(session.workspace_path).resolve() != expected:
                raise ResumeRejected("persisted workspace path is outside the session workspace")

    @staticmethod
    def _coerce_tool_result(
        result: ToolResult | Mapping[str, Any] | None,
        call_id: str,
        row: Mapping[str, Any],
    ) -> ToolResult:
        if isinstance(result, ToolResult):
            resolved = result
        elif isinstance(result, Mapping):
            resolved = ToolResult.from_dict(result)
        else:
            raise ValueError("effect-applied requires a persisted tool result")
        if resolved.call_id != call_id or resolved.tool_name != str(row["tool_name"]):
            raise ValueError("resolved result does not match the uncertain call")
        return resolved

    def _validate_non_overlapping_paths(self, source: Path) -> None:
        try:
            self.agent_home.relative_to(source)
        except ValueError:
            pass
        else:
            raise ValueError("agent_home must not be inside the source repository")
        try:
            source.relative_to(self.agent_home)
        except ValueError:
            pass
        else:
            raise ValueError("source repository must not be inside agent_home")
