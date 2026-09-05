from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
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
    Session,
    SummaryRecord,
    TERMINAL_STATES,
    ToolCallState,
)
from coding_agent.migrations import LATEST_SCHEMA_VERSION, MigrationRunner


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
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self._database,
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure_connection()
        self.schema_version = MigrationRunner(clock=self.clock).migrate(self._connection)

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
        event_timestamp = self.clock()
        initial_snapshot = snapshot
        payload = {
            "task": initial_snapshot.task,
            "source_name": Path(initial_snapshot.source_path).name,
        }
        if session_created_payload:
            payload.update(session_created_payload)
        with self._lock:
            try:
                with self._write_transaction():
                    existing = self._connection.execute(
                        "SELECT 1 FROM sessions WHERE id = ?", (initial_snapshot.session_id,)
                    ).fetchone()
                    if existing is not None:
                        raise PersistenceError(
                            f"session already exists: {initial_snapshot.session_id}"
                        )
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
                            initial_snapshot.session_id,
                            initial_snapshot.task,
                            initial_snapshot.source_path,
                            initial_snapshot.workspace_path,
                            initial_snapshot.state.value,
                            _json_dumps(initial_snapshot.policy.to_dict()),
                            initial_snapshot.source_fingerprint,
                            initial_snapshot.final_answer,
                            (
                                _json_dumps(initial_snapshot.failure)
                                if initial_snapshot.failure is not None
                                else None
                            ),
                            initial_snapshot.step_count,
                            initial_snapshot.model_calls,
                            initial_snapshot.tool_calls,
                            2,
                            initial_snapshot.version,
                            initial_snapshot.created_at,
                            initial_snapshot.updated_at,
                            initial_snapshot.interrupt_requested_at,
                            (
                                initial_snapshot.resume_target_state.value
                                if initial_snapshot.resume_target_state is not None
                                else None
                            ),
                            initial_snapshot.context_version,
                        ),
                    )
                    self._upsert_checkpoint(initial_snapshot, initial_snapshot.updated_at)
                    created_event = self._new_event(
                        session_id=initial_snapshot.session_id,
                        sequence=1,
                        event_type=EventType.SESSION_CREATED,
                        state=initial_snapshot.state,
                        timestamp=event_timestamp,
                        payload=payload,
                    )
                    message_event = self._new_event(
                        session_id=initial_snapshot.session_id,
                        sequence=2,
                        event_type=EventType.MESSAGE_ADDED,
                        state=initial_snapshot.state,
                        timestamp=event_timestamp,
                        payload={
                            "message": initial_message.to_dict(),
                            "message_index": 0,
                        },
                    )
                    self._insert_message(
                        initial_snapshot.session_id,
                        initial_message,
                        0,
                        event_timestamp,
                    )
                    self._insert_event(created_event)
                    self._insert_event(message_event)
                    self._invoke_commit_hook()
            except sqlite3.IntegrityError as exc:
                raise PersistenceError("failed to create session") from exc
        return (created_event, message_event)

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
        return [self.get_model_call(session_id, str(row["request_id"])) for row in rows if row]

    def list_tool_calls(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._read_session_row(session_id)
            rows = self._connection.execute(
                "SELECT call_id FROM tool_calls WHERE session_id=? ORDER BY ordinal",
                (session_id,),
            ).fetchall()
        return [
            self.get_tool_call(
                session_id,
                _unscoped_tool_call_id(session_id, str(row["call_id"])),
            )
            for row in rows
            if row
        ]

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

    def commit(self, mutation: JournalMutation) -> CommitResult:
        snapshot = mutation.snapshot_after
        if snapshot.session_id != mutation.session_id:
            raise InvariantViolation("mutation session id does not match snapshot")
        if snapshot.version != mutation.expected_version:
            raise InvariantViolation(
                "snapshot version must equal the mutation expected version"
            )
        timestamp = self.clock()
        with self._lock:
            try:
                with self._write_transaction():
                    row = self._read_session_row(mutation.session_id)
                    actual_state = self._row_state(row)
                    actual_version = int(row["version"])
                    if (
                        actual_state is not mutation.expected_state
                        or actual_version != mutation.expected_version
                    ):
                        self._raise_conflict(mutation.session_id, row, mutation)
                    if mutation.lease_owner is not None:
                        current_owner = row["lease_owner"]
                        expires_at = row["lease_expires_at"]
                        if current_owner != mutation.lease_owner or (
                            expires_at is not None
                            and self._timestamp_epoch(str(expires_at)) <= self._lease_now()
                        ):
                            raise LeaseConflict(
                                f"lease for {mutation.session_id} is not held by "
                                f"{mutation.lease_owner!r}"
                            )

                    sequence = int(row["last_event_sequence"]) + 1
                    committed_version = actual_version + 1
                    requested_at = (
                        None
                        if mutation.clear_interrupt
                        else (
                            str(row["interrupt_requested_at"])
                            if row["interrupt_requested_at"] is not None
                            else snapshot.interrupt_requested_at
                        )
                    )
                    committed_snapshot = replace(
                        snapshot,
                        updated_at=timestamp,
                        version=committed_version,
                        interrupt_requested_at=requested_at,
                    )
                    updated = self._connection.execute(
                        """
                        UPDATE sessions SET
                            task=?, source_path=?, workspace_path=?, state=?,
                            policy_json=?, source_fingerprint=?, final_answer=?,
                            failure_json=?, step_count=?, model_calls=?, tool_calls=?,
                            last_event_sequence=?, version=?, created_at=?, updated_at=?,
                            interrupt_requested_at=?, resume_target_state=?, context_version=?
                        WHERE id=? AND state=? AND version=?
                        """,
                        self._session_values(
                            committed_snapshot,
                            updated_at=timestamp,
                            last_event_sequence=sequence,
                            version=committed_version,
                        )
                        + (mutation.expected_state.value, mutation.expected_version),
                    )
                    if updated.rowcount != 1:
                        current = self._read_session_row(mutation.session_id)
                        self._raise_conflict(mutation.session_id, current, mutation)

                    message_index: int | None = None
                    if mutation.message_to_append is not None:
                        message_row = self._connection.execute(
                            """
                            SELECT COALESCE(MAX(message_index), -1) + 1
                            FROM messages WHERE session_id = ?
                            """,
                            (mutation.session_id,),
                        ).fetchone()
                        message_index = int(message_row[0])
                        self._insert_message(
                            mutation.session_id,
                            mutation.message_to_append,
                            message_index,
                            timestamp,
                        )

                    if mutation.model_call is not None:
                        self._upsert_model_call(
                            mutation.session_id, mutation.model_call, timestamp
                        )
                    if mutation.tool_call is not None:
                        self._upsert_tool_call(
                            mutation.session_id, mutation.tool_call, timestamp
                        )
                    if mutation.summary is not None:
                        self._upsert_summary(
                            mutation.session_id, mutation.summary, timestamp
                        )

                    event = self._new_event(
                        session_id=mutation.session_id,
                        sequence=sequence,
                        event_type=mutation.event_type,
                        state=committed_snapshot.state,
                        timestamp=timestamp,
                        payload=mutation.payload,
                    )
                    self._insert_event(event)
                    self._upsert_checkpoint(committed_snapshot, timestamp)
                    self._invoke_commit_hook()
            except sqlite3.IntegrityError as exc:
                raise PersistenceError("journal mutation violated a database constraint") from exc
        return CommitResult(event=event, committed_version=committed_version)

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
