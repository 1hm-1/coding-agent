from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol
import uuid

from coding_agent.domain import utc_now
from coding_agent.memory.domain import (
    MemoryAuditEvent,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
)
from coding_agent.memory.policy import MemoryPolicy, MemoryWriteContext
from coding_agent.memory.sqlite import SQLiteMemoryStore


class ProvenanceJournal(Protocol):
    def list_events(self, session_id: str) -> Sequence[object]:
        ...


class JournalProvenanceValidator:
    """Validate source event ids against the committed Runtime journal."""

    def __init__(
        self,
        journal: ProvenanceJournal,
        *,
        allowed_agent_ids: frozenset[str] = frozenset({"runtime"}),
    ):
        self.journal = journal
        self.allowed_agent_ids = allowed_agent_ids

    def __call__(self, record: MemoryRecord) -> bool:
        if record.source_agent_id not in self.allowed_agent_ids:
            return False
        try:
            events = self.journal.list_events(record.source_run_id)
        except (KeyError, LookupError, ValueError):
            return False
        committed_ids = {
            str(event_id)
            for event in events
            if (event_id := getattr(event, "event_id", None)) is not None
        }
        return bool(committed_ids) and set(record.source_event_refs).issubset(committed_ids)


class MemoryService:
    """Trusted lifecycle boundary; proposals never become active implicitly."""

    _allowed_transitions = {
        MemoryStatus.PROPOSED: frozenset(
            {MemoryStatus.ACTIVE, MemoryStatus.REJECTED, MemoryStatus.DELETED}
        ),
        MemoryStatus.ACTIVE: frozenset({MemoryStatus.STALE, MemoryStatus.DELETED}),
        MemoryStatus.STALE: frozenset({MemoryStatus.DELETED}),
        MemoryStatus.REJECTED: frozenset({MemoryStatus.DELETED}),
        MemoryStatus.DELETED: frozenset(),
    }

    def __init__(
        self,
        store: SQLiteMemoryStore,
        *,
        policy: MemoryPolicy | None = None,
        provenance_validator: Callable[[MemoryRecord], bool] | None = None,
        clock: Callable[[], str] = utc_now,
        id_factory: Callable[[], str] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ):
        self.store = store
        self.policy = policy or MemoryPolicy()
        self.provenance_validator = provenance_validator or (lambda record: False)
        self.clock = clock
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self.event_id_factory = event_id_factory or (lambda: str(uuid.uuid4()))

    def propose(
        self,
        *,
        context: MemoryWriteContext,
        scope: MemoryScope,
        kind: MemoryKind,
        content: str,
        source_run_id: str,
        source_agent_id: str,
        source_event_refs: Sequence[str],
        confidence: float,
        repository_revision: str | None = None,
        expires_at: str | None = None,
        supersedes: str | None = None,
        memory_id: str | None = None,
    ) -> MemoryRecord:
        now = self.clock()
        scope_id = context.scope_id(scope)
        if scope_id is None:
            # Let policy produce the stable containment error without creating
            # a structurally invalid empty scope id.
            scope_id = "unowned"
        record = MemoryRecord.proposed(
            memory_id=memory_id or self.id_factory(),
            scope=scope,
            scope_id=scope_id,
            kind=kind,
            content=content,
            source_run_id=source_run_id,
            source_agent_id=source_agent_id,
            source_event_refs=source_event_refs,
            repository_revision=repository_revision,
            confidence=confidence,
            created_at=now,
            expires_at=expires_at,
            supersedes=supersedes,
        )
        self.policy.validate_proposal(
            record,
            context,
            provenance_valid=self.provenance_validator(record),
        )
        if supersedes is not None:
            prior = self.store.get(supersedes)
            self.policy.validate_actor(prior, context)
            if (
                prior.status is not MemoryStatus.ACTIVE
                or prior.scope is not record.scope
                or prior.scope_id != record.scope_id
                or prior.kind is not record.kind
            ):
                raise ValueError("superseded memory must be active with matching scope and kind")
        event = self._event(
            record,
            context,
            event_type="memory_proposed",
            from_status=None,
            to_status=MemoryStatus.PROPOSED,
            payload={"content_hash": record.content_hash},
            created_at=now,
        )
        self.store.add_proposal(record, event)
        return record

    def activate(
        self,
        memory_id: str,
        *,
        context: MemoryWriteContext,
        expected_version: int = 0,
    ) -> MemoryRecord:
        return self._transition(
            memory_id,
            context=context,
            expected_status=MemoryStatus.PROPOSED,
            expected_version=expected_version,
            target_status=MemoryStatus.ACTIVE,
            event_type="memory_activated",
            stale_superseded=True,
        )

    def reject(
        self,
        memory_id: str,
        *,
        context: MemoryWriteContext,
        expected_version: int = 0,
        reason: str = "",
    ) -> MemoryRecord:
        return self._transition(
            memory_id,
            context=context,
            expected_status=MemoryStatus.PROPOSED,
            expected_version=expected_version,
            target_status=MemoryStatus.REJECTED,
            event_type="memory_rejected",
            payload={"reason": reason[:256]},
        )

    def mark_stale(
        self,
        memory_id: str,
        *,
        context: MemoryWriteContext,
        expected_version: int,
        reason: str = "",
    ) -> MemoryRecord:
        return self._transition(
            memory_id,
            context=context,
            expected_status=MemoryStatus.ACTIVE,
            expected_version=expected_version,
            target_status=MemoryStatus.STALE,
            event_type="memory_stale",
            payload={"reason": reason[:256]},
        )

    def delete(
        self,
        memory_id: str,
        *,
        context: MemoryWriteContext,
        expected_version: int,
    ) -> MemoryRecord:
        current = self.store.get(memory_id)
        return self._transition(
            memory_id,
            context=context,
            expected_status=current.status,
            expected_version=expected_version,
            target_status=MemoryStatus.DELETED,
            event_type="memory_deleted",
            payload={"content_hash": current.content_hash},
        )

    def _transition(
        self,
        memory_id: str,
        *,
        context: MemoryWriteContext,
        expected_status: MemoryStatus,
        expected_version: int,
        target_status: MemoryStatus,
        event_type: str,
        payload: dict[str, object] | None = None,
        stale_superseded: bool = False,
    ) -> MemoryRecord:
        current = self.store.get(memory_id)
        self.policy.validate_actor(current, context)
        if target_status not in self._allowed_transitions[current.status]:
            raise ValueError(f"invalid memory transition: {current.status.value} -> {target_status.value}")
        now = self.clock()
        event = self._event(
            current,
            context,
            event_type=event_type,
            from_status=current.status,
            to_status=target_status,
            payload=payload or {},
            created_at=now,
        )
        return self.store.transition(
            memory_id,
            expected_status=expected_status,
            expected_version=expected_version,
            target_status=target_status,
            event=event,
            stale_superseded=stale_superseded,
        )

    def _event(
        self,
        record: MemoryRecord,
        context: MemoryWriteContext,
        *,
        event_type: str,
        from_status: MemoryStatus | None,
        to_status: MemoryStatus,
        payload: dict[str, object],
        created_at: str,
    ) -> MemoryAuditEvent:
        return MemoryAuditEvent(
            event_id=self.event_id_factory(),
            memory_id=record.memory_id,
            event_type=event_type,
            actor_id=context.actor_id,
            from_status=from_status,
            to_status=to_status,
            payload=dict(payload),
            created_at=created_at,
        )
