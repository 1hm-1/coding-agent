from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Iterator, Mapping, Protocol

from coding_agent.domain import (
    Event,
    EventType,
    InvariantViolation,
    JsonObject,
    Message,
    RecoveryMode,
    RunPolicy,
    RuntimeSnapshot,
    RuntimeState,
    RuntimeStateMachine,
    Session,
    SummaryRecord,
    TERMINAL_STATES,
    ToolCallState,
)
from coding_agent.migrations import LATEST_SCHEMA_VERSION, MigrationRunner
from coding_agent.product_domain import (
    Conversation,
    InstructionManifest,
    PolicyEpoch,
    ProjectScope,
    RepositoryDescriptor,
    RepositoryIdentity,
    RuntimeExecution,
    Turn,
    TurnAdmission,
    TurnStartCodeCheckpoint,
    WorkspaceBinding,
    WorkspaceWriterClaim,
)


EVENT_SCHEMA_VERSION = 1
SCHEMA_VERSION = LATEST_SCHEMA_VERSION


class PersistenceError(RuntimeError):
    """Base class for failures while reading or writing the journal."""


class SessionNotFound(PersistenceError, LookupError):
    pass


class LeaseConflict(PersistenceError):
    """Another owner currently has the right to advance a session."""


class ResumeRejected(PersistenceError):
    """A session cannot be resumed without violating a persisted invariant."""


class ProductAdmissionConflict(PersistenceError):
    """An operation id or aggregate precondition cannot be safely reused."""


class ProductLifecycleConflict(PersistenceError):
    """A Conversation lifecycle precondition is no longer true."""


class DirectTreeReadOnlyViolation(PersistenceError):
    """M2 direct working-tree composition rejected a side effect."""


class JournalConflict(InvariantViolation):
    """An optimistic state/version precondition no longer holds."""

    def __init__(
        self,
        session_id: str,
        *,
        expected_state: RuntimeState,
        actual_state: RuntimeState,
        expected_version: int,
        actual_version: int,
    ):
        self.session_id = session_id
        self.expected_state = expected_state
        self.actual_state = actual_state
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"journal precondition failed for {session_id}: "
            f"expected {expected_state.value}/v{expected_version}, "
            f"found {actual_state.value}/v{actual_version}"
        )


VersionConflict = JournalConflict
OptimisticConcurrencyError = JournalConflict


@dataclass(frozen=True)
class JournalMutation:
    session_id: str
    expected_version: int
    expected_state: RuntimeState
    snapshot_after: RuntimeSnapshot
    event_type: EventType
    payload: JsonObject
    message_to_append: Message | None = None
    model_call: "ModelCallMutation | None" = None
    tool_call: "ToolCallMutation | None" = None
    summary: "SummaryMutation | None" = None
    clear_interrupt: bool = False
    lease_owner: str | None = None


@dataclass(frozen=True)
class ModelCallMutation:
    request_id: str
    ordinal: int
    backend: str
    status: str
    request: JsonObject
    attempt: int = 1
    response: JsonObject | None = None
    error: JsonObject | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class ToolCallMutation:
    call_id: str
    ordinal: int
    tool_name: str
    arguments: JsonObject
    recovery_mode: RecoveryMode
    status: ToolCallState
    attempt: int = 1
    pre_revision: str | None = None
    planned_post_revision: str | None = None
    result: JsonObject | None = None
    error: JsonObject | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class SummaryMutation:
    record: SummaryRecord
    supersedes: str | None = None


@dataclass(frozen=True)
class CommitResult:
    event: Event
    committed_version: int


class RunJournal(Protocol):
    def create_session(
        self,
        snapshot: RuntimeSnapshot,
        initial_message: Message,
        *,
        session_created_payload: JsonObject | None = None,
    ) -> tuple[Event, ...]:
        ...

    def commit(self, mutation: JournalMutation) -> CommitResult:
        ...

    def load_snapshot(self, session_id: str) -> RuntimeSnapshot:
        ...

    def load_session(self, session_id: str) -> Session:
        ...

    def list_sessions(self) -> list[dict[str, object]]:
        ...

    def load(self, session_id: str) -> list[Event]:
        ...

    def append(self, event: Event) -> None:
        ...

    def list_messages(self, session_id: str) -> list[Message]:
        ...

    def list_events(self, session_id: str) -> list[Event]:
        ...

    def session_version(self, session_id: str) -> int:
        ...

    def last_event_sequence(self, session_id: str) -> int:
        ...

    def trace_path(self, session_id: str) -> Path:
        ...

    def acquire_lease(self, session_id: str, owner: str, *, lease_seconds: float) -> None:
        ...

    def renew_lease(self, session_id: str, owner: str, *, lease_seconds: float) -> None:
        ...

    def release_lease(self, session_id: str, owner: str) -> None:
        ...

    def request_interrupt(self, session_id: str) -> str:
        ...

    def interrupt_requested_at(self, session_id: str) -> str | None:
        ...

    def get_model_call(self, session_id: str, request_id: str) -> dict[str, Any] | None:
        ...

    def get_tool_call(self, session_id: str, call_id: str) -> dict[str, Any] | None:
        ...

    def list_model_calls(self, session_id: str) -> list[dict[str, Any]]:
        ...

    def list_tool_calls(self, session_id: str) -> list[dict[str, Any]]:
        ...

    def completed_model_call_count(self, session_id: str) -> int:
        ...

    def get_summary(self, session_id: str, summary_id: str) -> SummaryRecord | None:
        ...

    def get_latest_summary(self, session_id: str) -> SummaryRecord | None:
        ...

    def list_summaries(self, session_id: str) -> list[SummaryRecord]:
        ...


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PersistenceError(f"value is not JSON serializable: {exc}") from exc


def _json_loads(raw: str, *, description: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InvariantViolation(f"invalid {description} JSON") from exc


def _scoped_tool_call_id(session_id: str, call_id: str) -> str:
    """Keep provider call ids session-scoped inside the global SQL primary key."""

    return f"{len(session_id)}:{session_id}:{call_id}"


def _unscoped_tool_call_id(session_id: str, storage_id: str) -> str:
    prefix = f"{len(session_id)}:{session_id}:"
    return storage_id[len(prefix) :] if storage_id.startswith(prefix) else storage_id


class SQLiteRunJournal:
    """SQLite authority for session snapshots, messages and committed events."""

    MAX_WRITER_LEASE_SECONDS = 300.0

    def __init__(
        self,
        db_path: str | Path,
        *,
        trace_root: str | Path | None = None,
        clock: Callable[[], str] | None = None,
        lease_clock: Callable[[], float] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        commit_hook: Callable[[str], None] | None = None,
        fault_injector: Callable[[str], None] | None = None,
        mapping_failure_hook: Callable[[str], None] | None = None,
    ):
        self.db_path = Path(db_path)
        self._database = str(db_path)
        if self._database != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if trace_root is not None:
            self.trace_root = Path(trace_root)
        elif self._database == ":memory:":
            self.trace_root = Path(tempfile.mkdtemp(prefix="coding-agent-traces-"))
        else:
            self.trace_root = self.db_path.parent / "traces"
        self.trace_root.mkdir(parents=True, exist_ok=True)
        self.clock = clock or self._default_clock
        self.lease_clock = lease_clock or time.time
        self.event_id_factory = event_id_factory or (lambda: str(uuid.uuid4()))
        self.commit_hook = commit_hook or fault_injector
        self.mapping_failure_hook = mapping_failure_hook
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self._database,
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._configure_connection()
            self.schema_version = MigrationRunner(clock=self.clock).migrate(self._connection)
            # Existing sessions are upgraded through independent, idempotent M1
            # mapping transactions.  This is intentionally outside the migration
            # DDL transaction and outside Runtime admission.
            self.backfill_legacy_product_mappings()
            # M2 recovery barriers are SQLite authority too.  Startup repairs
            # are idempotent and create no artifact in a bound checkout.
            self.repair_recovery_barriers_after_restart()
            self.repair_resolved_recovery_barriers_after_restart()
            self.repair_admitted_turn_finalizations_after_restart()
            self.repair_stable_writer_claims_after_restart()
        except BaseException:
            # Backfill failures are recorded in their own committed recovery
            # transaction.  Do not leave a half-constructed journal holding a
            # shared connection after surfacing that failure.
            self._connection.close()
            raise

    @staticmethod
    def _default_clock() -> str:
        from coding_agent.domain import utc_now

        return utc_now()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def _configure_connection(self) -> None:
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")

    @staticmethod
    def _timestamp_epoch(value: str) -> float:
        try:
            return datetime.fromisoformat(value).timestamp()
        except (TypeError, ValueError, OverflowError):
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise PersistenceError(f"invalid lease timestamp: {value!r}") from exc

    def _lease_now(self) -> float:
        return float(self.lease_clock())

    def _lease_expires_at(self, seconds: float) -> str:
        if seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        return datetime.fromtimestamp(
            self._lease_now() + seconds, timezone.utc
        ).isoformat()

    def migrate(self) -> int:
        with self._lock:
            self.schema_version = MigrationRunner(clock=self.clock).migrate(self._connection)
        return self.schema_version

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteRunJournal":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def _invoke_commit_hook(self) -> None:
        if self.commit_hook is not None:
            self.commit_hook("before_commit")

    def _new_event(
        self,
        *,
        session_id: str,
        sequence: int,
        event_type: EventType,
        state: RuntimeState,
        timestamp: str,
        payload: JsonObject,
    ) -> Event:
        return Event(
            schema_version=EVENT_SCHEMA_VERSION,
            event_id=str(self.event_id_factory()),
            session_id=session_id,
            sequence=sequence,
            event_type=event_type,
            timestamp=timestamp,
            state=state,
            payload=dict(payload),
        )

    def _session_values(
        self,
        snapshot: RuntimeSnapshot,
        *,
        updated_at: str,
        last_event_sequence: int,
        version: int,
    ) -> tuple[Any, ...]:
        return (
            snapshot.task,
            snapshot.source_path,
            snapshot.workspace_path,
            snapshot.state.value,
            _json_dumps(snapshot.policy.to_dict()),
            snapshot.source_fingerprint,
            snapshot.final_answer,
            _json_dumps(snapshot.failure) if snapshot.failure is not None else None,
            snapshot.step_count,
            snapshot.model_calls,
            snapshot.tool_calls,
            last_event_sequence,
            version,
            snapshot.created_at,
            updated_at,
            snapshot.interrupt_requested_at,
            (
                snapshot.resume_target_state.value
                if snapshot.resume_target_state is not None
                else None
            ),
            snapshot.context_version,
            snapshot.session_id,
        )

    def _insert_event(self, event: Event) -> None:
        self._connection.execute(
            """
            INSERT INTO events(
                event_id, session_id, sequence, schema_version, event_type,
                state, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.session_id,
                event.sequence,
                event.schema_version,
                event.event_type.value,
                event.state.value,
                _json_dumps(event.payload),
                event.timestamp,
            ),
        )

    def _insert_message(self, session_id: str, message: Message, index: int, timestamp: str) -> None:
        self._connection.execute(
            """
            INSERT INTO messages(
                session_id, message_index, role, content, tool_call_id,
                metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                index,
                message.role,
                message.content,
                message.tool_call_id,
                _json_dumps(message.metadata),
                timestamp,
            ),
        )

    def _upsert_checkpoint(self, snapshot: RuntimeSnapshot, timestamp: str) -> None:
        self._connection.execute(
            """
            INSERT INTO checkpoints(session_id, state, snapshot_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                state=excluded.state,
                snapshot_json=excluded.snapshot_json,
                updated_at=excluded.updated_at
            """,
            (
                snapshot.session_id,
                snapshot.state.value,
                snapshot.to_json(),
                timestamp,
            ),
        )

    def _upsert_model_call(self, session_id: str, mutation: ModelCallMutation, timestamp: str) -> None:
        if not mutation.request_id:
            raise InvariantViolation("model request id cannot be empty")
        existing = self._connection.execute(
            "SELECT session_id FROM model_calls WHERE request_id = ?",
            (mutation.request_id,),
        ).fetchone()
        started_at = mutation.started_at or timestamp
        response_json = (
            _json_dumps(mutation.response) if mutation.response is not None else None
        )
        error_json = _json_dumps(mutation.error) if mutation.error is not None else None
        finished_at = mutation.finished_at or (
            timestamp if mutation.status in {"succeeded", "failed", "uncertain"} else None
        )
        if existing is None:
            self._connection.execute(
                """
                INSERT INTO model_calls(
                    request_id, session_id, ordinal, attempt, backend, status,
                    request_json, response_json, error_json, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mutation.request_id,
                    session_id,
                    mutation.ordinal,
                    mutation.attempt,
                    mutation.backend,
                    mutation.status,
                    _json_dumps(mutation.request),
                    response_json,
                    error_json,
                    started_at,
                    finished_at,
                ),
            )
            return
        self._connection.execute(
            """
            UPDATE model_calls SET
                attempt=?, backend=?, status=?, request_json=?,
                response_json=?, error_json=?, started_at=COALESCE(started_at, ?),
                finished_at=?
            WHERE request_id=? AND session_id=?
            """,
            (
                mutation.attempt,
                mutation.backend,
                mutation.status,
                _json_dumps(mutation.request),
                response_json,
                error_json,
                started_at,
                finished_at,
                mutation.request_id,
                session_id,
            ),
        )

    def _upsert_tool_call(self, session_id: str, mutation: ToolCallMutation, timestamp: str) -> None:
        if not mutation.call_id:
            raise InvariantViolation("tool call id cannot be empty")
        storage_call_id = _scoped_tool_call_id(session_id, mutation.call_id)
        existing = self._connection.execute(
            "SELECT session_id FROM tool_calls WHERE call_id = ?", (storage_call_id,)
        ).fetchone()
        started_at = mutation.started_at or timestamp
        result_json = _json_dumps(mutation.result) if mutation.result is not None else None
        error_json = _json_dumps(mutation.error) if mutation.error is not None else None
        finished_at = mutation.finished_at or (
            timestamp
            if mutation.status in {ToolCallState.SUCCEEDED, ToolCallState.FAILED}
            else None
        )
        if existing is None:
            self._connection.execute(
                """
                INSERT INTO tool_calls(
                    call_id, session_id, ordinal, attempt, tool_name, arguments_json,
                    recovery_mode, status, pre_revision, planned_post_revision,
                    result_json, error_json, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    storage_call_id,
                    session_id,
                    mutation.ordinal,
                    mutation.attempt,
                    mutation.tool_name,
                    _json_dumps(mutation.arguments),
                    mutation.recovery_mode.value,
                    mutation.status.value,
                    mutation.pre_revision,
                    mutation.planned_post_revision,
                    result_json,
                    error_json,
                    started_at,
                    finished_at,
                ),
            )
            return
        self._connection.execute(
            """
            UPDATE tool_calls SET
                attempt=?, tool_name=?, arguments_json=?, recovery_mode=?, status=?,
                pre_revision=COALESCE(?, pre_revision),
                planned_post_revision=COALESCE(?, planned_post_revision),
                result_json=?, error_json=?, started_at=COALESCE(started_at, ?),
                finished_at=?
            WHERE call_id=? AND session_id=?
            """,
            (
                mutation.attempt,
                mutation.tool_name,
                _json_dumps(mutation.arguments),
                mutation.recovery_mode.value,
                mutation.status.value,
                mutation.pre_revision,
                mutation.planned_post_revision,
                result_json,
                error_json,
                started_at,
                finished_at,
                storage_call_id,
                session_id,
            ),
        )

    def _upsert_summary(
        self,
        session_id: str,
        mutation: SummaryMutation,
        timestamp: str,
    ) -> None:
        record = mutation.record
        if record.session_id != session_id:
            raise InvariantViolation("summary session id does not match mutation session")
        if record.schema_version != 1:
            raise InvariantViolation("unsupported summary schema version")
        if record.source_event_start <= 0 or record.source_event_end < record.source_event_start:
            raise InvariantViolation("summary event range is invalid")
        summary_json = _json_dumps(record.to_dict())
        existing = self._connection.execute(
            "SELECT summary_json FROM summaries WHERE summary_id=? AND session_id=?",
            (record.summary_id, session_id),
        ).fetchone()
        if existing is None:
            self._connection.execute(
                """
                INSERT INTO summaries(
                    summary_id, session_id, schema_version, source_event_start,
                    source_event_end, source_event_hash, workspace_revision,
                    summary_json, created_at, superseded_by, stale
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.summary_id,
                    session_id,
                    record.schema_version,
                    record.source_event_start,
                    record.source_event_end,
                    record.source_event_hash,
                    record.workspace_revision,
                    summary_json,
                    record.created_at or timestamp,
                    record.superseded_by,
                    int(record.stale),
                ),
            )
        else:
            previous = _json_loads(str(existing["summary_json"]), description="summary")
            if not isinstance(previous, Mapping):
                raise InvariantViolation("persisted summary must be an object")
            previous_record = SummaryRecord.from_dict(previous)
            immutable_fields = (
                "session_id",
                "source_event_start",
                "source_event_end",
                "source_event_hash",
                "workspace_revision",
            )
            if any(
                getattr(previous_record, field) != getattr(record, field)
                for field in immutable_fields
            ):
                raise InvariantViolation("summary identity fields cannot be changed")
            self._connection.execute(
                """
                UPDATE summaries SET summary_json=?, superseded_by=?, stale=?
                WHERE summary_id=? AND session_id=?
                """,
                (
                    summary_json,
                    record.superseded_by,
                    int(record.stale),
                    record.summary_id,
                    session_id,
                ),
            )
        if mutation.supersedes is not None:
            previous_row = self._connection.execute(
                "SELECT summary_json FROM summaries "
                "WHERE session_id=? AND summary_id=?",
                (session_id, mutation.supersedes),
            ).fetchone()
            if previous_row is None:
                raise InvariantViolation("summary to supersede was not found")
            previous_raw = _json_loads(
                str(previous_row["summary_json"]), description="summary"
            )
            if not isinstance(previous_raw, Mapping):
                raise InvariantViolation("persisted summary must be an object")
            previous = SummaryRecord.from_dict(previous_raw)
            previous = replace(previous, superseded_by=record.summary_id)
            updated = self._connection.execute(
                """
                UPDATE summaries SET summary_json=?, superseded_by=?
                WHERE session_id=? AND summary_id=? AND summary_id<>?
                """,
                (
                    _json_dumps(previous.to_dict()),
                    record.summary_id,
                    session_id,
                    mutation.supersedes,
                    record.summary_id,
                ),
            )
            if updated.rowcount != 1:
                raise InvariantViolation("summary to supersede was not found")

    def create_session(
        self,
        snapshot: RuntimeSnapshot,
        initial_message: Message,
        *,
        session_created_payload: JsonObject | None = None,
    ) -> tuple[Event, ...]:
        if snapshot.version != 0:
            raise InvariantViolation("a new session must start at journal version 0")
        if not snapshot.session_id:
            raise ValueError("session id cannot be empty")
        with self._lock:
            try:
                with self._write_transaction():
                    created_event, message_event = self._insert_new_session(
                        snapshot, initial_message,
                        session_created_payload=session_created_payload,
                    )
                    self._invoke_commit_hook()
            except sqlite3.IntegrityError as exc:
                raise PersistenceError("failed to create session") from exc
        return (created_event, message_event)

    def _insert_new_session(
        self,
        snapshot: RuntimeSnapshot,
        initial_message: Message,
        *,
        session_created_payload: JsonObject | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> tuple[Event, Event]:
        """Insert the legacy admission bundle inside an already-open transaction."""
        event_timestamp = self.clock()
        payload: JsonObject = {
            "task": snapshot.task,
            "source_name": Path(snapshot.source_path).name,
        }
        if session_created_payload:
            payload.update(session_created_payload)
        existing = self._connection.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (snapshot.session_id,)
        ).fetchone()
        if existing is not None:
            raise PersistenceError(f"session already exists: {snapshot.session_id}")
        if fault_hook is not None:
            fault_hook("before_session_row")
        self._connection.execute(
            """
            INSERT INTO sessions(
                id, task, source_path, workspace_path, state, policy_json,
                source_fingerprint, final_answer, failure_json, step_count,
                model_calls, tool_calls, last_event_sequence, version,
                created_at, updated_at, interrupt_requested_at,
                resume_target_state, context_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.session_id, snapshot.task, snapshot.source_path,
                snapshot.workspace_path, snapshot.state.value,
                _json_dumps(snapshot.policy.to_dict()), snapshot.source_fingerprint,
                snapshot.final_answer,
                _json_dumps(snapshot.failure) if snapshot.failure is not None else None,
                snapshot.step_count, snapshot.model_calls, snapshot.tool_calls, 2,
                snapshot.version, snapshot.created_at, snapshot.updated_at,
                snapshot.interrupt_requested_at,
                snapshot.resume_target_state.value if snapshot.resume_target_state else None,
                snapshot.context_version,
            ),
        )
        if fault_hook is not None:
            fault_hook("after_session_row")
            fault_hook("before_checkpoint")
        self._upsert_checkpoint(snapshot, snapshot.updated_at)
        if fault_hook is not None:
            fault_hook("after_checkpoint")
        created_event = self._new_event(
            session_id=snapshot.session_id, sequence=1,
            event_type=EventType.SESSION_CREATED, state=snapshot.state,
            timestamp=event_timestamp, payload=payload,
        )
        message_event = self._new_event(
            session_id=snapshot.session_id, sequence=2,
            event_type=EventType.MESSAGE_ADDED, state=snapshot.state,
            timestamp=event_timestamp,
            payload={"message": initial_message.to_dict(), "message_index": 0},
        )
        if fault_hook is not None:
            fault_hook("before_initial_message")
        self._insert_message(snapshot.session_id, initial_message, 0, event_timestamp)
        if fault_hook is not None:
            fault_hook("after_initial_message")
            fault_hook("before_session_created_event")
        self._insert_event(created_event)
        if fault_hook is not None:
            fault_hook("after_session_created_event")
            fault_hook("before_message_added_event")
        self._insert_event(message_event)
        if fault_hook is not None:
            fault_hook("after_message_added_event")
        return created_event, message_event

    def register_repository_identity(
        self,
        descriptors: Mapping[str, str],
        *,
        repository_id: str | None = None,
    ) -> str:
        """Register a generated product identity and its observed descriptors.

        Only a previously registered canonical path or Git common-directory can
        discover an identity.  Remote and history evidence is retained for
        inspection, but deliberately cannot merge independent clones.
        """

        normalized = {
            kind: value for kind, value in descriptors.items() if kind and value
        }
        if not normalized:
            raise ValueError("at least one non-empty repository descriptor is required")
        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                selected = repository_id
                if selected is None:
                    discovery = [
                        (kind, value)
                        for kind, value in normalized.items()
                        if kind in {"canonical_path", "git_common_dir"}
                    ]
                    candidates = {
                        str(row["repository_id"])
                        for kind, value in discovery
                        for row in self._connection.execute(
                            """
                            SELECT repository_id FROM repository_descriptors
                            WHERE descriptor_kind=? AND descriptor_value=?
                            """,
                            (kind, value),
                        )
                    }
                    if len(candidates) > 1:
                        raise PersistenceError("repository descriptor discovery is ambiguous")
                    selected = next(iter(candidates), str(uuid.uuid4()))
                existing = self._connection.execute(
                    "SELECT 1 FROM repository_identities WHERE repository_id=?", (selected,)
                ).fetchone()
                if existing is None:
                    if repository_id is not None:
                        raise PersistenceError("repository identity must be registered before adding descriptors")
                    self._connection.execute(
                        "INSERT INTO repository_identities(repository_id, created_at) VALUES (?, ?)",
                        (selected, timestamp),
                    )
                for kind, value in normalized.items():
                    if kind in {"canonical_path", "git_common_dir"}:
                        conflict = self._connection.execute(
                            """
                            SELECT repository_id FROM repository_descriptors
                            WHERE descriptor_kind=? AND descriptor_value=?
                            """,
                            (kind, value),
                        ).fetchone()
                        if conflict is not None and str(conflict["repository_id"]) != selected:
                            raise PersistenceError(
                                "a discovery descriptor cannot belong to multiple repository identities"
                            )
                    exists = self._connection.execute(
                        """
                        SELECT 1 FROM repository_descriptors
                        WHERE repository_id=? AND descriptor_kind=? AND descriptor_value=?
                        """,
                        (selected, kind, value),
                    ).fetchone()
                    if exists is None:
                        self._connection.execute(
                            """
                            INSERT INTO repository_descriptors(
                                descriptor_id, repository_id, descriptor_kind,
                                descriptor_value, observed_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (str(uuid.uuid4()), selected, kind, value, timestamp),
                        )
        return selected

    def get_repository_identity(self, repository_id: str) -> RepositoryIdentity | None:
        """Load a generated M1 identity; descriptor evidence remains separate."""

        with self._lock:
            row = self._connection.execute(
                "SELECT repository_id, created_at FROM repository_identities WHERE repository_id=?",
                (repository_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return RepositoryIdentity(str(row["repository_id"]), str(row["created_at"]))
        except (TypeError, ValueError) as exc:
            raise PersistenceError("corrupt repository identity") from exc

    def list_repository_descriptors(self, repository_id: str) -> list[RepositoryDescriptor]:
        """Load ordered discovery evidence without making it identity authority."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT descriptor_id, repository_id, descriptor_kind, descriptor_value
                FROM repository_descriptors WHERE repository_id=?
                ORDER BY descriptor_kind, descriptor_value, descriptor_id
                """,
                (repository_id,),
            ).fetchall()
        try:
            return [
                RepositoryDescriptor(
                    str(row["descriptor_id"]),
                    str(row["repository_id"]),
                    str(row["descriptor_kind"]),
                    str(row["descriptor_value"]),
                )
                for row in rows
            ]
        except (TypeError, ValueError) as exc:
            raise PersistenceError("corrupt repository descriptor") from exc

    def ensure_legacy_product_mapping(
        self,
        session_id: str,
        *,
        failure_hook: Callable[[str], None] | None = None,
    ) -> RuntimeExecution:
        """Atomically create, or return, the private M1 mapping for one Session.

        This is deliberately a separate M1 transaction from legacy Session
        creation.  It supplies recoverable compatibility data without changing
        legacy ``run_task()`` admission/lifecycle atomicity.
        """

        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                mapped = self._connection.execute(
                    """
                    SELECT runtime_execution_id, legacy_session_id, turn_id,
                           workspace_binding_id
                    FROM runtime_executions WHERE legacy_session_id=?
                    """,
                    (session_id,),
                ).fetchone()
                if mapped is not None:
                    # Never bless a pre-existing V5 row merely because its
                    # execution row survives.  A reopen/backfill must reject
                    # a broken aggregate before recording recovery.
                    execution = self.get_legacy_product_mapping(session_id)
                    if execution is None:
                        raise PersistenceError("mapped legacy session has no runtime execution")
                    self._mark_mapping_recovered(session_id, timestamp)
                    return execution
                session = self._connection.execute(
                    "SELECT source_path, workspace_path FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                if session is None:
                    raise SessionNotFound(session_id)
                # The path is a mutable descriptor, never the identity value.
                canonical_path = str(Path(str(session["source_path"])).resolve())
                repository_id = self._register_repository_identity_in_transaction(
                    {"canonical_path": canonical_path}, timestamp
                )
                scope_row = self._connection.execute(
                    """
                    SELECT project_scope_id FROM project_scopes
                    WHERE repository_id=? AND relative_path='.'
                    """,
                    (repository_id,),
                ).fetchone()
                if scope_row is None:
                    project_scope_id = str(uuid.uuid4())
                    self._connection.execute(
                        """
                        INSERT INTO project_scopes(project_scope_id, repository_id, relative_path, created_at)
                        VALUES (?, ?, '.', ?)
                        """,
                        (project_scope_id, repository_id, timestamp),
                    )
                else:
                    project_scope_id = str(scope_row["project_scope_id"])
                workspace_path = session["workspace_path"]
                if workspace_path:
                    binding_kind = "legacy_copied_workspace"
                    binding_locator = str(Path(str(workspace_path)).resolve())
                else:
                    # No Runtime workspace was recorded yet. Do not make the
                    # source descriptor look like an execution workspace.
                    binding_kind = "legacy_unprepared_compatibility"
                    binding_locator = f"legacy-session:{session_id}:workspace-not-recorded"
                workspace_binding_id = str(uuid.uuid4())
                self._connection.execute(
                    """
                    INSERT INTO workspace_bindings(
                        workspace_binding_id, repository_id, project_scope_id,
                        binding_kind, locator, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_binding_id,
                        repository_id,
                        project_scope_id,
                        binding_kind,
                        binding_locator,
                        timestamp,
                    ),
                )
                conversation_id = str(uuid.uuid4())
                self._connection.execute(
                    """
                    INSERT INTO conversations(
                        conversation_id, repository_id, project_scope_id,
                        default_workspace_binding_id, provenance_kind, created_at
                    ) VALUES (?, ?, ?, ?, 'synthetic_legacy_import', ?)
                    """,
                    (conversation_id, repository_id, project_scope_id, workspace_binding_id, timestamp),
                )
                turn_id = str(uuid.uuid4())
                self._connection.execute(
                    """
                    INSERT INTO turns(turn_id, conversation_id, ordinal, provenance_kind, created_at)
                    VALUES (?, ?, 1, 'synthetic_legacy_import', ?)
                    """,
                    (turn_id, conversation_id, timestamp),
                )
                self._connection.execute(
                    """
                    INSERT INTO conversation_semantic_events(
                        conversation_event_id, conversation_id, sequence, event_type,
                        provenance_kind, provenance_json, created_at
                    ) VALUES (?, ?, 1, 'legacy_session_imported', 'synthetic_legacy_mapping', ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        conversation_id,
                        _json_dumps(
                            {
                                "legacy_session_id": session_id,
                                "semantic_limit": (
                                    "mapping only; does not reconstruct an InitialRequest "
                                    "or historical interactive exchange"
                                ),
                            }
                        ),
                        timestamp,
                    ),
                )
                hook = failure_hook or self.mapping_failure_hook
                if hook is not None:
                    hook("before_runtime_execution_insert")
                runtime_execution_id = str(uuid.uuid4())
                self._connection.execute(
                    """
                    INSERT INTO runtime_executions(
                        runtime_execution_id, legacy_session_id, turn_id,
                        workspace_binding_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (runtime_execution_id, session_id, turn_id, workspace_binding_id, timestamp),
                )
                self._mark_mapping_recovered(session_id, timestamp)
                return RuntimeExecution(
                    runtime_execution_id=runtime_execution_id,
                    legacy_session_id=session_id,
                    turn_id=turn_id,
                    workspace_binding_id=workspace_binding_id,
                )

    def backfill_legacy_product_mappings(self) -> list[RuntimeExecution]:
        """Map every legacy Session, allowing a later retry after interruption."""

        with self._lock:
            session_ids = [
                str(row["id"])
                for row in self._connection.execute("SELECT id FROM sessions ORDER BY created_at, id")
            ]
        mappings: list[RuntimeExecution] = []
        for session_id in session_ids:
            try:
                mappings.append(self.ensure_legacy_product_mapping(session_id))
            except Exception as error:
                # Mapping rows roll back as a unit. Record the recovery seam in
                # a separate transaction, then surface the failed backfill.
                try:
                    self.record_legacy_product_mapping_failure(session_id, error)
                except Exception:
                    pass
                raise
        return mappings

    def get_legacy_product_mapping(self, session_id: str) -> RuntimeExecution | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT runtime_execution_id, legacy_session_id, turn_id, workspace_binding_id
                FROM runtime_executions WHERE legacy_session_id=?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            execution = RuntimeExecution(
                runtime_execution_id=str(row["runtime_execution_id"]),
                legacy_session_id=str(row["legacy_session_id"]),
                turn_id=str(row["turn_id"]),
                workspace_binding_id=str(row["workspace_binding_id"]),
            )
            inspected = self.inspect_legacy_product_mapping(session_id)
            if inspected is None:
                raise PersistenceError("corrupt product mapping has no complete aggregate")
            inspected_execution = inspected["runtime_execution"]
            if inspected_execution != execution:
                raise PersistenceError("corrupt product mapping has mismatched runtime execution")
            return execution

    def record_legacy_product_mapping_failure(self, session_id: str, error: Exception) -> None:
        """Persist an observable recovery seam without changing a RunResult."""

        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                self._connection.execute(
                    """
                    INSERT INTO product_mapping_failures(
                        legacy_session_id, attempt_count, last_error,
                        first_failed_at, last_failed_at, recovered_at
                    ) VALUES (?, 1, ?, ?, ?, NULL)
                    ON CONFLICT(legacy_session_id) DO UPDATE SET
                        attempt_count=product_mapping_failures.attempt_count + 1,
                        last_error=excluded.last_error,
                        last_failed_at=excluded.last_failed_at,
                        recovered_at=NULL
                    """,
                    (session_id, f"{type(error).__name__}: {error}", timestamp, timestamp),
                )

    def get_legacy_product_mapping_failure(self, session_id: str) -> dict[str, object] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT attempt_count, last_error, first_failed_at, last_failed_at, recovered_at
                FROM product_mapping_failures WHERE legacy_session_id=?
                """,
                (session_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def inspect_legacy_product_mapping(self, session_id: str) -> dict[str, object] | None:
        """Return M1 records for compatibility inspection, without IPC exposure."""

        with self._lock:
            execution_exists = self._connection.execute(
                "SELECT 1 FROM runtime_executions WHERE legacy_session_id=?", (session_id,)
            ).fetchone()
            row = self._connection.execute(
                """
            SELECT execution.runtime_execution_id, execution.legacy_session_id,
                   execution.turn_id, execution.workspace_binding_id,
                   turn.conversation_id, turn.ordinal, turn.provenance_kind AS turn_provenance,
                   conversation.repository_id, conversation.project_scope_id,
                   conversation.default_workspace_binding_id AS conversation_binding_id,
                   conversation.provenance_kind AS conversation_provenance,
                   scope.repository_id AS scope_repository_id, scope.relative_path,
                   binding.repository_id AS binding_repository_id,
                   binding.project_scope_id AS binding_project_scope_id,
                   binding.binding_kind, binding.locator,
                   default_binding.repository_id AS default_binding_repository_id,
                   default_binding.project_scope_id AS default_binding_project_scope_id,
                   identity.created_at AS repository_created_at
            FROM runtime_executions execution
            JOIN turns turn ON turn.turn_id=execution.turn_id
            JOIN conversations conversation ON conversation.conversation_id=turn.conversation_id
            JOIN project_scopes scope ON scope.project_scope_id=conversation.project_scope_id
            JOIN workspace_bindings binding ON binding.workspace_binding_id=execution.workspace_binding_id
            JOIN workspace_bindings default_binding
                ON default_binding.workspace_binding_id=conversation.default_workspace_binding_id
            JOIN repository_identities identity ON identity.repository_id=conversation.repository_id
            WHERE execution.legacy_session_id=?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                if execution_exists is not None:
                    raise PersistenceError("corrupt product mapping has a broken aggregate relation")
                return None
            try:
                repository = RepositoryIdentity(str(row["repository_id"]), str(row["repository_created_at"]))
                scope = ProjectScope(
                    str(row["project_scope_id"]),
                    str(row["scope_repository_id"]),
                    str(row["relative_path"]),
                )
                binding = WorkspaceBinding(
                    str(row["workspace_binding_id"]),
                    str(row["binding_repository_id"]),
                    str(row["binding_project_scope_id"]),
                    str(row["binding_kind"]),
                    str(row["locator"]),
                )
                conversation = Conversation(
                    str(row["conversation_id"]),
                    repository.repository_id,
                    scope.project_scope_id,
                    str(row["conversation_binding_id"]),
                    str(row["conversation_provenance"]),
                )
                turn = Turn(
                    str(row["turn_id"]),
                    conversation.conversation_id,
                    int(row["ordinal"]),
                    str(row["turn_provenance"]),
                )
                execution = RuntimeExecution(
                    str(row["runtime_execution_id"]),
                    str(row["legacy_session_id"]),
                    turn.turn_id,
                    binding.workspace_binding_id,
                )
            except (TypeError, ValueError) as exc:
                raise PersistenceError("corrupt product mapping violates a domain invariant") from exc
            if (
                scope.repository_id != repository.repository_id
                or binding.repository_id != repository.repository_id
                or binding.project_scope_id != scope.project_scope_id
                or str(row["default_binding_repository_id"]) != repository.repository_id
                or str(row["default_binding_project_scope_id"]) != scope.project_scope_id
            ):
                raise PersistenceError("corrupt product mapping has inconsistent aggregate relations")
        return {
            "repository_identity": repository,
            "project_scope": scope,
            "workspace_binding": binding,
            "conversation": conversation,
            "turn": turn,
            "runtime_execution": execution,
        }

    def list_conversation_semantic_events(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
            SELECT event.event_type, event.provenance_kind, event.provenance_json, event.sequence
            FROM conversation_semantic_events event
            JOIN turns turn ON turn.conversation_id=event.conversation_id
            JOIN runtime_executions execution ON execution.turn_id=turn.turn_id
            WHERE execution.legacy_session_id=? ORDER BY event.sequence
                """,
                (session_id,),
            ).fetchall()
        return [
            {
                "event_type": str(row["event_type"]),
                "provenance_kind": str(row["provenance_kind"]),
                "provenance": _json_loads(str(row["provenance_json"]), description="semantic provenance"),
                "sequence": int(row["sequence"]),
            }
            for row in rows
        ]

    def _register_repository_identity_in_transaction(
        self, descriptors: Mapping[str, str], timestamp: str
    ) -> str:
        discovery = [
            (kind, value)
            for kind, value in descriptors.items()
            if kind in {"canonical_path", "git_common_dir"}
        ]
        candidates = {
            str(row["repository_id"])
            for kind, value in discovery
            for row in self._connection.execute(
                """
                SELECT repository_id FROM repository_descriptors
                WHERE descriptor_kind=? AND descriptor_value=?
                """,
                (kind, value),
            )
        }
        if len(candidates) > 1:
            raise PersistenceError("repository descriptor discovery is ambiguous")
        repository_id = next(iter(candidates), str(uuid.uuid4()))
        if not candidates:
            self._connection.execute(
                "INSERT INTO repository_identities(repository_id, created_at) VALUES (?, ?)",
                (repository_id, timestamp),
            )
        for kind, value in descriptors.items():
            exists = self._connection.execute(
                """
                SELECT 1 FROM repository_descriptors
                WHERE repository_id=? AND descriptor_kind=? AND descriptor_value=?
                """,
                (repository_id, kind, value),
            ).fetchone()
            if exists is None:
                self._connection.execute(
                    """
                    INSERT INTO repository_descriptors(
                        descriptor_id, repository_id, descriptor_kind,
                        descriptor_value, observed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), repository_id, kind, value, timestamp),
                )
        return repository_id

    def _mark_mapping_recovered(self, session_id: str, timestamp: str) -> None:
        self._connection.execute(
            """
            UPDATE product_mapping_failures SET recovered_at=?
            WHERE legacy_session_id=? AND recovered_at IS NULL
            """,
            (timestamp, session_id),
        )

    # ---- M2 Product lifecycle projection ---------------------------------
    # These methods use the same lock/connection/BEGIN IMMEDIATE authority as
    # the legacy journal.  They never advance RuntimeState; the inserted
    # Session is a normal CREATED legacy record and Runtime remains its owner.

    @staticmethod
    def canonical_product_payload_digest(payload: Mapping[str, object]) -> str:
        """Return the stable SHA-256 digest used by Product operation ids."""
        return hashlib.sha256(_json_dumps(dict(payload)).encode("utf-8")).hexdigest()

    @classmethod
    def canonical_admission_digest(
        cls, *, initial_request: str, repository_id: str, project_scope_id: str,
        workspace_binding_id: str, snapshot: RuntimeSnapshot, policy: RunPolicy,
        conversation_id: str | None, expected_conversation_version: int | None,
    ) -> str:
        # Retry identity covers caller intent and immutable admission inputs,
        # not wall-clock timestamps captured in an equivalent fresh snapshot.
        snapshot_identity = {
            "session_id": snapshot.session_id, "task": snapshot.task,
            "source_path": snapshot.source_path, "source_fingerprint": snapshot.source_fingerprint,
            "state": snapshot.state.value, "version": snapshot.version,
            "policy": snapshot.policy.to_dict(),
        }
        return cls.canonical_product_payload_digest({
            "kind": "turn_admission", "initial_request": initial_request,
            "repository_id": repository_id, "project_scope_id": project_scope_id,
            "workspace_binding_id": workspace_binding_id, "snapshot": snapshot_identity,
            "policy": policy.to_dict(), "conversation_id": conversation_id,
            "expected_conversation_version": expected_conversation_version,
        })

    @classmethod
    def canonical_input_digest(
        cls, *, conversation_id: str, input_kind: str, payload: Mapping[str, object],
        correlation_id: str | None, expected_conversation_version: int,
    ) -> str:
        return cls.canonical_product_payload_digest({
            "kind": input_kind, "conversation_id": conversation_id,
            "payload": dict(payload), "correlation_id": correlation_id,
            "expected_conversation_version": expected_conversation_version,
        })

    @classmethod
    def canonical_rebind_digest(
        cls, *, conversation_id: str, workspace_binding_id: str, expected_version: int,
        observation: Mapping[str, object],
    ) -> str:
        return cls.canonical_product_payload_digest({
            "kind": "conversation_rebind", "conversation_id": conversation_id,
            "workspace_binding_id": workspace_binding_id, "expected_version": expected_version,
            "observation": dict(observation),
        })

    def _read_admission_in_transaction(self, operation_id: str) -> TurnAdmission | None:
        row = self._connection.execute(
            """
            SELECT admission.operation_id, admission.payload_digest,
                   admission.conversation_id, admission.turn_id,
                   admission.runtime_execution_id, admission.legacy_session_id,
                   admission.checkpoint_id, artifact.checkpoint_json,
                   admission.instruction_manifest_id, artifact.instruction_manifest_json,
                   admission.policy_epoch_id, artifact.policy_json
            FROM product_admissions admission
            JOIN turn_admission_artifacts artifact ON artifact.turn_id=admission.turn_id
            WHERE admission.operation_id=?
            """,
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            checkpoint_raw = _json_loads(str(row["checkpoint_json"]), description="M2 checkpoint")
            manifest_raw = _json_loads(str(row["instruction_manifest_json"]), description="M2 manifest")
            policy_raw = _json_loads(str(row["policy_json"]), description="M2 policy epoch")
            if not all(isinstance(value, Mapping) for value in (checkpoint_raw, manifest_raw, policy_raw)):
                raise PersistenceError("corrupt M2 admission artifact")
            return TurnAdmission(
                operation_id=str(row["operation_id"]), payload_digest=str(row["payload_digest"]),
                conversation_id=str(row["conversation_id"]), turn_id=str(row["turn_id"]),
                runtime_execution_id=str(row["runtime_execution_id"]),
                legacy_session_id=str(row["legacy_session_id"]),
                checkpoint=TurnStartCodeCheckpoint(
                    str(row["checkpoint_id"]), str(checkpoint_raw["observation_digest"]),
                    str(checkpoint_raw["coverage_state"]), str(checkpoint_raw["excluded_state"]),
                    str(checkpoint_raw.get("observation_frontier", "legacy_m2_unknown_frontier")),
                    dict(checkpoint_raw.get("baseline_references", {})),
                    dict(checkpoint_raw.get("exclusions", {})),
                ),
                instruction_manifest=InstructionManifest(
                    str(row["instruction_manifest_id"]), str(manifest_raw["placeholder_kind"]),
                ),
                policy_epoch=PolicyEpoch(str(row["policy_epoch_id"]), str(policy_raw["mode"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceError("corrupt M2 admission aggregate") from exc

    def get_turn_admission(self, operation_id: str) -> TurnAdmission | None:
        with self._lock:
            return self._read_admission_in_transaction(operation_id)

    def inspect_conversation(self, conversation_id: str) -> dict[str, object] | None:
        """Read a Conversation projection without dispatching/resuming Runtime work."""
        with self._lock:
            row = self._connection.execute(
                """SELECT conversation_id, repository_id, project_scope_id,
                          default_workspace_binding_id, product_version, open_turn_id
                   FROM conversations WHERE conversation_id=?""", (conversation_id,),
            ).fetchone()
            if row is None:
                return None
            turns = self._connection.execute(
                """SELECT turns.turn_id, turns.ordinal, executions.runtime_execution_id,
                          executions.legacy_session_id, executions.workspace_binding_id, sessions.state
                   FROM turns JOIN runtime_executions executions ON executions.turn_id=turns.turn_id
                   JOIN sessions ON sessions.id=executions.legacy_session_id
                   WHERE turns.conversation_id=? ORDER BY turns.ordinal""", (conversation_id,),
            ).fetchall()
        return {
            "conversation_id": str(row["conversation_id"]), "repository_id": str(row["repository_id"]),
            "project_scope_id": str(row["project_scope_id"]),
            "default_workspace_binding_id": str(row["default_workspace_binding_id"]),
            "product_version": int(row["product_version"]), "open_turn_id": row["open_turn_id"],
            "turns": [dict(turn) for turn in turns], "resume_started": False,
        }

    def resume_execution(self, operation_id: str) -> TurnAdmission:
        """Validate an immutable, unfinished execution before a host resumes it.

        This is deliberately not Conversation inspection: a moved/drifted
        direct tree, recovery barrier, or terminal execution is rejected
        before any Runtime host can be attached.
        """
        # Resolve the immutable binding first, then observe outside SQLite.
        with self._lock:
            admission = self._read_admission_in_transaction(operation_id)
            if admission is None:
                raise ProductLifecycleConflict("admission was not found")
            row = self._connection.execute(
                """SELECT executions.workspace_binding_id, bindings.locator, bindings.binding_kind,
                          sessions.state, sessions.lease_owner, sessions.lease_expires_at,
                          artifact.checkpoint_json
                   FROM runtime_executions executions
                   JOIN workspace_bindings bindings ON bindings.workspace_binding_id=executions.workspace_binding_id
                   JOIN sessions ON sessions.id=executions.legacy_session_id
                   JOIN turn_admission_artifacts artifact ON artifact.turn_id=executions.turn_id
                   WHERE executions.runtime_execution_id=?""", (admission.runtime_execution_id,),
                ).fetchone()
            if row is None or not str(row["locator"]):
                raise ProductLifecycleConflict("immutable execution WorkspaceBinding is unavailable")
            if RuntimeState(str(row["state"])) in TERMINAL_STATES:
                raise ProductLifecycleConflict("terminal RuntimeExecution cannot be resumed")
            barrier = self._connection.execute(
                "SELECT 1 FROM workspace_recovery_barriers WHERE workspace_binding_id=? AND status='active'",
                (str(row["workspace_binding_id"]),),
            ).fetchone()
            if barrier is not None:
                raise ProductLifecycleConflict("recovery barrier blocks execution resume")
            binding_id = str(row["workspace_binding_id"])
            binding_kind = str(row["binding_kind"])
            checkpoint_raw = _json_loads(str(row["checkpoint_json"]), description="turn checkpoint")
        if binding_kind == "m2_direct_read_only":
            fresh, _ = self._capture_direct_binding_observation(binding_id)
            if fresh is None or not isinstance(checkpoint_raw, Mapping):
                raise ProductLifecycleConflict("immutable execution WorkspaceBinding is unavailable")
            with self._lock:
                stored_row = self._connection.execute(
                    """SELECT observation_json FROM workspace_binding_observations
                       WHERE workspace_binding_id=? AND observation_digest=?""",
                    (binding_id, str(checkpoint_raw.get("observation_digest", ""))),
                ).fetchone()
                if stored_row is None:
                    raise ProductLifecycleConflict("execution checkpoint observation is unavailable")
                stored = _json_loads(str(stored_row["observation_json"]), description="checkpoint observation")
                if not isinstance(stored, Mapping):
                    raise PersistenceError("execution checkpoint observation is corrupt")
                old_git = stored.get("git_facts", {})
                new_git = fresh.get("git_facts", {})
                for fact in ("branch_head", "branch_tip"):
                    if isinstance(old_git, Mapping) and isinstance(new_git, Mapping) and old_git.get(fact) != new_git.get(fact):
                        raise ProductLifecycleConflict("execution branch/HEAD observation has drifted")
                # Ordinary content/index drift invalidates the old observation
                # but does not silently change the immutable binding.
                with self._write_transaction():
                    self._persist_binding_observation_in_transaction(binding_id, fresh, self.clock())
        return admission

    def inspect_workspace_startup(self, workspace_binding_id: str) -> dict[str, object]:
        """Read-only discovery required before an M2 Product attachment is considered."""
        with self._lock:
            binding = self._connection.execute(
                "SELECT locator, binding_kind FROM workspace_bindings WHERE workspace_binding_id=?", (workspace_binding_id,)
            ).fetchone()
            if binding is None:
                raise ProductLifecycleConflict("workspace binding was not found")
            observations = self._connection.execute(
                "SELECT observation_json, observed_at FROM workspace_binding_observations WHERE workspace_binding_id=? ORDER BY observed_at",
                (workspace_binding_id,),
            ).fetchall()
            writer = self._connection.execute(
                "SELECT claim_id, owner_id, claim_epoch, expires_at, status FROM workspace_writer_claims WHERE workspace_binding_id=?",
                (workspace_binding_id,),
            ).fetchone()
            barriers = self._connection.execute(
                "SELECT barrier_id, claim_id, reason, status FROM workspace_recovery_barriers WHERE workspace_binding_id=? AND status='active'",
                (workspace_binding_id,),
            ).fetchall()
            unfinished = self._connection.execute(
                """SELECT runtime_execution_id, legacy_session_id FROM runtime_executions
                   JOIN sessions ON sessions.id=legacy_session_id
                   WHERE workspace_binding_id=? AND sessions.state NOT IN ('completed','failed','cancelled')""",
                (workspace_binding_id,),
            ).fetchall()
        return {
            "workspace_binding_id": workspace_binding_id, "locator": str(binding["locator"]),
            "binding_kind": str(binding["binding_kind"]),
            "observations": [dict(row) for row in observations],
            "active_writer_claim": dict(writer) if writer is not None and writer["status"] == "active" else None,
            "recovery_barriers": [dict(row) for row in barriers],
            "unfinished_executions": [dict(row) for row in unfinished],
            "discovery_mode": "read_only",
        }

    def register_project_scope(self, repository_id: str, relative_path: str = ".") -> ProjectScope:
        if not relative_path or Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
            raise ValueError("project scope must be a repository-relative path")
        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                if self.get_repository_identity(repository_id) is None:
                    raise ProductLifecycleConflict("repository identity was not found")
                row = self._connection.execute(
                    "SELECT project_scope_id FROM project_scopes WHERE repository_id=? AND relative_path=?",
                    (repository_id, relative_path),
                ).fetchone()
                if row is None:
                    scope_id = str(uuid.uuid4())
                    self._connection.execute(
                        "INSERT INTO project_scopes(project_scope_id, repository_id, relative_path, created_at) VALUES (?, ?, ?, ?)",
                        (scope_id, repository_id, relative_path, timestamp),
                    )
                else:
                    scope_id = str(row["project_scope_id"])
                return ProjectScope(scope_id, repository_id, relative_path)

    def register_workspace_binding(
        self, *, repository_id: str, project_scope_id: str, binding_kind: str,
        locator: str, observation: Mapping[str, object] | None = None,
    ) -> WorkspaceBinding:
        if not binding_kind or not locator:
            raise ValueError("workspace binding kind and locator are required")
        if binding_kind == "m2_direct_read_only":
            try:
                resolved_locator = str(Path(locator).resolve(strict=True))
            except OSError as exc:
                raise ProductLifecycleConflict("direct WorkspaceBinding locator is unavailable") from exc
            if not Path(resolved_locator).is_dir():
                raise ProductLifecycleConflict("direct WorkspaceBinding locator must be a directory")
            if observation is None or observation.get("canonical_path") != resolved_locator:
                raise ProductLifecycleConflict("direct WorkspaceBinding observation must match its concrete locator")
            locator = resolved_locator
        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                scope = self._connection.execute(
                    "SELECT repository_id FROM project_scopes WHERE project_scope_id=?", (project_scope_id,)
                ).fetchone()
                if scope is None or str(scope["repository_id"]) != repository_id:
                    raise ProductLifecycleConflict("workspace binding must use a compatible project scope")
                row = self._connection.execute(
                    """SELECT workspace_binding_id FROM workspace_bindings
                       WHERE repository_id=? AND project_scope_id=? AND binding_kind=? AND locator=?""",
                    (repository_id, project_scope_id, binding_kind, locator),
                ).fetchone()
                binding_id = str(row["workspace_binding_id"]) if row is not None else str(uuid.uuid4())
                if row is None:
                    self._connection.execute(
                        """INSERT INTO workspace_bindings(workspace_binding_id, repository_id,
                           project_scope_id, binding_kind, locator, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                        (binding_id, repository_id, project_scope_id, binding_kind, locator, timestamp),
                    )
                if observation is not None:
                    serialized = _json_dumps(dict(observation))
                    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
                    self._connection.execute(
                        """INSERT OR IGNORE INTO workspace_binding_observations(observation_id,
                           workspace_binding_id, observation_digest, observation_json, observed_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (str(uuid.uuid4()), binding_id, digest, serialized, timestamp),
                    )
                return WorkspaceBinding(binding_id, repository_id, project_scope_id, binding_kind, locator)

    def _capture_direct_binding_observation(
        self, workspace_binding_id: str,
    ) -> tuple[dict[str, object] | None, sqlite3.Row]:
        """Capture direct-tree facts outside any SQLite write transaction."""
        with self._lock:
            binding = self._connection.execute(
                "SELECT repository_id, project_scope_id, binding_kind, locator FROM workspace_bindings WHERE workspace_binding_id=?",
                (workspace_binding_id,),
            ).fetchone()
            previous = self._connection.execute(
                """SELECT observation_json FROM workspace_binding_observations
                   WHERE workspace_binding_id=? ORDER BY observed_at DESC, rowid DESC LIMIT 1""",
                (workspace_binding_id,),
            ).fetchone()
        if binding is None:
            raise ProductLifecycleConflict("workspace binding was not found")
        if str(binding["binding_kind"]) != "m2_direct_read_only":
            return None, binding
        from coding_agent.product_workspace import observe_direct_workspace

        try:
            fresh = observe_direct_workspace(str(binding["locator"])).to_dict()
        except (OSError, ProductLifecycleConflict) as exc:
            raise ProductLifecycleConflict("direct workspace observation is unavailable") from exc
        if previous is not None:
            stored = _json_loads(str(previous["observation_json"]), description="binding observation")
            if not isinstance(stored, Mapping):
                raise PersistenceError("direct workspace observation is corrupt")
            topology_keys = ("canonical_path", "repository_root", "git_layout", "git_common_dir")
            if any(stored.get(key) != fresh.get(key) for key in topology_keys):
                raise ProductLifecycleConflict("direct workspace identity or topology has drifted")
        return dict(fresh), binding

    def _persist_binding_observation_in_transaction(
        self, workspace_binding_id: str, observation: Mapping[str, object], timestamp: str,
    ) -> str:
        serialized = _json_dumps(dict(observation))
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        self._connection.execute(
            """INSERT OR IGNORE INTO workspace_binding_observations(observation_id,
               workspace_binding_id, observation_digest, observation_json, observed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), workspace_binding_id, digest, serialized, timestamp),
        )
        return digest

    def admit_turn(
        self,
        *,
        operation_id: str,
        payload_digest: str,
        initial_request: str,
        repository_id: str,
        project_scope_id: str,
        workspace_binding_id: str,
        snapshot: RuntimeSnapshot,
        policy: RunPolicy,
        conversation_id: str | None = None,
        expected_conversation_version: int | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> TurnAdmission:
        """Atomically publish the complete M2 Turn Admission bundle.

        No filesystem, provider, ToolHarness, or Runtime transition occurs in
        this method.  The legacy Session/checkpoint/events are inserted only as
        one member of the Product transaction.
        """
        if not operation_id or not payload_digest or not initial_request:
            raise ValueError("M2 admission operation, digest, and request are required")
        expected_digest = self.canonical_admission_digest(
            initial_request=initial_request, repository_id=repository_id,
            project_scope_id=project_scope_id, workspace_binding_id=workspace_binding_id,
            snapshot=snapshot, policy=policy, conversation_id=conversation_id,
            expected_conversation_version=expected_conversation_version,
        )
        if payload_digest != expected_digest:
            raise ProductAdmissionConflict("admission payload digest is not canonical")
        if snapshot.version != 0 or snapshot.state is not RuntimeState.CREATED:
            raise InvariantViolation("M2 admission must create a fresh Runtime Session")
        if snapshot.policy != policy:
            raise InvariantViolation("M2 admission policy must match the Session policy")
        with self._lock:
            existing = self._read_admission_in_transaction(operation_id)
            if existing is not None:
                if existing.payload_digest != payload_digest:
                    raise ProductAdmissionConflict("admission operation id was reused with a different digest")
                return TurnAdmission(**{**existing.__dict__, "idempotent": True})
        fresh_observation, captured_binding = self._capture_direct_binding_observation(
            workspace_binding_id
        )
        timestamp = self.clock()
        with self._lock:
            try:
                with self._write_transaction():
                    existing = self._read_admission_in_transaction(operation_id)
                    if existing is not None:
                        if existing.payload_digest != payload_digest:
                            raise ProductAdmissionConflict("admission operation id was reused with a different digest")
                        return TurnAdmission(**{**existing.__dict__, "idempotent": True})

                    binding = self._connection.execute(
                        """
                        SELECT repository_id, project_scope_id, binding_kind, locator FROM workspace_bindings
                        WHERE workspace_binding_id=?
                        """, (workspace_binding_id,)
                    ).fetchone()
                    scope = self._connection.execute(
                        "SELECT repository_id FROM project_scopes WHERE project_scope_id=?",
                        (project_scope_id,),
                    ).fetchone()
                    if binding is None or scope is None or str(binding["repository_id"]) != repository_id \
                        or str(scope["repository_id"]) != repository_id \
                        or str(binding["project_scope_id"]) != project_scope_id:
                        raise ProductLifecycleConflict("admission workspace binding is not compatible with repository scope")
                    if (
                        str(binding["binding_kind"]) != str(captured_binding["binding_kind"])
                        or str(binding["locator"]) != str(captured_binding["locator"])
                    ):
                        raise ProductLifecycleConflict("workspace binding changed during admission observation")

                    if fault_hook is not None:
                        fault_hook("before_conversation_publication")
                    if conversation_id is None:
                        conversation_id = str(uuid.uuid4())
                        ordinal = 1
                        conversation_version = 0
                        self._connection.execute(
                            """
                            INSERT INTO conversations(
                                conversation_id, repository_id, project_scope_id,
                                default_workspace_binding_id, provenance_kind, created_at,
                                product_version, open_turn_id
                            ) VALUES (?, ?, ?, ?, 'm2_admitted', ?, 0, NULL)
                            """,
                            (conversation_id, repository_id, project_scope_id, workspace_binding_id, timestamp),
                        )
                    else:
                        if expected_conversation_version is None:
                            raise ProductLifecycleConflict("existing Conversation admission requires an expected version")
                        conversation = self._connection.execute(
                            """
                            SELECT repository_id, project_scope_id, default_workspace_binding_id,
                                   product_version, open_turn_id
                            FROM conversations WHERE conversation_id=?
                            """, (conversation_id,),
                        ).fetchone()
                        if conversation is None:
                            raise ProductLifecycleConflict("conversation was not found")
                        if str(conversation["repository_id"]) != repository_id or str(conversation["project_scope_id"]) != project_scope_id:
                            raise ProductLifecycleConflict("conversation cannot cross repository or scope")
                        if str(conversation["default_workspace_binding_id"]) != workspace_binding_id:
                            raise ProductLifecycleConflict("existing Conversation admission must use its current default binding")
                        conversation_version = int(conversation["product_version"])
                        if expected_conversation_version is not None and conversation_version != expected_conversation_version:
                            raise ProductLifecycleConflict("conversation version precondition failed")
                        if conversation["open_turn_id"] is not None:
                            raise ProductLifecycleConflict("conversation already has an open Turn")
                        ordinal = int(self._connection.execute(
                            "SELECT COUNT(*) FROM turns WHERE conversation_id=?", (conversation_id,)
                        ).fetchone()[0]) + 1
                    if fault_hook is not None:
                        fault_hook("after_conversation_publication")

                    turn_id = str(uuid.uuid4())
                    runtime_execution_id = str(uuid.uuid4())
                    if fresh_observation is not None:
                        observation_digest = self._persist_binding_observation_in_transaction(
                            workspace_binding_id, fresh_observation, timestamp,
                        )
                        frontier = str(fresh_observation.get("observation_frontier", "incomplete_observation"))
                        raw_exclusions = fresh_observation.get("exclusions", {})
                        raw_git_facts = fresh_observation.get("git_facts", {})
                        exclusions = dict(raw_exclusions) if isinstance(raw_exclusions, Mapping) else {"observation": "invalid_exclusions"}
                        baseline = {
                            "workspace_binding_id": workspace_binding_id,
                            "binding_observation_digest": observation_digest,
                            "git_facts": dict(raw_git_facts) if isinstance(raw_git_facts, Mapping) else {},
                            "observed_entries": fresh_observation.get("observed_entries", 0),
                            "observed_bytes": fresh_observation.get("observed_bytes", 0),
                        }
                    else:
                        observation = self._connection.execute(
                            """SELECT observation_digest, observation_json FROM workspace_binding_observations
                               WHERE workspace_binding_id=? ORDER BY observed_at DESC, rowid DESC LIMIT 1""",
                            (workspace_binding_id,),
                        ).fetchone()
                        if observation is None:
                            raise ProductLifecycleConflict("admission requires a pre-captured read-only binding observation")
                        observation_digest = str(observation["observation_digest"])
                        raw_observation = _json_loads(str(observation["observation_json"]), description="binding observation")
                        frontier = str(raw_observation.get("observation_frontier", "incomplete_observation")) if isinstance(raw_observation, Mapping) else "incomplete_observation"
                        exclusions = dict(raw_observation.get("exclusions", {})) if isinstance(raw_observation, Mapping) else {"observation": "unavailable"}
                        baseline = {"workspace_binding_id": workspace_binding_id, "binding_observation_digest": observation_digest}
                    checkpoint = TurnStartCodeCheckpoint(
                        str(uuid.uuid4()), observation_digest, "m2_observation_only",
                        "no_m4_content_or_undo_baseline", frontier,
                        baseline,
                        {**exclusions, "m3_instruction_protection": "unimplemented", "m4_mutation_coverage": "non_restorable"},
                    )
                    manifest = InstructionManifest(str(uuid.uuid4()), "m2_placeholder_no_discovery")
                    epoch = PolicyEpoch("m2-read-only-direct-tree-v1", "m2_read_only_direct_tree")
                    if fault_hook is not None:
                        fault_hook("before_session")
                    self._insert_new_session(
                        snapshot, Message(role="user", content=initial_request),
                        session_created_payload={"product_admission_operation_id": operation_id},
                        fault_hook=fault_hook,
                    )
                    if fault_hook is not None:
                        fault_hook("after_session")
                    if fault_hook is not None:
                        fault_hook("before_turn")
                    self._connection.execute(
                        """INSERT INTO turns(turn_id, conversation_id, ordinal, provenance_kind, created_at)
                           VALUES (?, ?, ?, 'm2_admitted', ?)""",
                        (turn_id, conversation_id, ordinal, timestamp),
                    )
                    if fault_hook is not None:
                        fault_hook("after_turn")
                    if fault_hook is not None:
                        fault_hook("before_runtime_execution")
                    self._connection.execute(
                        """INSERT INTO runtime_executions(runtime_execution_id, legacy_session_id, turn_id,
                           workspace_binding_id, created_at) VALUES (?, ?, ?, ?, ?)""",
                        (runtime_execution_id, snapshot.session_id, turn_id, workspace_binding_id, timestamp),
                    )
                    if fault_hook is not None:
                        fault_hook("after_runtime_execution")
                    if fault_hook is not None:
                        fault_hook("before_admission_artifacts")
                    self._connection.execute(
                        """INSERT INTO turn_admission_artifacts(turn_id, checkpoint_id, checkpoint_json,
                           instruction_manifest_id, instruction_manifest_json, policy_epoch_id, policy_json, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (turn_id, checkpoint.checkpoint_id, _json_dumps(checkpoint.__dict__),
                         manifest.instruction_manifest_id, _json_dumps(manifest.__dict__),
                         epoch.policy_epoch_id, _json_dumps(epoch.__dict__), timestamp),
                    )
                    if fault_hook is not None:
                        fault_hook("after_admission_artifacts")
                    if fault_hook is not None:
                        fault_hook("before_initial_input")
                    input_sequence = int(self._connection.execute(
                        "SELECT COUNT(*) FROM product_inputs WHERE conversation_id=?", (conversation_id,)
                    ).fetchone()[0]) + 1
                    self._connection.execute(
                        """INSERT INTO product_inputs(input_id, operation_id, payload_digest, conversation_id,
                           turn_id, sequence, input_kind, correlation_id, payload_json, status, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, 'initial_request', NULL, ?, 'accepted', ?)""",
                        (str(uuid.uuid4()), operation_id, payload_digest, conversation_id, turn_id,
                         input_sequence, _json_dumps({"text": initial_request}), timestamp),
                    )
                    if fault_hook is not None:
                        fault_hook("after_initial_input")
                        fault_hook("before_admission_record")
                    self._connection.execute(
                        """INSERT INTO product_admissions(operation_id, payload_digest, conversation_id, turn_id,
                           runtime_execution_id, legacy_session_id, checkpoint_id, instruction_manifest_id,
                           policy_epoch_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (operation_id, payload_digest, conversation_id, turn_id, runtime_execution_id,
                         snapshot.session_id, checkpoint.checkpoint_id, manifest.instruction_manifest_id,
                         epoch.policy_epoch_id, timestamp),
                    )
                    if fault_hook is not None:
                        fault_hook("after_admission_record")
                    if fault_hook is not None:
                        fault_hook("before_conversation_update")
                    self._connection.execute(
                        "UPDATE conversations SET product_version=?, open_turn_id=? WHERE conversation_id=?",
                        (conversation_version + 1, turn_id, conversation_id),
                    )
                    if fault_hook is not None:
                        fault_hook("after_conversation_update")
                        fault_hook("before_initial_request_accepted_event")
                    self._append_conversation_semantic_event(
                        conversation_id, "initial_request_accepted", "m2_admission",
                        {"operation_id": operation_id, "turn_id": turn_id}, timestamp,
                    )
                    if fault_hook is not None:
                        fault_hook("after_initial_request_accepted_event")
                        fault_hook("before_turn_admitted_event")
                    self._append_conversation_semantic_event(
                        conversation_id, "turn_admitted", "m2_admission",
                        {"operation_id": operation_id, "turn_id": turn_id}, timestamp,
                    )
                    if fault_hook is not None:
                        fault_hook("after_turn_admitted_event")
                        fault_hook("after_semantic_events")
                        fault_hook("before_precommit")
                        fault_hook("before_commit")
                    self._invoke_commit_hook()
                    if fault_hook is not None:
                        fault_hook("after_precommit")
                    return TurnAdmission(operation_id, payload_digest, conversation_id, turn_id,
                                         runtime_execution_id, snapshot.session_id, checkpoint, manifest, epoch)
            except sqlite3.IntegrityError as exc:
                raise PersistenceError("failed to atomically admit M2 Turn") from exc

    def append_product_input(
        self, *, operation_id: str, payload_digest: str, conversation_id: str,
        input_kind: str, payload: Mapping[str, object], expected_conversation_version: int,
        correlation_id: str | None = None,
    ) -> str:
        if input_kind not in {"steering", "reply", "cancel"}:
            raise ValueError("M2 accepts only steering, reply, or cancel controls here")
        expected_digest = self.canonical_input_digest(
            conversation_id=conversation_id, input_kind=input_kind, payload=payload,
            correlation_id=correlation_id, expected_conversation_version=expected_conversation_version,
        )
        if payload_digest != expected_digest:
            raise ProductAdmissionConflict("input payload digest is not canonical")
        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                previous = self._connection.execute(
                    "SELECT input_id, payload_digest FROM product_inputs WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if previous is not None:
                    if str(previous["payload_digest"]) != payload_digest:
                        raise ProductAdmissionConflict("input operation id was reused with a different digest")
                    return str(previous["input_id"])
                conversation = self._connection.execute(
                    "SELECT open_turn_id, product_version FROM conversations WHERE conversation_id=?", (conversation_id,)
                ).fetchone()
                if conversation is None:
                    raise ProductLifecycleConflict("conversation was not found")
                if int(conversation["product_version"]) != expected_conversation_version:
                    raise ProductLifecycleConflict("conversation version precondition failed")
                turn_id = conversation["open_turn_id"]
                if turn_id is None:
                    raise ProductLifecycleConflict("control input requires an open Turn")
                if input_kind == "reply":
                    if not correlation_id:
                        raise ProductLifecycleConflict("UserReply requires an outstanding ordinary-input correlation")
                    outstanding = self._connection.execute(
                        """SELECT input_id FROM product_inputs
                           WHERE input_id=? AND conversation_id=? AND turn_id=?
                             AND input_kind='ordinary_input_request' AND status='outstanding'""",
                        (correlation_id, conversation_id, str(turn_id)),
                    ).fetchone()
                    if outstanding is None:
                        raise ProductLifecycleConflict("UserReply does not match an outstanding ordinary input request")
                    self._connection.execute(
                        "UPDATE product_inputs SET status='resolved' WHERE input_id=?", (correlation_id,)
                    )
                sequence = int(self._connection.execute(
                    "SELECT COUNT(*) FROM product_inputs WHERE conversation_id=?", (conversation_id,)
                ).fetchone()[0]) + 1
                input_id = str(uuid.uuid4())
                self._connection.execute(
                    """INSERT INTO product_inputs(input_id, operation_id, payload_digest, conversation_id,
                       turn_id, sequence, input_kind, correlation_id, payload_json, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?)""",
                    (input_id, operation_id, payload_digest, conversation_id, turn_id, sequence,
                     input_kind, correlation_id, _json_dumps(dict(payload)), timestamp),
                )
                self._connection.execute(
                    "UPDATE conversations SET product_version=product_version+1 WHERE conversation_id=?", (conversation_id,)
                )
                self._append_conversation_semantic_event(
                    conversation_id, f"{input_kind}_accepted", "m2_typed_input",
                    {"operation_id": operation_id, "input_id": input_id, "turn_id": str(turn_id)}, timestamp,
                )
                if input_kind == "reply":
                    execution = self._connection.execute(
                        "SELECT legacy_session_id FROM runtime_executions WHERE turn_id=?", (str(turn_id),)
                    ).fetchone()
                    if execution is None:
                        raise PersistenceError("open Turn has no RuntimeExecution")
                    self._transition_admitted_session_in_transaction(
                        str(execution["legacy_session_id"]), RuntimeState.BUILDING_CONTEXT,
                        "ordinary_input_replied", {"request_id": correlation_id, "reply_input_id": input_id},
                        message=Message(role="user", content=str(payload.get("text", "")), metadata={
                            "m2_input_id": input_id, "m2_input_kind": "reply", "correlation_id": correlation_id,
                        }),
                    )
                    self._connection.execute(
                        "UPDATE product_inputs SET status='consumed' WHERE input_id=?",
                        (input_id,),
                    )
                return input_id

    def apply_cancel_at_safe_boundary(
        self, *, conversation_id: str, legacy_session_id: str, operation_id: str,
        expected_conversation_version: int,
    ) -> None:
        """Turn a prior cancel intent into terminal cancellation only when safe."""
        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                execution = self._connection.execute(
                    "SELECT turn_id, workspace_binding_id FROM runtime_executions WHERE legacy_session_id=?",
                    (legacy_session_id,),
                ).fetchone()
                if execution is None:
                    raise ProductLifecycleConflict("RuntimeExecution was not found")
                conversation = self._connection.execute(
                    "SELECT product_version FROM conversations WHERE conversation_id=?", (conversation_id,)
                ).fetchone()
                if conversation is None or int(conversation["product_version"]) != expected_conversation_version:
                    raise ProductLifecycleConflict("conversation version precondition failed")
                intent = self._connection.execute(
                    """SELECT input_id FROM product_inputs WHERE conversation_id=? AND turn_id=?
                       AND operation_id=? AND input_kind='cancel' AND status='accepted'""",
                    (conversation_id, str(execution["turn_id"]), operation_id),
                ).fetchone()
                if intent is None:
                    raise ProductLifecycleConflict("cancel intent was not accepted for this open Turn")
                barrier = self._connection.execute(
                    "SELECT 1 FROM workspace_recovery_barriers WHERE workspace_binding_id=? AND status='active'",
                    (str(execution["workspace_binding_id"]),),
                ).fetchone()
                if barrier is not None:
                    raise ProductLifecycleConflict("unknown effect blocks terminal cancellation")
                self._transition_admitted_session_in_transaction(
                    legacy_session_id, RuntimeState.CANCELLED, "cancel_safe_boundary",
                    {"cancel_input_id": str(intent["input_id"])},
                )
                self._connection.execute("UPDATE product_inputs SET status='consumed' WHERE input_id=?", (str(intent["input_id"]),))
                self._append_conversation_semantic_event(
                    conversation_id, "turn_cancelled", "m2_cancel_safe_boundary",
                    {"operation_id": operation_id, "legacy_session_id": legacy_session_id}, timestamp,
                )
                admission = self._connection.execute(
                    "SELECT operation_id FROM product_admissions WHERE legacy_session_id=?", (legacy_session_id,),
                ).fetchone()
                if admission is None:
                    raise PersistenceError("cancelled execution has no admission")
                self._finalize_admitted_turn_in_transaction(
                    str(admission["operation_id"]), timestamp, "m2_cancel_safe_boundary",
                )

    def request_ordinary_input(
        self, *, operation_id: str, payload_digest: str, conversation_id: str,
        request_id: str, payload: Mapping[str, object], expected_conversation_version: int,
    ) -> str:
        """Persist a non-permission ordinary-input wait for an open Turn.

        This is a Product lifecycle correlation record only.  It neither
        changes the Runtime FSM nor resolves M4 permission/reconciliation
        domains; a later correlated ``UserReply`` is the sole resolver.
        """
        if not request_id:
            raise ValueError("ordinary input request id is required")
        expected_digest = self.canonical_input_digest(
            conversation_id=conversation_id, input_kind="ordinary_input_request", payload=payload,
            correlation_id=request_id, expected_conversation_version=expected_conversation_version,
        )
        if payload_digest != expected_digest:
            raise ProductAdmissionConflict("ordinary input payload digest is not canonical")
        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                previous = self._connection.execute(
                    "SELECT input_id, payload_digest FROM product_inputs WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if previous is not None:
                    if str(previous["payload_digest"]) != payload_digest:
                        raise ProductAdmissionConflict("ordinary input operation id was reused with a different digest")
                    return str(previous["input_id"])
                conversation = self._connection.execute(
                    "SELECT open_turn_id, product_version FROM conversations WHERE conversation_id=?", (conversation_id,)
                ).fetchone()
                if conversation is None or conversation["open_turn_id"] is None:
                    raise ProductLifecycleConflict("ordinary input request requires an open Turn")
                if int(conversation["product_version"]) != expected_conversation_version:
                    raise ProductLifecycleConflict("conversation version precondition failed")
                existing_request = self._connection.execute(
                    "SELECT input_id FROM product_inputs WHERE input_id=?", (request_id,)
                ).fetchone()
                if existing_request is not None:
                    raise ProductAdmissionConflict("ordinary input request id is already in use")
                turn_id = str(conversation["open_turn_id"])
                sequence = int(self._connection.execute(
                    "SELECT COUNT(*) FROM product_inputs WHERE conversation_id=?", (conversation_id,)
                ).fetchone()[0]) + 1
                self._connection.execute(
                    """INSERT INTO product_inputs(input_id, operation_id, payload_digest, conversation_id,
                       turn_id, sequence, input_kind, correlation_id, payload_json, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'ordinary_input_request', ?, ?, 'outstanding', ?)""",
                    (request_id, operation_id, payload_digest, conversation_id, turn_id, sequence,
                     request_id, _json_dumps(dict(payload)), timestamp),
                )
                self._connection.execute(
                    "UPDATE conversations SET product_version=product_version+1 WHERE conversation_id=?", (conversation_id,)
                )
                self._append_conversation_semantic_event(
                    conversation_id, "ordinary_input_requested", "m2_waiting_user_input",
                    {"operation_id": operation_id, "request_id": request_id, "turn_id": turn_id}, timestamp,
                )
                execution = self._connection.execute(
                    "SELECT legacy_session_id FROM runtime_executions WHERE turn_id=?", (turn_id,)
                ).fetchone()
                if execution is None:
                    raise PersistenceError("open Turn has no RuntimeExecution")
                self._transition_admitted_session_in_transaction(
                    str(execution["legacy_session_id"]), RuntimeState.WAITING_USER_INPUT,
                    "ordinary_input_requested", {"request_id": request_id},
                )
                return request_id

    def consume_steering_inputs(
        self, *, conversation_id: str, legacy_session_id: str, expected_conversation_version: int,
    ) -> list[str]:
        """Acknowledge steering only at a durable non-tool Runtime safe boundary."""
        return self._consume_steering_inputs(
            conversation_id=conversation_id,
            legacy_session_id=legacy_session_id,
            expected_conversation_version=expected_conversation_version,
        )

    def consume_current_steering_inputs(
        self, *, conversation_id: str, legacy_session_id: str,
    ) -> list[str]:
        """Atomically select and consume the current Runtime-boundary batch.

        Unlike the external optimistic-concurrency API, this internal boundary
        deliberately reads the current Conversation version inside the same
        transaction as batch selection.  A concurrently accepted steering
        input therefore lands either in this ordered batch or the next safe
        boundary; it cannot turn a valid Runtime execution into FAILED merely
        because an earlier version was observed.
        """
        return self._consume_steering_inputs(
            conversation_id=conversation_id,
            legacy_session_id=legacy_session_id,
            expected_conversation_version=None,
        )

    def _consume_steering_inputs(
        self, *, conversation_id: str, legacy_session_id: str,
        expected_conversation_version: int | None,
    ) -> list[str]:
        """Consume one ordered batch, optionally enforcing a caller version."""
        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                execution = self._connection.execute(
                    """SELECT executions.turn_id, sessions.state FROM runtime_executions executions
                       JOIN sessions ON sessions.id=executions.legacy_session_id
                       WHERE executions.legacy_session_id=?""", (legacy_session_id,),
                ).fetchone()
                if execution is None:
                    raise ProductLifecycleConflict("RuntimeExecution was not found")
                conversation = self._connection.execute(
                    "SELECT product_version FROM conversations WHERE conversation_id=?", (conversation_id,)
                ).fetchone()
                if conversation is None:
                    raise ProductLifecycleConflict("conversation was not found")
                if (
                    expected_conversation_version is not None
                    and int(conversation["product_version"]) != expected_conversation_version
                ):
                    raise ProductLifecycleConflict("conversation version precondition failed")
                if RuntimeState(str(execution["state"])) not in {
                    RuntimeState.CREATED, RuntimeState.BUILDING_CONTEXT,
                }:
                    raise ProductLifecycleConflict("steering may be consumed only at an M2 Runtime safe boundary")
                rows = self._connection.execute(
                    """SELECT input_id, payload_json FROM product_inputs WHERE conversation_id=? AND turn_id=?
                       AND input_kind='steering' AND status='accepted' ORDER BY sequence""",
                    (conversation_id, str(execution["turn_id"])),
                ).fetchall()
                input_ids = [str(row["input_id"]) for row in rows]
                for row in rows:
                    input_id = str(row["input_id"])
                    payload = _json_loads(str(row["payload_json"]), description="steering input")
                    if not isinstance(payload, Mapping) or not isinstance(payload.get("text"), str):
                        raise PersistenceError("steering input payload is corrupt")
                    self._append_runtime_message_in_transaction(
                        legacy_session_id,
                        Message(role="user", content=str(payload["text"]), metadata={
                            "m2_input_id": input_id, "m2_input_kind": "steering",
                        }),
                        {"input_id": input_id, "kind": "steering"},
                    )
                    self._connection.execute("UPDATE product_inputs SET status='consumed' WHERE input_id=?", (input_id,))
                    self._append_conversation_semantic_event(
                        conversation_id, "steering_consumed", "m2_runtime_safe_boundary",
                        {"input_id": input_id, "legacy_session_id": legacy_session_id}, timestamp,
                    )
                return input_ids

    def _append_conversation_semantic_event(
        self, conversation_id: str, event_type: str, provenance_kind: str,
        payload: Mapping[str, object], timestamp: str,
    ) -> None:
        sequence = int(self._connection.execute(
            "SELECT COUNT(*) FROM conversation_semantic_events WHERE conversation_id=?", (conversation_id,)
        ).fetchone()[0]) + 1
        self._connection.execute(
            """INSERT INTO conversation_semantic_events(conversation_event_id, conversation_id,
               sequence, event_type, provenance_kind, provenance_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), conversation_id, sequence, event_type, provenance_kind,
             _json_dumps(dict(payload)), timestamp),
        )

    def _transition_admitted_session_in_transaction(
        self, session_id: str, target: RuntimeState, reason: str,
        payload: Mapping[str, object], message: Message | None = None,
    ) -> None:
        """Apply an admitted Runtime transition using the shared state-machine authority."""
        snapshot_row = self._connection.execute(
            "SELECT snapshot_json FROM checkpoints WHERE session_id=?", (session_id,)
        ).fetchone()
        if snapshot_row is None:
            raise PersistenceError("admitted Session has no checkpoint")
        snapshot = RuntimeSnapshot.from_json(str(snapshot_row["snapshot_json"]))
        target_snapshot = RuntimeStateMachine.transition_snapshot(snapshot, target)
        self._commit_in_open_transaction(
            JournalMutation(
                session_id=session_id, expected_version=snapshot.version,
                expected_state=snapshot.state, snapshot_after=target_snapshot,
                event_type=EventType.STATE_TRANSITION,
                payload={"from": snapshot.state.value, "to": target.value, "reason": reason, **dict(payload)},
                message_to_append=message,
            ), self.clock(),
        )

    def _append_runtime_message_in_transaction(
        self, session_id: str, message: Message, payload: Mapping[str, object],
    ) -> None:
        """Durably deliver M2 ordered text without fabricating an FSM transition."""
        snapshot_row = self._connection.execute(
            "SELECT snapshot_json FROM checkpoints WHERE session_id=?", (session_id,)
        ).fetchone()
        if snapshot_row is None:
            raise PersistenceError("admitted Session has no checkpoint")
        snapshot = RuntimeSnapshot.from_json(str(snapshot_row["snapshot_json"]))
        self._commit_in_open_transaction(
            JournalMutation(
                session_id=session_id, expected_version=snapshot.version,
                expected_state=snapshot.state, snapshot_after=snapshot,
                event_type=EventType.MESSAGE_ADDED,
                payload={"m2_runtime_delivery": dict(payload)}, message_to_append=message,
            ), self.clock(),
        )

    def rebind_conversation(
        self, *, operation_id: str, payload_digest: str, conversation_id: str,
        workspace_binding_id: str, expected_version: int,
        observation: Mapping[str, object],
    ) -> int:
        """Audit an explicit compatible rebind only while no Turn is open."""
        canonical_digest = self.canonical_rebind_digest(
            conversation_id=conversation_id, workspace_binding_id=workspace_binding_id,
            expected_version=expected_version, observation=observation,
        )
        if payload_digest != canonical_digest:
            raise ProductAdmissionConflict("rebind payload digest is not canonical")
        with self._lock:
            existing = self._connection.execute(
                "SELECT payload_digest, resulting_version FROM conversation_rebinds WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_digest"]) != payload_digest:
                    raise ProductAdmissionConflict("rebind operation id was reused with a different digest")
                return int(existing["resulting_version"])
        fresh_observation, captured_binding = self._capture_direct_binding_observation(
            workspace_binding_id
        )
        if fresh_observation is not None and _json_dumps(dict(observation)) != _json_dumps(fresh_observation):
            raise ProductLifecycleConflict("rebind workspace observation is stale or caller-forged")
        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                existing = self._connection.execute(
                    "SELECT payload_digest, resulting_version FROM conversation_rebinds WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if existing is not None:
                    if str(existing["payload_digest"]) != payload_digest:
                        raise ProductAdmissionConflict("rebind operation id was reused with a different digest")
                    return int(existing["resulting_version"])
                row = self._connection.execute(
                    "SELECT repository_id, project_scope_id, default_workspace_binding_id, product_version, open_turn_id FROM conversations WHERE conversation_id=?", (conversation_id,)
                ).fetchone()
                binding = self._connection.execute(
                    "SELECT repository_id, project_scope_id, binding_kind, locator FROM workspace_bindings WHERE workspace_binding_id=?", (workspace_binding_id,)
                ).fetchone()
                if row is None or binding is None:
                    raise ProductLifecycleConflict("conversation or binding was not found")
                if row["open_turn_id"] is not None:
                    raise ProductLifecycleConflict("cannot rebind while a Turn is open")
                if int(row["product_version"]) != expected_version:
                    raise ProductLifecycleConflict("conversation version precondition failed")
                if str(row["repository_id"]) != str(binding["repository_id"]) or str(row["project_scope_id"]) != str(binding["project_scope_id"]):
                    raise ProductLifecycleConflict("rebind cannot cross repository or scope")
                if (
                    str(binding["binding_kind"]) != str(captured_binding["binding_kind"])
                    or str(binding["locator"]) != str(captured_binding["locator"])
                ):
                    raise ProductLifecycleConflict("workspace binding changed during rebind observation")
                if fresh_observation is not None:
                    self._persist_binding_observation_in_transaction(
                        workspace_binding_id, fresh_observation, timestamp,
                    )
                resulting = expected_version + 1
                self._connection.execute(
                    "UPDATE conversations SET default_workspace_binding_id=?, product_version=? WHERE conversation_id=?",
                    (workspace_binding_id, resulting, conversation_id),
                )
                self._connection.execute(
                    """INSERT INTO conversation_rebinds(rebind_id, operation_id, payload_digest,
                       conversation_id, previous_workspace_binding_id, workspace_binding_id,
                       expected_version, resulting_version, observation_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), operation_id, payload_digest, conversation_id,
                     str(row["default_workspace_binding_id"]), workspace_binding_id,
                     expected_version, resulting, _json_dumps(dict(observation)), timestamp),
                )
                return resulting

    def canonical_writer_claim_digest(
        self, *, workspace_binding_id: str, owner_id: str, expected_epoch: int,
        expected_observation_digest: str, observation: Mapping[str, object], lease_seconds: float,
        runtime_execution_id: str | None = None,
    ) -> str:
        return self.canonical_product_payload_digest({
            "kind": "writer_claim", "workspace_binding_id": workspace_binding_id,
            "owner_id": owner_id, "expected_epoch": expected_epoch,
            "expected_observation_digest": expected_observation_digest,
            "observation": dict(observation), "lease_seconds": lease_seconds,
            "runtime_execution_id": runtime_execution_id,
        })

    def _writer_receipt_in_transaction(
        self, operation_id: str, payload_digest: str, operation_kind: str,
    ) -> tuple[str, Mapping[str, object]] | None:
        row = self._connection.execute(
            """SELECT payload_digest, operation_kind, claim_id, result_json
               FROM workspace_writer_operation_receipts WHERE operation_id=?""", (operation_id,)
        ).fetchone()
        if row is None:
            return None
        if str(row["payload_digest"]) != payload_digest or str(row["operation_kind"]) != operation_kind:
            raise ProductAdmissionConflict("writer operation id was reused with a different payload")
        result = _json_loads(str(row["result_json"]), description="writer operation receipt")
        if not isinstance(result, Mapping):
            raise PersistenceError("writer operation receipt is corrupt")
        return str(row["claim_id"]), result

    def _record_writer_receipt_in_transaction(
        self, operation_id: str, payload_digest: str, operation_kind: str,
        claim_id: str, result: Mapping[str, object], timestamp: str,
    ) -> None:
        self._connection.execute(
            """INSERT INTO workspace_writer_operation_receipts(operation_id, payload_digest,
               operation_kind, claim_id, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
            (operation_id, payload_digest, operation_kind, claim_id, _json_dumps(dict(result)), timestamp),
        )

    def claim_workspace_writer(
        self, *, operation_id: str, payload_digest: str, workspace_binding_id: str,
        owner_id: str, expected_epoch: int, expected_observation_digest: str,
        lease_seconds: float, observation: Mapping[str, object], runtime_execution_id: str | None = None,
    ) -> WorkspaceWriterClaim:
        if not operation_id or not owner_id or expected_epoch < 0:
            raise ValueError("writer operation, owner, and non-negative expected epoch are required")
        if lease_seconds <= 0 or lease_seconds > self.MAX_WRITER_LEASE_SECONDS:
            raise ValueError("writer lease_seconds must be in (0, 300]")
        canonical = self.canonical_writer_claim_digest(
            workspace_binding_id=workspace_binding_id, owner_id=owner_id,
            expected_epoch=expected_epoch, expected_observation_digest=expected_observation_digest,
            observation=observation, lease_seconds=lease_seconds, runtime_execution_id=runtime_execution_id,
        )
        if payload_digest != canonical:
            raise ProductAdmissionConflict("writer claim digest is not canonical")
        with self._lock:
            receipt = self._writer_receipt_in_transaction(operation_id, payload_digest, "acquire")
            if receipt is not None:
                claim_id, result = receipt
                return WorkspaceWriterClaim(
                    claim_id, workspace_binding_id, str(result["owner_id"]),
                    int(str(result["claim_epoch"])), str(result["expires_at"]),
                )
        fresh_observation, captured_binding = self._capture_direct_binding_observation(
            workspace_binding_id
        )
        expires_at = self._lease_expires_at(lease_seconds)
        timestamp = self.clock()
        expired_barrier_created = False
        claim: WorkspaceWriterClaim | None = None
        with self._lock:
            with self._write_transaction():
                receipt = self._writer_receipt_in_transaction(operation_id, payload_digest, "acquire")
                if receipt is not None:
                    claim_id, result = receipt
                    return WorkspaceWriterClaim(
                        claim_id, workspace_binding_id, str(result["owner_id"]), int(str(result["claim_epoch"])), str(result["expires_at"]),
                    )
                if self._connection.execute("SELECT 1 FROM workspace_bindings WHERE workspace_binding_id=?", (workspace_binding_id,)).fetchone() is None:
                    raise ProductLifecycleConflict("workspace binding was not found")
                if runtime_execution_id is not None:
                    execution = self._connection.execute(
                        "SELECT workspace_binding_id FROM runtime_executions WHERE runtime_execution_id=?",
                        (runtime_execution_id,),
                    ).fetchone()
                    if execution is None or str(execution["workspace_binding_id"]) != workspace_binding_id:
                        raise ProductLifecycleConflict(
                            "writer execution ownership must name the immutable binding execution"
                        )
                latest_observation = self._connection.execute(
                    """SELECT observations.observation_digest, observations.observation_json,
                              bindings.locator, bindings.binding_kind
                       FROM workspace_binding_observations observations
                       JOIN workspace_bindings bindings ON bindings.workspace_binding_id=observations.workspace_binding_id
                       WHERE observations.workspace_binding_id=? ORDER BY observations.observed_at DESC LIMIT 1""",
                    (workspace_binding_id,),
                ).fetchone()
                if latest_observation is None or str(latest_observation["observation_digest"]) != expected_observation_digest:
                    raise LeaseConflict("writer claim observation precondition is stale or unavailable")
                if (
                    str(latest_observation["binding_kind"]) != str(captured_binding["binding_kind"])
                    or str(latest_observation["locator"]) != str(captured_binding["locator"])
                ):
                    raise LeaseConflict("writer claim binding changed during observation")
                stored = _json_loads(str(latest_observation["observation_json"]), description="writer observation")
                if fresh_observation is not None and (
                    not isinstance(stored, Mapping)
                    or _json_dumps(dict(stored)) != _json_dumps(fresh_observation)
                ):
                    raise LeaseConflict("writer claim workspace has live drift")
                barrier = self._connection.execute(
                    "SELECT barrier_id FROM workspace_recovery_barriers WHERE workspace_binding_id=? AND status='active'", (workspace_binding_id,)
                ).fetchone()
                if barrier is not None:
                    raise LeaseConflict("workspace recovery barrier requires durable reconciliation")
                row = self._connection.execute(
                    "SELECT claim_id, operation_id, payload_digest, owner_id, claim_epoch, expires_at, status FROM workspace_writer_claims WHERE workspace_binding_id=?", (workspace_binding_id,)
                ).fetchone()
                if row is not None and str(row["status"]) == "active":
                    if self._timestamp_epoch(str(row["expires_at"])) > self._lease_now():
                        if str(row["operation_id"]) == operation_id and str(row["payload_digest"]) == payload_digest:
                            return WorkspaceWriterClaim(str(row["claim_id"]), workspace_binding_id, owner_id, int(row["claim_epoch"]), str(row["expires_at"]))
                        if str(row["owner_id"]) != owner_id or int(row["claim_epoch"]) != expected_epoch:
                            raise LeaseConflict("workspace writer is already claimed")
                        raise LeaseConflict("active writer must renew rather than reacquire")
                    else:
                        # Expiry never grants a new writer by itself: persist a
                        # barrier and require the old claim's reconciliation.
                        self._connection.execute(
                            """INSERT INTO workspace_recovery_barriers(barrier_id, workspace_binding_id,
                               claim_id, reason, status, reconciliation_token, created_at, cleared_at)
                               VALUES (?, ?, ?, 'writer_claim_expired', 'active', NULL, ?, NULL)""",
                            (str(uuid.uuid4()), workspace_binding_id, str(row["claim_id"]), timestamp),
                        )
                        expired_barrier_created = True
                if not expired_barrier_created:
                    current_epoch = int(row["claim_epoch"]) if row is not None else 0
                    if current_epoch != expected_epoch:
                        raise LeaseConflict("writer claim epoch is stale")
                    epoch = current_epoch + 1
                    claim_id = str(uuid.uuid4())
                    self._connection.execute(
                    """INSERT INTO workspace_writer_claims(workspace_binding_id, claim_id, operation_id,
                       payload_digest, owner_id, runtime_execution_id, claim_epoch, expires_at, status, observation_digest,
                       observation_json, created_at, released_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL)
                       ON CONFLICT(workspace_binding_id) DO UPDATE SET claim_id=excluded.claim_id,
                       operation_id=excluded.operation_id, payload_digest=excluded.payload_digest,
                       owner_id=excluded.owner_id, runtime_execution_id=excluded.runtime_execution_id, claim_epoch=excluded.claim_epoch, expires_at=excluded.expires_at,
                       status='active', observation_digest=excluded.observation_digest,
                       observation_json=excluded.observation_json, created_at=excluded.created_at, released_at=NULL""",
                    (workspace_binding_id, claim_id, operation_id, payload_digest, owner_id, runtime_execution_id, epoch, expires_at,
                     expected_observation_digest,
                     _json_dumps(fresh_observation if fresh_observation is not None else dict(observation)),
                     timestamp),
                    )
                    claim = WorkspaceWriterClaim(claim_id, workspace_binding_id, owner_id, epoch, expires_at)
                    self._record_writer_receipt_in_transaction(
                        operation_id, payload_digest, "acquire", claim_id,
                        {"owner_id": owner_id, "claim_epoch": epoch, "expires_at": expires_at}, timestamp,
                    )
        if expired_barrier_created:
            raise LeaseConflict("expired writer claim requires durable reconciliation")
        if claim is None:
            raise PersistenceError("writer claim publication did not produce a claim")
        return claim

    def release_workspace_writer(
        self, claim_id: str, *, operation_id: str, payload_digest: str, owner_id: str,
        expected_epoch: int, expected_observation_digest: str,
    ) -> None:
        canonical = self.canonical_product_payload_digest({
            "kind": "writer_release", "claim_id": claim_id, "owner_id": owner_id,
            "expected_epoch": expected_epoch, "expected_observation_digest": expected_observation_digest,
        })
        if payload_digest != canonical:
            raise ProductAdmissionConflict("writer release digest is not canonical")
        with self._lock:
            receipt = self._writer_receipt_in_transaction(operation_id, payload_digest, "release")
            if receipt is not None:
                if receipt[0] != claim_id:
                    raise ProductAdmissionConflict("writer release receipt claim does not match")
                return
            claim_row = self._connection.execute(
                "SELECT workspace_binding_id FROM workspace_writer_claims WHERE claim_id=?",
                (claim_id,),
            ).fetchone()
        if claim_row is None:
            raise LeaseConflict("writer claim was not found")
        fresh_observation, captured_binding = self._capture_direct_binding_observation(
            str(claim_row["workspace_binding_id"])
        )
        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                receipt = self._writer_receipt_in_transaction(operation_id, payload_digest, "release")
                if receipt is not None:
                    if receipt[0] != claim_id:
                        raise ProductAdmissionConflict("writer release receipt claim does not match")
                    return
                row = self._connection.execute(
                    "SELECT workspace_binding_id, owner_id, claim_epoch FROM workspace_writer_claims WHERE claim_id=? AND status='active'", (claim_id,)
                ).fetchone()
                if row is None or str(row["owner_id"]) != owner_id or int(row["claim_epoch"]) != expected_epoch:
                    raise LeaseConflict("writer release owner or epoch is stale")
                observed = self._connection.execute(
                    """SELECT observations.observation_digest, observations.observation_json,
                              bindings.locator, bindings.binding_kind
                       FROM workspace_binding_observations observations
                       JOIN workspace_bindings bindings ON bindings.workspace_binding_id=observations.workspace_binding_id
                       WHERE observations.workspace_binding_id=? ORDER BY observations.observed_at DESC LIMIT 1""",
                    (str(row["workspace_binding_id"]),),
                ).fetchone()
                if observed is None or str(observed["observation_digest"]) != expected_observation_digest:
                    raise LeaseConflict("writer release observation has drifted")
                if (
                    str(observed["binding_kind"]) != str(captured_binding["binding_kind"])
                    or str(observed["locator"]) != str(captured_binding["locator"])
                ):
                    raise LeaseConflict("writer release binding changed during observation")
                stored = _json_loads(str(observed["observation_json"]), description="writer observation")
                if fresh_observation is not None and (
                    not isinstance(stored, Mapping)
                    or _json_dumps(dict(stored)) != _json_dumps(fresh_observation)
                ):
                    raise LeaseConflict("writer release workspace has live drift")
                self._connection.execute(
                    "UPDATE workspace_writer_claims SET status='released', released_at=? WHERE claim_id=?", (timestamp, claim_id),
                )
                self._record_writer_receipt_in_transaction(
                    operation_id, payload_digest, "release", claim_id, {"released": True}, timestamp,
                )
                # Releasing a cooperative writer is not reconciliation of an
                # uncertain effect.  In particular, an arbitrary caller token
                # must never clear a binding-scoped recovery barrier.

    def renew_workspace_writer(
        self, *, operation_id: str, payload_digest: str, claim_id: str, owner_id: str,
        expected_epoch: int, expected_observation_digest: str, lease_seconds: float,
    ) -> WorkspaceWriterClaim:
        """Renew only the exact active owner/epoch against current observation evidence."""
        if lease_seconds <= 0 or lease_seconds > self.MAX_WRITER_LEASE_SECONDS:
            raise ValueError("writer lease_seconds must be in (0, 300]")
        canonical = self.canonical_product_payload_digest({
            "kind": "writer_renew", "claim_id": claim_id, "owner_id": owner_id,
            "expected_epoch": expected_epoch, "expected_observation_digest": expected_observation_digest,
            "lease_seconds": lease_seconds,
        })
        if payload_digest != canonical:
            raise ProductAdmissionConflict("writer renewal digest is not canonical")
        with self._lock:
            receipt = self._writer_receipt_in_transaction(operation_id, payload_digest, "renew")
            if receipt is not None:
                receipt_claim_id, result = receipt
                if receipt_claim_id != claim_id:
                    raise ProductAdmissionConflict("writer renewal receipt claim does not match")
                return WorkspaceWriterClaim(
                    claim_id, str(result["workspace_binding_id"]), owner_id,
                    int(str(result["claim_epoch"])), str(result["expires_at"]),
                )
            claim_row = self._connection.execute(
                "SELECT workspace_binding_id FROM workspace_writer_claims WHERE claim_id=?",
                (claim_id,),
            ).fetchone()
        if claim_row is None:
            raise LeaseConflict("writer claim was not found")
        fresh_observation, captured_binding = self._capture_direct_binding_observation(
            str(claim_row["workspace_binding_id"])
        )
        expires_at = self._lease_expires_at(lease_seconds)
        with self._lock:
            with self._write_transaction():
                receipt = self._writer_receipt_in_transaction(operation_id, payload_digest, "renew")
                if receipt is not None:
                    receipt_claim_id, result = receipt
                    if receipt_claim_id != claim_id:
                        raise ProductAdmissionConflict("writer renewal receipt claim does not match")
                    return WorkspaceWriterClaim(
                        claim_id, str(result["workspace_binding_id"]), owner_id,
                        int(str(result["claim_epoch"])), str(result["expires_at"]),
                    )
                row = self._connection.execute(
                    "SELECT workspace_binding_id, claim_epoch, expires_at, status, owner_id FROM workspace_writer_claims WHERE claim_id=?",
                    (claim_id,),
                ).fetchone()
                if row is None or str(row["status"]) != "active" or str(row["owner_id"]) != owner_id \
                    or int(row["claim_epoch"]) != expected_epoch or self._timestamp_epoch(str(row["expires_at"])) <= self._lease_now():
                    raise LeaseConflict("writer renewal owner or epoch is stale")
                observed = self._connection.execute(
                    """SELECT observations.observation_digest, observations.observation_json,
                              bindings.locator, bindings.binding_kind
                       FROM workspace_binding_observations observations
                       JOIN workspace_bindings bindings ON bindings.workspace_binding_id=observations.workspace_binding_id
                       WHERE observations.workspace_binding_id=? ORDER BY observations.observed_at DESC LIMIT 1""",
                    (str(row["workspace_binding_id"]),),
                ).fetchone()
                if observed is None or str(observed["observation_digest"]) != expected_observation_digest:
                    raise LeaseConflict("writer renewal observation has drifted")
                if (
                    str(observed["binding_kind"]) != str(captured_binding["binding_kind"])
                    or str(observed["locator"]) != str(captured_binding["locator"])
                ):
                    raise LeaseConflict("writer renewal binding changed during observation")
                stored = _json_loads(str(observed["observation_json"]), description="writer observation")
                if fresh_observation is not None and (
                    not isinstance(stored, Mapping)
                    or _json_dumps(dict(stored)) != _json_dumps(fresh_observation)
                ):
                    raise LeaseConflict("writer renewal workspace has live drift")
                self._connection.execute(
                    "UPDATE workspace_writer_claims SET expires_at=? WHERE claim_id=?", (expires_at, claim_id),
                )
                self._record_writer_receipt_in_transaction(
                    operation_id, payload_digest, "renew", claim_id,
                    {"workspace_binding_id": str(row["workspace_binding_id"]), "claim_epoch": expected_epoch, "expires_at": expires_at},
                    self.clock(),
                )
                return WorkspaceWriterClaim(claim_id, str(row["workspace_binding_id"]), owner_id, expected_epoch, expires_at)

    def writer_expiry_reconciliation_digest(self, claim_id: str) -> str:
        """Return the exact durable evidence identity required for expiry repair."""
        with self._lock:
            row = self._connection.execute(
                "SELECT observation_json FROM workspace_writer_claims WHERE claim_id=?", (claim_id,)
            ).fetchone()
            if row is None:
                raise LeaseConflict("writer claim was not found")
            observation = _json_loads(str(row["observation_json"]), description="writer observation")
            if not isinstance(observation, Mapping):
                raise PersistenceError("writer observation is corrupt")
            return self.canonical_product_payload_digest({
                "kind": "writer_claim_expiry_reconciliation", "claim_id": claim_id,
                "observation": dict(observation),
            })

    def resolve_expired_writer_barrier(
        self, *, barrier_id: str, claim_id: str, reconciliation_digest: str,
    ) -> None:
        """Clear only the matching expiry barrier with its recorded evidence."""
        expected = self.writer_expiry_reconciliation_digest(claim_id)
        if reconciliation_digest != expected:
            raise LeaseConflict("writer reconciliation evidence does not match the expired invocation")
        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                updated = self._connection.execute(
                    """UPDATE workspace_recovery_barriers
                       SET status='cleared', reconciliation_token=?, cleared_at=?
                       WHERE barrier_id=? AND claim_id=? AND reason='writer_claim_expired' AND status='active'""",
                    (reconciliation_digest, timestamp, barrier_id, claim_id),
                )
                if updated.rowcount != 1:
                    raise LeaseConflict("matching active writer-expiry barrier was not found")

    def canonical_unknown_effect_barrier_digest(
        self, *, workspace_binding_id: str, uncertain_invocation_id: str,
        resolver_kind: str, evidence_digest: str,
    ) -> str:
        return self.canonical_product_payload_digest({
            "kind": "unknown_effect_barrier", "workspace_binding_id": workspace_binding_id,
            "uncertain_invocation_id": uncertain_invocation_id, "resolver_kind": resolver_kind,
            "evidence_digest": evidence_digest,
        })

    def _runtime_uncertain_invocation_evidence_in_transaction(
        self, workspace_binding_id: str, call_id: str,
    ) -> str:
        """Return evidence only for a real uncertain Runtime tool invocation."""
        result = self._runtime_invocation_evidence_any_status_in_transaction(
            workspace_binding_id, call_id, include_status=True,
        )
        if not isinstance(result, tuple):
            raise PersistenceError("uncertain invocation evidence status is unavailable")
        evidence, status = result
        if status != ToolCallState.UNCERTAIN.value:
            raise LeaseConflict("recovery barrier requires a real uncertain Runtime tool invocation")
        return evidence

    def _runtime_invocation_evidence_any_status_in_transaction(
        self, workspace_binding_id: str, call_id: str, *, include_status: bool = False,
    ) -> str | tuple[str, str]:
        """Read immutable invocation identity; callers decide allowed state."""
        row = self._connection.execute(
            """SELECT calls.session_id, calls.call_id, calls.tool_name, calls.recovery_mode,
                      calls.status, executions.runtime_execution_id
               FROM tool_calls calls
               JOIN runtime_executions executions ON executions.legacy_session_id=calls.session_id
               WHERE executions.workspace_binding_id=?
                 AND calls.call_id=printf('%d:%s:%s', length(calls.session_id), calls.session_id, ?)""",
            (workspace_binding_id, call_id),
        ).fetchone()
        if row is None:
            raise LeaseConflict("recovery barrier requires a real Runtime tool invocation")
        evidence = self.canonical_product_payload_digest({
            "kind": "runtime_uncertain_tool_invocation",
            "runtime_execution_id": str(row["runtime_execution_id"]),
            "session_id": str(row["session_id"]), "call_id": str(row["call_id"]),
            "tool_name": str(row["tool_name"]), "recovery_mode": str(row["recovery_mode"]),
        })
        return (evidence, str(row["status"])) if include_status else evidence

    def record_unknown_effect_recovery_barrier(
        self, *, operation_id: str, payload_digest: str, workspace_binding_id: str,
        uncertain_invocation_id: str, resolver_kind: str, evidence_digest: str,
    ) -> str:
        """Atomically retain a resolver-bound barrier for one uncertain invocation."""
        canonical = self.canonical_unknown_effect_barrier_digest(
            workspace_binding_id=workspace_binding_id, uncertain_invocation_id=uncertain_invocation_id,
            resolver_kind=resolver_kind, evidence_digest=evidence_digest,
        )
        if payload_digest != canonical:
            raise ProductAdmissionConflict("unknown-effect barrier digest is not canonical")
        if not all((operation_id, uncertain_invocation_id, resolver_kind, evidence_digest)):
            raise ValueError("unknown-effect barrier identity is required")
        if resolver_kind != "call_resolved":
            raise LeaseConflict("M2 recovery barriers require the durable Runtime call_resolved resolver")
        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                runtime_evidence = self._runtime_uncertain_invocation_evidence_in_transaction(
                    workspace_binding_id, uncertain_invocation_id,
                )
                if evidence_digest != runtime_evidence:
                    raise LeaseConflict("recovery barrier evidence does not match uncertain Runtime invocation")
                existing = self._connection.execute(
                    "SELECT barrier_id, evidence_digest, resolver_kind FROM workspace_recovery_barriers WHERE workspace_binding_id=? AND uncertain_invocation_id=?",
                    (workspace_binding_id, uncertain_invocation_id),
                ).fetchone()
                if existing is not None:
                    if str(existing["evidence_digest"]) != evidence_digest or str(existing["resolver_kind"]) != resolver_kind:
                        raise LeaseConflict("uncertain invocation identity conflicts with existing barrier")
                    return str(existing["barrier_id"])
                barrier_id = str(uuid.uuid4())
                self._connection.execute(
                    """INSERT INTO workspace_recovery_barriers(barrier_id, workspace_binding_id, claim_id,
                       reason, uncertain_invocation_id, resolver_kind, evidence_digest,
                       resolved_by_operation_id, status, reconciliation_token, created_at, cleared_at)
                       VALUES (?, ?, NULL, 'unknown_effect', ?, ?, ?, NULL, 'active', NULL, ?, NULL)""",
                    (barrier_id, workspace_binding_id, uncertain_invocation_id, resolver_kind,
                     evidence_digest, timestamp),
                )
                return barrier_id

    def resolve_unknown_effect_recovery_barrier(
        self, *, operation_id: str, payload_digest: str, barrier_id: str,
        workspace_binding_id: str, uncertain_invocation_id: str, resolver_kind: str,
        evidence_digest: str,
    ) -> None:
        """Resolve only the exact invocation/resolver/evidence tuple in one transaction."""
        canonical = self.canonical_unknown_effect_barrier_digest(
            workspace_binding_id=workspace_binding_id, uncertain_invocation_id=uncertain_invocation_id,
            resolver_kind=resolver_kind, evidence_digest=evidence_digest,
        )
        if payload_digest != canonical:
            raise ProductAdmissionConflict("unknown-effect resolution digest is not canonical")
        if resolver_kind not in {"call_resolved", "legacy_uncertain_call_resolver"}:
            raise LeaseConflict("unknown-effect resolution requires an exact Runtime call resolver")
        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                invocation = self._connection.execute(
                    """SELECT calls.session_id, calls.status FROM tool_calls calls
                       JOIN runtime_executions executions ON executions.legacy_session_id=calls.session_id
                       WHERE executions.workspace_binding_id=?
                         AND calls.call_id=printf('%d:%s:%s', length(calls.session_id), calls.session_id, ?)""",
                    (workspace_binding_id, uncertain_invocation_id),
                ).fetchone()
                if invocation is None or str(invocation["status"]) == ToolCallState.UNCERTAIN.value:
                    raise LeaseConflict("Runtime call has not durably resolved the uncertain effect")
                events = self._connection.execute(
                    "SELECT payload_json FROM events WHERE session_id=? AND event_type='call_resolved'",
                    (str(invocation["session_id"]),),
                ).fetchall()
                if not any(
                    isinstance(payload := _json_loads(str(event["payload_json"]), description="call resolution"), Mapping)
                    and payload.get("call_id") == uncertain_invocation_id
                    for event in events
                ):
                    raise LeaseConflict("durable call resolution evidence is unavailable")
                updated = self._connection.execute(
                    """UPDATE workspace_recovery_barriers
                       SET status='cleared', reconciliation_token=?, resolved_by_operation_id=?, cleared_at=?
                       WHERE barrier_id=? AND workspace_binding_id=?
                         AND reason IN ('unknown_effect', 'legacy_waiting_approval_reconciliation')
                         AND uncertain_invocation_id=? AND resolver_kind=? AND evidence_digest=? AND status='active'""",
                    (evidence_digest, operation_id, timestamp, barrier_id, workspace_binding_id,
                     uncertain_invocation_id, resolver_kind, evidence_digest),
                )
                if updated.rowcount != 1:
                    raise LeaseConflict("unknown-effect resolution does not match the active barrier")

    def repair_recovery_barriers_after_restart(self) -> int:
        """Project each legacy wait to an exact barrier or a fail-closed sentinel."""
        timestamp = self.clock()
        repaired = 0
        with self._lock:
            with self._write_transaction():
                rows = self._connection.execute(
                    """SELECT executions.workspace_binding_id, executions.legacy_session_id,
                              checkpoints.snapshot_json
                       FROM runtime_executions executions
                       JOIN sessions ON sessions.id=executions.legacy_session_id
                       JOIN checkpoints ON checkpoints.session_id=executions.legacy_session_id
                       WHERE sessions.state='waiting_approval'"""
                ).fetchall()
                for row in rows:
                    session_id = str(row["legacy_session_id"])
                    binding_id = str(row["workspace_binding_id"])
                    snapshot = RuntimeSnapshot.from_json(str(row["snapshot_json"]))
                    active_call_id = snapshot.active_call_id if snapshot.active_call_kind == "tool" else None
                    call_rows = self._connection.execute(
                        "SELECT call_id, status FROM tool_calls WHERE session_id=? AND status=?",
                        (session_id, ToolCallState.UNCERTAIN.value),
                    ).fetchall()
                    exact_calls = [
                        candidate for candidate in call_rows
                        if active_call_id is not None
                        and _unscoped_tool_call_id(session_id, str(candidate["call_id"])) == active_call_id
                    ]
                    uncertain_events = self._connection.execute(
                        "SELECT payload_json FROM events WHERE session_id=? AND event_type='tool_call_uncertain'",
                        (session_id,),
                    ).fetchall()
                    exact_event = any(
                        isinstance(payload := _json_loads(str(event["payload_json"]), description="uncertain call event"), Mapping)
                        and payload.get("call_id") == active_call_id
                        for event in uncertain_events
                    )
                    if len(call_rows) != 1 or len(exact_calls) != 1 or not exact_event or active_call_id is None:
                        # Missing *or contradictory* call evidence cannot be
                        # resolved by ordinary permission/text. A durable,
                        # idempotent binding sentinel blocks resume and writers.
                        sentinel = f"legacy-incomplete-evidence:{session_id}"
                        inserted = self._connection.execute(
                            """INSERT OR IGNORE INTO workspace_recovery_barriers(barrier_id, workspace_binding_id,
                               claim_id, reason, uncertain_invocation_id, resolver_kind, evidence_digest,
                               resolved_by_operation_id, status, reconciliation_token, created_at, cleared_at)
                               VALUES (?, ?, NULL, 'legacy_waiting_approval_incomplete_evidence', ?,
                               'unresolvable_fail_closed', ?, NULL, 'active', NULL, ?, NULL)""",
                            (str(uuid.uuid4()), binding_id, sentinel,
                             self.canonical_product_payload_digest({
                                 "kind": "legacy_waiting_approval_incomplete_evidence",
                                 "session_id": session_id, "active_call_id": active_call_id,
                                 "uncertain_call_count": len(call_rows), "exact_event": exact_event,
                             }), timestamp),
                        )
                        repaired += inserted.rowcount
                        continue
                    invocation = active_call_id
                    try:
                        evidence = self._runtime_uncertain_invocation_evidence_in_transaction(
                            binding_id, invocation,
                        )
                    except LeaseConflict:
                        # This should be rare after the exact checks, but still
                        # must remain fail closed rather than be skipped.
                        continue
                    existing = self._connection.execute(
                        "SELECT 1 FROM workspace_recovery_barriers WHERE workspace_binding_id=? AND uncertain_invocation_id=?",
                        (binding_id, invocation),
                    ).fetchone()
                    if existing is None:
                        self._connection.execute(
                            """INSERT INTO workspace_recovery_barriers(barrier_id, workspace_binding_id, claim_id,
                               reason, uncertain_invocation_id, resolver_kind, evidence_digest,
                               resolved_by_operation_id, status, reconciliation_token, created_at, cleared_at)
                               VALUES (?, ?, NULL, 'legacy_waiting_approval_reconciliation', ?,
                               'legacy_uncertain_call_resolver', ?, NULL,
                               'active', NULL, ?, NULL)""",
                            (str(uuid.uuid4()), binding_id, invocation, evidence, timestamp),
                        )
                        repaired += 1
        return repaired

    def repair_admitted_turn_finalizations_after_restart(self) -> int:
        """Idempotently project terminal M2 Runtime rows left by a crash.

        The Runtime row remains authoritative; this only restores the missing
        Product finalization/open-Turn projection in the same SQLite authority.
        """
        repaired = 0
        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                rows = self._connection.execute(
                    """SELECT admission.operation_id FROM product_admissions admission
                       JOIN sessions ON sessions.id=admission.legacy_session_id
                       LEFT JOIN turn_finalizations finalized ON finalized.turn_id=admission.turn_id
                       WHERE sessions.state IN ('completed', 'failed', 'cancelled')
                         AND finalized.turn_id IS NULL"""
                ).fetchall()
                for row in rows:
                    self._finalize_admitted_turn_in_transaction(
                        str(row["operation_id"]), timestamp, "m2_restart_terminal_projection_repair",
                    )
                    repaired += 1
        return repaired

    def repair_resolved_recovery_barriers_after_restart(self) -> int:
        """Close only barriers whose matching Runtime resolution is committed.

        Runtime's historical recorder owns its transaction.  Until its
        ``CALL_RESOLVED`` event and non-uncertain call row are both durable the
        active barrier blocks writers.  This repair therefore closes the
        compatibility seam after a crash without ever opening a writer window.
        """
        timestamp = self.clock()
        repaired = 0
        with self._lock:
            with self._write_transaction():
                rows = self._connection.execute(
                    """SELECT barriers.barrier_id, barriers.workspace_binding_id,
                              barriers.uncertain_invocation_id, barriers.evidence_digest,
                              calls.session_id, calls.status
                       FROM workspace_recovery_barriers barriers
                       JOIN runtime_executions executions
                         ON executions.workspace_binding_id=barriers.workspace_binding_id
                       JOIN tool_calls calls
                         ON calls.session_id=executions.legacy_session_id
                        AND calls.call_id=printf('%d:%s:%s', length(calls.session_id), calls.session_id, barriers.uncertain_invocation_id)
                       WHERE barriers.reason IN ('unknown_effect', 'legacy_waiting_approval_reconciliation') AND barriers.status='active'
                         AND barriers.resolver_kind IN ('call_resolved','legacy_uncertain_call_resolver')"""
                ).fetchall()
                for row in rows:
                    if str(row["status"]) == ToolCallState.UNCERTAIN.value:
                        continue
                    events = self._connection.execute(
                        "SELECT payload_json FROM events WHERE session_id=? AND event_type='call_resolved'",
                        (str(row["session_id"]),),
                    ).fetchall()
                    if not any(
                        isinstance(payload := _json_loads(str(event["payload_json"]), description="call resolution"), Mapping)
                        and payload.get("call_id") == str(row["uncertain_invocation_id"])
                        for event in events
                    ):
                        continue
                    evidence = self._runtime_invocation_evidence_any_status_in_transaction(
                        str(row["workspace_binding_id"]), str(row["uncertain_invocation_id"]),
                    )
                    if evidence != str(row["evidence_digest"]):
                        continue
                    updated = self._connection.execute(
                        """UPDATE workspace_recovery_barriers SET status='cleared',
                           resolved_by_operation_id='m2_restart_runtime_resolution_repair',
                           reconciliation_token=?, cleared_at=?
                           WHERE barrier_id=? AND status='active'""",
                        (evidence, timestamp, str(row["barrier_id"])),
                    )
                    repaired += updated.rowcount
        return repaired

    def finalize_admitted_turn(self, operation_id: str, *, repair_provenance: str = "m2_runtime_observation") -> None:
        """Project an already-terminal legacy Session and reopen its Conversation.

        This does not mutate the Runtime; it only reads the physical Session
        terminal state and records an idempotent Product finalization.
        """
        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                self._finalize_admitted_turn_in_transaction(operation_id, timestamp, repair_provenance)

    def finalize_admitted_turn_for_session(
        self, session_id: str, *, repair_provenance: str = "m2_runtime_terminal_return",
    ) -> None:
        """Project a normal mapped Runtime terminal return; legacy sessions are untouched."""
        with self._lock:
            with self._write_transaction():
                row = self._connection.execute(
                    "SELECT operation_id FROM product_admissions WHERE legacy_session_id=?", (session_id,)
                ).fetchone()
                if row is not None:
                    self._finalize_admitted_turn_in_transaction(
                        str(row["operation_id"]), self.clock(), repair_provenance,
                    )

    def _finalize_admitted_turn_in_transaction(
        self, operation_id: str, timestamp: str, repair_provenance: str,
    ) -> None:
        """Project a terminal Session while its enclosing journal transaction is open."""
        admission = self._connection.execute(
            "SELECT conversation_id, turn_id, runtime_execution_id, legacy_session_id FROM product_admissions WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if admission is None:
            raise ProductLifecycleConflict("admission was not found")
        session = self._connection.execute(
            "SELECT state FROM sessions WHERE id=?", (str(admission["legacy_session_id"]),)
        ).fetchone()
        if session is None:
            raise PersistenceError("admitted Session disappeared")
        state = RuntimeState(str(session["state"]))
        if state not in TERMINAL_STATES:
            raise ProductLifecycleConflict("cannot finalize a non-terminal RuntimeExecution")
        existing = self._connection.execute(
            "SELECT outcome FROM turn_finalizations WHERE turn_id=?", (str(admission["turn_id"]),)
        ).fetchone()
        if existing is not None:
            if str(existing["outcome"]) != state.value:
                raise PersistenceError("terminal projection conflicts with Session authority")
            return
        self._connection.execute(
            """INSERT INTO turn_finalizations(turn_id, runtime_execution_id, outcome, finalized_at, repair_provenance)
               VALUES (?, ?, ?, ?, ?)""",
            (str(admission["turn_id"]), str(admission["runtime_execution_id"]), state.value, timestamp, repair_provenance),
        )
        self._append_conversation_semantic_event(
            str(admission["conversation_id"]), "turn_finalized", "m2_runtime_projection",
            {"turn_id": str(admission["turn_id"]), "outcome": state.value,
             "repair_provenance": repair_provenance}, timestamp,
        )
        self._connection.execute(
            "UPDATE conversations SET open_turn_id=NULL, product_version=product_version+1 WHERE conversation_id=? AND open_turn_id=?",
            (str(admission["conversation_id"]), str(admission["turn_id"])),
        )
        # Inputs serialized before the terminal event cannot leak into a later
        # Turn. Delivered replies/steering are already consumed; remaining
        # accepted controls are durably closed as terminally unconsumed.
        self._connection.execute(
            """UPDATE product_inputs SET status='closed_terminal_unconsumed'
               WHERE turn_id=? AND input_kind IN ('steering','reply','cancel')
                 AND status='accepted'""",
            (str(admission["turn_id"]),),
        )
        self._connection.execute(
            """UPDATE workspace_writer_claims SET status='released', released_at=?
               WHERE runtime_execution_id=? AND status='active'""",
            (timestamp, str(admission["runtime_execution_id"])),
        )

    def repair_stable_writer_claims_after_restart(self) -> int:
        """Release execution-owned claims only at terminal or detached stable waits.

        A recovery barrier is intentionally untouched and continues to block
        subsequent acquire after a release.
        """
        timestamp = self.clock()
        with self._lock:
            with self._write_transaction():
                updated = self._connection.execute(
                    """UPDATE workspace_writer_claims SET status='released', released_at=?
                       WHERE status='active' AND runtime_execution_id IN (
                         SELECT executions.runtime_execution_id FROM runtime_executions executions
                         JOIN sessions ON sessions.id=executions.legacy_session_id
                         WHERE sessions.state IN ('completed','failed','cancelled','interrupted',
                           'waiting_user_input','waiting_permission')
                       )""",
                    (timestamp,),
                )
                return updated.rowcount

    def mark_admitted_initialization_failed(self, operation_id: str, error: Exception) -> None:
        """Record a post-admission initialization failure without retracting it.

        M2 deliberately invokes the existing Runtime journal mutation API; it
        neither reimplements the FSM nor manufactures a Product terminal state.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT legacy_session_id FROM product_admissions WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise ProductLifecycleConflict("admission was not found")
            session_id = str(row["legacy_session_id"])
            snapshot_row = self._connection.execute(
                "SELECT snapshot_json FROM checkpoints WHERE session_id=?", (session_id,)
            ).fetchone()
            if snapshot_row is None:
                raise PersistenceError("admitted Session has no checkpoint")
            snapshot = RuntimeSnapshot.from_json(str(snapshot_row["snapshot_json"]))
        if snapshot.state in TERMINAL_STATES:
            self.finalize_admitted_turn(operation_id, repair_provenance="m2_initialization_failure_recheck")
            return
        failed = replace(
            RuntimeStateMachine.transition_snapshot(snapshot, RuntimeState.FAILED),
            failure={"kind": "m2_post_admission_initialization", "message": str(error)},
        )
        mutation = JournalMutation(
            session_id=session_id, expected_version=snapshot.version,
            expected_state=snapshot.state, snapshot_after=failed,
            event_type=EventType.RUN_FINISHED,
            payload={"outcome": "failed", "provenance": "m2_post_admission_initialization"},
        )
        with self._lock:
            try:
                with self._write_transaction():
                    self._commit_in_open_transaction(mutation, self.clock())
                    self._finalize_admitted_turn_in_transaction(
                        operation_id, self.clock(), "m2_post_admission_initialization",
                    )
                    self._invoke_commit_hook()
            except sqlite3.IntegrityError as exc:
                raise PersistenceError("M2 initialization finalization violated a database constraint") from exc

    def acquire_lease(self, session_id: str, owner: str, *, lease_seconds: float = 60.0) -> None:
        if not owner:
            raise ValueError("lease owner cannot be empty")
        expires_at = self._lease_expires_at(lease_seconds)
        with self._lock:
            with self._write_transaction():
                row = self._read_session_row(session_id)
                if RuntimeState(str(row["state"])) in TERMINAL_STATES:
                    raise LeaseConflict(f"terminal session cannot be leased: {session_id}")
                current_owner = row["lease_owner"]
                current_expiry = row["lease_expires_at"]
                expired = current_expiry is None or (
                    self._timestamp_epoch(str(current_expiry)) <= self._lease_now()
                )
                if current_owner not in (None, owner) and not expired:
                    raise LeaseConflict(
                        f"session {session_id} is leased by another active owner"
                    )
                self._connection.execute(
                    """
                    UPDATE sessions SET lease_owner=?, lease_expires_at=?
                    WHERE id=?
                    """,
                    (owner, expires_at, session_id),
                )

    def renew_lease(self, session_id: str, owner: str, *, lease_seconds: float = 60.0) -> None:
        expires_at = self._lease_expires_at(lease_seconds)
        with self._lock:
            with self._write_transaction():
                row = self._read_session_row(session_id)
                if row["lease_owner"] != owner:
                    raise LeaseConflict(f"session {session_id} is not leased by {owner!r}")
                self._connection.execute(
                    "UPDATE sessions SET lease_expires_at=? WHERE id=? AND lease_owner=?",
                    (expires_at, session_id, owner),
                )

    def release_lease(self, session_id: str, owner: str) -> None:
        with self._lock:
            with self._write_transaction():
                row = self._read_session_row(session_id)
                if row["lease_owner"] != owner:
                    raise LeaseConflict(f"session {session_id} is not leased by {owner!r}")
                self._connection.execute(
                    """
                    UPDATE sessions SET lease_owner=NULL, lease_expires_at=NULL
                    WHERE id=? AND lease_owner=?
                    """,
                    (session_id, owner),
                )

    def request_interrupt(self, session_id: str) -> str:
        requested_at = self.clock()
        with self._lock:
            with self._write_transaction():
                row = self._read_session_row(session_id)
                state = self._row_state(row)
                if state in TERMINAL_STATES:
                    raise PersistenceError(f"terminal session cannot be interrupted: {session_id}")
                checkpoint = self._connection.execute(
                    "SELECT snapshot_json FROM checkpoints WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if checkpoint is None:
                    raise InvariantViolation(f"session {session_id} has no checkpoint")
                snapshot = RuntimeSnapshot.from_json(str(checkpoint["snapshot_json"]))
                snapshot = replace(snapshot, interrupt_requested_at=requested_at)
                self._connection.execute(
                    "UPDATE sessions SET interrupt_requested_at=? WHERE id=?",
                    (requested_at, session_id),
                )
                self._connection.execute(
                    "UPDATE checkpoints SET snapshot_json=? WHERE session_id=?",
                    (snapshot.to_json(), session_id),
                )
        return requested_at

    def interrupt_requested_at(self, session_id: str) -> str | None:
        with self._lock:
            row = self._read_session_row(session_id)
        return str(row["interrupt_requested_at"]) if row["interrupt_requested_at"] else None

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, task, source_path, state, workspace_path, version,
                       model_calls, tool_calls, lease_owner, lease_expires_at,
                       interrupt_requested_at, updated_at
                FROM sessions ORDER BY updated_at, id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _decode_optional_object(raw: str | None, *, description: str) -> JsonObject | None:
        if raw is None:
            return None
        value = _json_loads(raw, description=description)
        if not isinstance(value, Mapping):
            raise InvariantViolation(f"persisted {description} must be an object")
        return dict(value)

    def get_model_call(self, session_id: str, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._read_session_row(session_id)
            row = self._connection.execute(
                "SELECT * FROM model_calls WHERE session_id=? AND request_id=?",
                (session_id, request_id),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["request"] = self._decode_optional_object(
            str(row["request_json"]), description="model request"
        )
        result["response"] = self._decode_optional_object(
            row["response_json"], description="model response"
        )
        result["error"] = self._decode_optional_object(
            row["error_json"], description="model error"
        )
        return result

    def get_tool_call(self, session_id: str, call_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._read_session_row(session_id)
            storage_call_id = _scoped_tool_call_id(session_id, call_id)
            row = self._connection.execute(
                "SELECT * FROM tool_calls WHERE session_id=? AND call_id=?",
                (session_id, storage_call_id),
            ).fetchone()
            if row is None:
                # Read rows written by the early v2 implementation before call ids
                # were made session-scoped inside the global SQL primary key.
                row = self._connection.execute(
                    "SELECT * FROM tool_calls WHERE session_id=? AND call_id=?",
                    (session_id, call_id),
                ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["call_id"] = call_id
        result["arguments"] = self._decode_optional_object(
            str(row["arguments_json"]), description="tool arguments"
        )
        result["result"] = self._decode_optional_object(
            row["result_json"], description="tool result"
        )
        result["error"] = self._decode_optional_object(
            row["error_json"], description="tool error"
        )
        return result

    def list_model_calls(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._read_session_row(session_id)
            rows = self._connection.execute(
                "SELECT request_id FROM model_calls WHERE session_id=? ORDER BY ordinal",
                (session_id,),
            ).fetchall()
        calls: list[dict[str, Any]] = []
        for row in rows:
            call = self.get_model_call(session_id, str(row["request_id"]))
            if call is None:
                raise InvariantViolation("listed model call cannot be read back")
            calls.append(call)
        return calls

    def list_tool_calls(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._read_session_row(session_id)
            rows = self._connection.execute(
                "SELECT call_id FROM tool_calls WHERE session_id=? ORDER BY ordinal",
                (session_id,),
            ).fetchall()
        calls: list[dict[str, Any]] = []
        for row in rows:
            call = self.get_tool_call(
                session_id,
                _unscoped_tool_call_id(session_id, str(row["call_id"])),
            )
            if call is None:
                raise InvariantViolation("listed tool call cannot be read back")
            calls.append(call)
        return calls

    def get_summary(self, session_id: str, summary_id: str) -> SummaryRecord | None:
        with self._lock:
            self._read_session_row(session_id)
            row = self._connection.execute(
                "SELECT summary_json FROM summaries WHERE session_id=? AND summary_id=?",
                (session_id, summary_id),
            ).fetchone()
        if row is None:
            return None
        raw = _json_loads(str(row["summary_json"]), description="summary")
        if not isinstance(raw, Mapping):
            raise InvariantViolation("persisted summary must be an object")
        record = SummaryRecord.from_dict(raw)
        if record.session_id != session_id or record.summary_id != summary_id:
            raise InvariantViolation("summary identity does not match its row")
        return record

    def get_latest_summary(self, session_id: str) -> SummaryRecord | None:
        with self._lock:
            self._read_session_row(session_id)
            row = self._connection.execute(
                """
                SELECT summary_id FROM summaries
                WHERE session_id=? AND superseded_by IS NULL
                ORDER BY source_event_end DESC, created_at DESC, summary_id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return self.get_summary(session_id, str(row["summary_id"]))

    def list_summaries(self, session_id: str) -> list[SummaryRecord]:
        with self._lock:
            self._read_session_row(session_id)
            rows = self._connection.execute(
                """
                SELECT summary_id FROM summaries
                WHERE session_id=? ORDER BY source_event_start, source_event_end, summary_id
                """,
                (session_id,),
            ).fetchall()
        result: list[SummaryRecord] = []
        for row in rows:
            record = self.get_summary(session_id, str(row["summary_id"]))
            if record is not None:
                result.append(record)
        return result

    def completed_model_call_count(self, session_id: str) -> int:
        with self._lock:
            self._read_session_row(session_id)
            row = self._connection.execute(
                """
                SELECT COUNT(*) FROM model_calls
                WHERE session_id=? AND response_json IS NOT NULL
                """,
                (session_id,),
            ).fetchone()
        return int(row[0])

    def _read_session_row(self, session_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        return row

    @staticmethod
    def _row_state(row: sqlite3.Row) -> RuntimeState:
        try:
            return RuntimeState(str(row["state"]))
        except ValueError as exc:
            raise InvariantViolation("session contains an unknown runtime state") from exc

    def _raise_conflict(
        self,
        session_id: str,
        row: sqlite3.Row,
        mutation: JournalMutation,
    ) -> None:
        raise JournalConflict(
            session_id,
            expected_state=mutation.expected_state,
            actual_state=self._row_state(row),
            expected_version=mutation.expected_version,
            actual_version=int(row["version"]),
        )

    def _commit_in_open_transaction(
        self, mutation: JournalMutation, timestamp: str,
    ) -> CommitResult:
        """Apply one Runtime journal mutation inside the caller's transaction.

        The public ``commit`` method and the M2 post-admission failure path
        share this primitive so a Session terminal outcome and its Product
        finalization can be one atomic SQLite publication.
        """
        snapshot = mutation.snapshot_after
        if snapshot.session_id != mutation.session_id:
            raise InvariantViolation("mutation session id does not match snapshot")
        if snapshot.version != mutation.expected_version:
            raise InvariantViolation(
                "snapshot version must equal the mutation expected version"
            )
        row = self._read_session_row(mutation.session_id)
        actual_state = self._row_state(row)
        actual_version = int(row["version"])
        if actual_state is not mutation.expected_state or actual_version != mutation.expected_version:
            self._raise_conflict(mutation.session_id, row, mutation)
        if mutation.lease_owner is not None:
            current_owner = row["lease_owner"]
            expires_at = row["lease_expires_at"]
            if current_owner != mutation.lease_owner or (
                expires_at is not None and self._timestamp_epoch(str(expires_at)) <= self._lease_now()
            ):
                raise LeaseConflict(f"lease for {mutation.session_id} is not held by {mutation.lease_owner!r}")

        sequence = int(row["last_event_sequence"]) + 1
        committed_version = actual_version + 1
        requested_at = None if mutation.clear_interrupt else (
            str(row["interrupt_requested_at"])
            if row["interrupt_requested_at"] is not None else snapshot.interrupt_requested_at
        )
        committed_snapshot = replace(
            snapshot, updated_at=timestamp, version=committed_version,
            interrupt_requested_at=requested_at,
        )
        updated = self._connection.execute(
            """
            UPDATE sessions SET
                task=?, source_path=?, workspace_path=?, state=?, policy_json=?,
                source_fingerprint=?, final_answer=?, failure_json=?, step_count=?,
                model_calls=?, tool_calls=?, last_event_sequence=?, version=?,
                created_at=?, updated_at=?, interrupt_requested_at=?,
                resume_target_state=?, context_version=?
            WHERE id=? AND state=? AND version=?
            """,
            self._session_values(
                committed_snapshot, updated_at=timestamp,
                last_event_sequence=sequence, version=committed_version,
            ) + (mutation.expected_state.value, mutation.expected_version),
        )
        if updated.rowcount != 1:
            self._raise_conflict(mutation.session_id, self._read_session_row(mutation.session_id), mutation)

        if mutation.message_to_append is not None:
            message_row = self._connection.execute(
                "SELECT COALESCE(MAX(message_index), -1) + 1 FROM messages WHERE session_id = ?",
                (mutation.session_id,),
            ).fetchone()
            self._insert_message(mutation.session_id, mutation.message_to_append, int(message_row[0]), timestamp)
        if mutation.model_call is not None:
            self._upsert_model_call(mutation.session_id, mutation.model_call, timestamp)
        if mutation.tool_call is not None:
            self._upsert_tool_call(mutation.session_id, mutation.tool_call, timestamp)
            if mutation.tool_call.status is ToolCallState.UNCERTAIN:
                self._ensure_runtime_uncertain_barrier_in_transaction(
                    mutation.session_id, mutation.tool_call.call_id, timestamp,
                )
        if mutation.summary is not None:
            self._upsert_summary(mutation.session_id, mutation.summary, timestamp)
        event = self._new_event(
            session_id=mutation.session_id, sequence=sequence,
            event_type=mutation.event_type, state=committed_snapshot.state,
            timestamp=timestamp, payload=mutation.payload,
        )
        self._insert_event(event)
        self._upsert_checkpoint(committed_snapshot, timestamp)
        # When M2 owns the mapped Runtime and Product projection, stable
        # lifecycle publication shares this journal transaction.  This closes
        # both the terminal/finalization window and the live writer-release
        # window; a recovery barrier is deliberately independent and remains.
        admission = self._connection.execute(
            "SELECT operation_id, runtime_execution_id FROM product_admissions WHERE legacy_session_id=?",
            (mutation.session_id,),
        ).fetchone()
        if admission is not None:
            if committed_snapshot.state in TERMINAL_STATES:
                self._finalize_admitted_turn_in_transaction(
                    str(admission["operation_id"]), timestamp, "m2_atomic_runtime_terminal",
                )
            elif committed_snapshot.state in {
                RuntimeState.INTERRUPTED,
                RuntimeState.WAITING_USER_INPUT,
                RuntimeState.WAITING_PERMISSION,
            }:
                self._connection.execute(
                    """UPDATE workspace_writer_claims SET status='released', released_at=?
                       WHERE runtime_execution_id=? AND status='active'""",
                    (timestamp, str(admission["runtime_execution_id"])),
                )
        return CommitResult(event=event, committed_version=committed_version)

    def _ensure_runtime_uncertain_barrier_in_transaction(
        self, session_id: str, call_id: str, timestamp: str,
    ) -> None:
        """Publish a mapped M2 uncertain-call barrier with the Runtime commit.

        Legacy sessions without a Product RuntimeExecution deliberately have no
        binding to protect here; journal-open repair handles only mapped legacy
        evidence and never guesses a binding.
        """
        mapping = self._connection.execute(
            "SELECT workspace_binding_id FROM runtime_executions WHERE legacy_session_id=?", (session_id,)
        ).fetchone()
        if mapping is None:
            return
        binding_id = str(mapping["workspace_binding_id"])
        evidence = self._runtime_uncertain_invocation_evidence_in_transaction(binding_id, call_id)
        existing = self._connection.execute(
            "SELECT evidence_digest, resolver_kind FROM workspace_recovery_barriers WHERE workspace_binding_id=? AND uncertain_invocation_id=?",
            (binding_id, call_id),
        ).fetchone()
        if existing is not None:
            if str(existing["evidence_digest"]) != evidence or str(existing["resolver_kind"]) not in {
                "call_resolved", "legacy_uncertain_call_resolver",
            }:
                raise LeaseConflict("uncertain Runtime invocation conflicts with its recovery barrier")
            return
        self._connection.execute(
            """INSERT INTO workspace_recovery_barriers(barrier_id, workspace_binding_id, claim_id,
               reason, uncertain_invocation_id, resolver_kind, evidence_digest, resolved_by_operation_id,
               status, reconciliation_token, created_at, cleared_at)
               VALUES (?, ?, NULL, 'unknown_effect', ?, 'call_resolved', ?, NULL, 'active', NULL, ?, NULL)""",
            (str(uuid.uuid4()), binding_id, call_id, evidence, timestamp),
        )

    def commit(self, mutation: JournalMutation) -> CommitResult:
        with self._lock:
            try:
                with self._write_transaction():
                    result = self._commit_in_open_transaction(mutation, self.clock())
                    self._invoke_commit_hook()
            except sqlite3.IntegrityError as exc:
                raise PersistenceError("journal mutation violated a database constraint") from exc
        return result

    def load_snapshot(self, session_id: str) -> RuntimeSnapshot:
        with self._lock:
            row = self._read_session_row(session_id)
            checkpoint = self._connection.execute(
                "SELECT * FROM checkpoints WHERE session_id = ?", (session_id,)
            ).fetchone()
        if checkpoint is None:
            raise InvariantViolation(f"session {session_id} has no checkpoint")
        snapshot = RuntimeSnapshot.from_json(str(checkpoint["snapshot_json"]))
        if snapshot.session_id != session_id:
            raise InvariantViolation("checkpoint session id does not match its session")
        if snapshot.state.value != str(row["state"]):
            raise InvariantViolation("checkpoint state disagrees with session")
        if snapshot.version != int(row["version"]):
            raise InvariantViolation("checkpoint version disagrees with session")
        if str(checkpoint["state"]) != snapshot.state.value:
            raise InvariantViolation("checkpoint state column disagrees with snapshot")
        if str(checkpoint["updated_at"]) != snapshot.updated_at:
            raise InvariantViolation("checkpoint timestamp disagrees with snapshot")
        self._assert_snapshot_matches_row(snapshot, row)
        return snapshot

    def _assert_snapshot_matches_row(
        self,
        snapshot: RuntimeSnapshot,
        row: sqlite3.Row,
    ) -> None:
        if snapshot.task != str(row["task"]):
            raise InvariantViolation("checkpoint task disagrees with session")
        if snapshot.source_path != str(row["source_path"]):
            raise InvariantViolation("checkpoint source path disagrees with session")
        policy = _json_loads(str(row["policy_json"]), description="policy")
        if not isinstance(policy, Mapping):
            raise InvariantViolation("session policy must be an object")
        try:
            normalized_policy = RunPolicy.from_dict(policy).to_dict()
        except (TypeError, ValueError) as exc:
            raise InvariantViolation("session policy contains invalid values") from exc
        if snapshot.policy.to_dict() != normalized_policy:
            raise InvariantViolation("checkpoint policy disagrees with session")
        if snapshot.workspace_path != row["workspace_path"]:
            raise InvariantViolation("checkpoint workspace path disagrees with session")
        if snapshot.source_fingerprint != str(row["source_fingerprint"]):
            raise InvariantViolation("checkpoint source fingerprint disagrees with session")
        if snapshot.final_answer != row["final_answer"]:
            raise InvariantViolation("checkpoint final answer disagrees with session")
        row_failure = (
            _json_loads(str(row["failure_json"]), description="failure")
            if row["failure_json"] is not None
            else None
        )
        if snapshot.failure != row_failure:
            raise InvariantViolation("checkpoint failure disagrees with session")
        for field in ("step_count", "model_calls", "tool_calls"):
            if getattr(snapshot, field) != int(row[field]):
                raise InvariantViolation(f"checkpoint {field} disagrees with session")
        row_interrupt = (
            str(row["interrupt_requested_at"])
            if row["interrupt_requested_at"] is not None
            else None
        )
        if snapshot.interrupt_requested_at != row_interrupt:
            raise InvariantViolation("checkpoint interrupt request disagrees with session")
        row_resume = (
            RuntimeState(str(row["resume_target_state"]))
            if row["resume_target_state"] is not None
            else None
        )
        if snapshot.resume_target_state is not row_resume:
            raise InvariantViolation("checkpoint resume target disagrees with session")
        if snapshot.context_version != str(row["context_version"]):
            raise InvariantViolation("checkpoint context version disagrees with session")
        if snapshot.created_at != str(row["created_at"]):
            raise InvariantViolation("checkpoint creation time disagrees with session")
        if snapshot.updated_at != str(row["updated_at"]):
            raise InvariantViolation("checkpoint update time disagrees with session")

    def list_messages(self, session_id: str) -> list[Message]:
        with self._lock:
            self._read_session_row(session_id)
            rows = self._connection.execute(
                """
                SELECT message_index, role, content, tool_call_id, metadata_json
                FROM messages WHERE session_id = ? ORDER BY message_index
                """,
                (session_id,),
            ).fetchall()
        messages: list[Message] = []
        for expected_index, row in enumerate(rows):
            if int(row["message_index"]) != expected_index:
                raise InvariantViolation("message indexes are not contiguous")
            metadata = _json_loads(str(row["metadata_json"]), description="message metadata")
            if not isinstance(metadata, Mapping):
                raise InvariantViolation("message metadata must be an object")
            try:
                messages.append(
                    Message(
                        role=str(row["role"]),
                        content=str(row["content"]),
                        tool_call_id=(
                            str(row["tool_call_id"])
                            if row["tool_call_id"] is not None
                            else None
                        ),
                        metadata=dict(metadata),
                    )
                )
            except (TypeError, ValueError, KeyError) as exc:
                raise InvariantViolation("invalid persisted message") from exc
        return messages

    def list_events(self, session_id: str) -> list[Event]:
        with self._lock:
            self._read_session_row(session_id)
            rows = self._connection.execute(
                """
                SELECT event_id, session_id, sequence, schema_version, event_type,
                       state, payload_json, created_at
                FROM events WHERE session_id = ? ORDER BY sequence
                """,
                (session_id,),
            ).fetchall()
        events: list[Event] = []
        for row in rows:
            payload = _json_loads(str(row["payload_json"]), description="event payload")
            if not isinstance(payload, Mapping):
                raise InvariantViolation("event payload must be an object")
            try:
                events.append(
                    Event.from_dict(
                        {
                            "schema_version": row["schema_version"],
                            "event_id": row["event_id"],
                            "session_id": row["session_id"],
                            "sequence": row["sequence"],
                            "event_type": row["event_type"],
                            "timestamp": row["created_at"],
                            "state": row["state"],
                            "payload": dict(payload),
                        }
                    )
                )
            except (TypeError, ValueError, KeyError) as exc:
                raise InvariantViolation("invalid persisted event") from exc
        return events

    def load(self, session_id: str) -> list[Event]:
        return self.list_events(session_id)

    def trace_path(self, session_id: str) -> Path:
        return self.trace_root / f"{session_id}.jsonl"

    def session_version(self, session_id: str) -> int:
        with self._lock:
            row = self._read_session_row(session_id)
        return int(row["version"])

    def last_event_sequence(self, session_id: str) -> int:
        with self._lock:
            row = self._read_session_row(session_id)
        return int(row["last_event_sequence"])

    def load_session(self, session_id: str) -> Session:
        snapshot = self.load_snapshot(session_id)
        return Session.from_snapshot(snapshot, self.list_messages(session_id))

    def append(self, event: Event) -> None:
        raise PersistenceError(
            "SQLite events are authority; use JournalMutation.commit instead of append"
        )

    def export_trace(self, session_id: str, destination: str | Path | None = None) -> Path:
        from coding_agent.export import export_trace

        return export_trace(self, session_id, destination)


SQLiteJournal = SQLiteRunJournal
SQLiteEventStore = SQLiteRunJournal
SQLitePersistence = SQLiteRunJournal
