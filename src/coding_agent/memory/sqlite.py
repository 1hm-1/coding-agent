from __future__ import annotations

from collections.abc import Callable, Sequence
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from coding_agent.domain import utc_now
from coding_agent.memory.domain import (
    MemoryAuditEvent,
    MemoryQuery,
    MemoryRecord,
    MemoryStatus,
    stable_json,
)
from coding_agent.migrations import MigrationRunner


class MemoryStoreError(RuntimeError):
    """The memory store could not complete a requested operation."""


class MemoryNotFound(MemoryStoreError, LookupError):
    pass


class MemoryConflict(MemoryStoreError):
    pass


class DuplicateMemory(MemoryConflict):
    pass


class SQLiteMemoryStore:
    """SQLite authority for memory records, lifecycle events and retrieval audit."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], str] = utc_now,
        fault_injector: Callable[[str], None] | None = None,
    ):
        self.db_path = Path(db_path)
        self._database = str(db_path)
        if self._database != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.fault_injector = fault_injector
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self._database,
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self.schema_version = MigrationRunner(clock=clock).migrate(self._connection)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        self._connection.close()

    def _inject(self, stage: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(stage)

    def add_proposal(self, record: MemoryRecord, event: MemoryAuditEvent) -> None:
        if record.status is not MemoryStatus.PROPOSED:
            raise MemoryConflict("new memory must have proposed status")
        if (
            event.memory_id != record.memory_id
            or event.from_status is not None
            or event.to_status is not MemoryStatus.PROPOSED
        ):
            raise MemoryConflict("proposal audit event does not match memory record")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._insert_record(record)
                self._inject("after_memory_record_insert")
                self._insert_event(event)
                self._inject("before_memory_commit")
                self._connection.commit()
            except sqlite3.IntegrityError as exc:
                self._connection.rollback()
                message = str(exc).lower()
                if "unique" in message:
                    raise DuplicateMemory("memory id or scoped content already exists") from exc
                raise MemoryStoreError("memory proposal violates storage constraints") from exc
            except BaseException:
                self._connection.rollback()
                raise

    def transition(
        self,
        memory_id: str,
        *,
        expected_status: MemoryStatus,
        expected_version: int,
        target_status: MemoryStatus,
        event: MemoryAuditEvent,
        stale_superseded: bool = False,
    ) -> MemoryRecord:
        if (
            event.memory_id != memory_id
            or event.from_status is not expected_status
            or event.to_status is not target_status
        ):
            raise MemoryConflict("transition audit event does not match requested transition")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._get_record_locked(memory_id)
                if current.status is not expected_status or current.version != expected_version:
                    raise MemoryConflict("memory status or version changed")
                updated = current.with_status(target_status, updated_at=event.created_at)
                cursor = self._connection.execute(
                    """
                    UPDATE memory_records
                    SET status = ?, content = ?, version = ?, updated_at = ?
                    WHERE memory_id = ? AND status = ? AND version = ?
                    """,
                    (
                        updated.status.value,
                        updated.content,
                        updated.version,
                        updated.updated_at,
                        memory_id,
                        expected_status.value,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise MemoryConflict("memory transition lost an optimistic update race")
                if stale_superseded and current.supersedes is not None:
                    prior = self._get_record_locked(current.supersedes)
                    if (
                        prior.scope is not current.scope
                        or prior.scope_id != current.scope_id
                        or prior.kind is not current.kind
                    ):
                        raise MemoryConflict("superseded memory must share scope and kind")
                    if prior.status is not MemoryStatus.ACTIVE:
                        raise MemoryConflict("superseded memory is not active")
                    self._connection.execute(
                        """
                        UPDATE memory_records
                        SET status = ?, version = version + 1, updated_at = ?
                        WHERE memory_id = ? AND status = ?
                        """,
                        (
                            MemoryStatus.STALE.value,
                            event.created_at,
                            prior.memory_id,
                            MemoryStatus.ACTIVE.value,
                        ),
                    )
                    supersede_event = MemoryAuditEvent(
                        event_id=f"{event.event_id}:superseded",
                        memory_id=prior.memory_id,
                        event_type="memory_superseded",
                        actor_id=event.actor_id,
                        from_status=MemoryStatus.ACTIVE,
                        to_status=MemoryStatus.STALE,
                        payload={"superseded_by": current.memory_id},
                        created_at=event.created_at,
                    )
                    self._insert_event(supersede_event)
                self._inject("after_memory_transition")
                self._insert_event(event)
                self._inject("before_memory_commit")
                self._connection.commit()
                return updated
            except BaseException:
                self._connection.rollback()
                raise

    def get(self, memory_id: str) -> MemoryRecord:
        with self._lock:
            return self._get_record_locked(memory_id)

    def _get_record_locked(self, memory_id: str) -> MemoryRecord:
        row = self._connection.execute(
            "SELECT * FROM memory_records WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise MemoryNotFound(f"unknown memory: {memory_id}")
        return self._record_from_row(row)

    def list_candidates(self, query: MemoryQuery) -> list[MemoryRecord]:
        scope_pairs = [
            ("session", query.session_id),
            ("repository", query.repository_id),
            ("user", query.user_id),
        ]
        active_pairs = [(scope, scope_id) for scope, scope_id in scope_pairs if scope_id]
        if not active_pairs:
            return []
        clauses = " OR ".join("(scope = ? AND scope_id = ?)" for _ in active_pairs)
        parameters: list[Any] = []
        for scope, scope_id in active_pairs:
            parameters.extend((scope, scope_id))
        kind_values = [kind.value for kind in query.kinds]
        kind_slots = ",".join("?" for _ in kind_values)
        parameters.extend(kind_values)
        parameters.append(MemoryStatus.ACTIVE.value)
        sql = (
            "SELECT * FROM memory_records WHERE ("
            + clauses
            + f") AND kind IN ({kind_slots}) AND status = ? ORDER BY memory_id"
        )
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [self._record_from_row(row) for row in rows]

    def list_events(self, memory_id: str) -> list[MemoryAuditEvent]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM memory_events
                WHERE memory_id = ? ORDER BY created_at, event_id
                """,
                (memory_id,),
            ).fetchall()
        return [
            MemoryAuditEvent(
                event_id=str(row["event_id"]),
                memory_id=str(row["memory_id"]),
                event_type=str(row["event_type"]),
                actor_id=str(row["actor_id"]),
                from_status=(
                    MemoryStatus(str(row["from_status"]))
                    if row["from_status"] is not None
                    else None
                ),
                to_status=MemoryStatus(str(row["to_status"])),
                payload=dict(json.loads(str(row["payload_json"]))),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def record_retrieval(
        self,
        *,
        retrieval_id: str,
        query_hash: str,
        query: MemoryQuery,
        selected: Sequence[dict[str, Any]],
        token_cost: int,
        duration_ms: float,
        created_at: str,
    ) -> None:
        with self._lock:
            try:
                self._connection.execute(
                    """
                    INSERT INTO memory_retrievals(
                        retrieval_id, query_hash, session_id, repository_id, user_id,
                        repository_revision, selected_json, token_cost, duration_ms, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        retrieval_id,
                        query_hash,
                        query.session_id,
                        query.repository_id,
                        query.user_id,
                        query.repository_revision,
                        stable_json(list(selected)),
                        token_cost,
                        duration_ms,
                        created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise MemoryConflict("duplicate retrieval id") from exc

    def get_retrieval(self, retrieval_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memory_retrievals WHERE retrieval_id = ?", (retrieval_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["selected"] = json.loads(str(result.pop("selected_json")))
        return result

    def _insert_record(self, record: MemoryRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO memory_records(
                memory_id, schema_version, scope, scope_id, kind, content,
                source_run_id, source_agent_id, source_event_refs_json,
                repository_revision, confidence, created_at, expires_at, status,
                supersedes, content_hash, version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.memory_id,
                record.schema_version,
                record.scope.value,
                record.scope_id,
                record.kind.value,
                record.content,
                record.source_run_id,
                record.source_agent_id,
                stable_json(list(record.source_event_refs)),
                record.repository_revision,
                record.confidence,
                record.created_at,
                record.expires_at,
                record.status.value,
                record.supersedes,
                record.content_hash,
                record.version,
                record.updated_at,
            ),
        )

    def _insert_event(self, event: MemoryAuditEvent) -> None:
        self._connection.execute(
            """
            INSERT INTO memory_events(
                event_id, memory_id, event_type, actor_id, from_status,
                to_status, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.memory_id,
                event.event_type,
                event.actor_id,
                event.from_status.value if event.from_status else None,
                event.to_status.value,
                stable_json(event.payload),
                event.created_at,
            ),
        )

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord.from_dict(
            {
                **dict(row),
                "source_event_refs": json.loads(str(row["source_event_refs_json"])),
            }
        )
