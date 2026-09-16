from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import math
import re
import time
import uuid

from coding_agent.domain import utc_now
from coding_agent.memory.domain import MemoryHit, MemoryQuery, MemoryScope, MemorySelection
from coding_agent.memory.sqlite import SQLiteMemoryStore


_TERM = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*|[\u3400-\u9fff]")


def lexical_terms(value: str) -> frozenset[str]:
    return frozenset(term.casefold() for term in _TERM.findall(value))


def estimate_tokens(value: str) -> int:
    return max(1, math.ceil(len(value.encode("utf-8")) / 4)) + 8


class LexicalMemoryRetriever:
    """Deterministic bounded baseline; no vectors or external retrieval service."""

    def __init__(
        self,
        store: SQLiteMemoryStore,
        *,
        clock: Callable[[], str] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] | None = None,
    ):
        self.store = store
        self.clock = clock
        self.monotonic = monotonic
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))

    def retrieve(self, query: MemoryQuery) -> MemorySelection:
        return self._select(query, audit=True)

    def preview(self, query: MemoryQuery) -> MemorySelection:
        """Select identically without writing retrieval audit or consuming an id."""

        return self._select(query, audit=False)

    def _select(self, query: MemoryQuery, *, audit: bool) -> MemorySelection:
        started = self.monotonic()
        now = datetime.fromisoformat(self.clock())
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        query_terms = lexical_terms(query.text)
        candidates: list[MemoryHit] = []
        for record in self.store.list_candidates(query):
            if record.expires_at is not None and datetime.fromisoformat(record.expires_at) <= now:
                continue
            if record.scope is MemoryScope.REPOSITORY:
                if query.repository_revision is None:
                    continue
                if record.repository_revision != query.repository_revision:
                    continue
            record_terms = lexical_terms(record.content)
            matched = tuple(sorted(query_terms & record_terms))
            if not matched:
                continue
            lexical_score = len(matched) / max(1, len(query_terms))
            score = round(lexical_score * record.confidence, 8)
            candidates.append(
                MemoryHit(
                    record=record,
                    score=score,
                    token_cost=estimate_tokens(record.content),
                    matched_terms=matched,
                )
            )
        candidates.sort(key=lambda hit: (-hit.score, hit.record.memory_id))
        selected: list[MemoryHit] = []
        total = 0
        for hit in candidates:
            if len(selected) >= query.top_k:
                break
            if total + hit.token_cost > query.token_budget:
                continue
            selected.append(hit)
            total += hit.token_cost
        duration_ms = max(0.0, (self.monotonic() - started) * 1000.0)
        query_hash = hashlib.sha256(query.text.encode("utf-8")).hexdigest()
        selection = MemorySelection(
            retrieval_id=(
                self.id_factory()
                if audit
                else f"preview:{query_hash[:16]}"
            ),
            query_hash=query_hash,
            hits=tuple(selected),
            total_token_cost=total,
            duration_ms=duration_ms,
        )
        if audit:
            self.store.record_retrieval(
                retrieval_id=selection.retrieval_id,
                query_hash=query_hash,
                query=query,
                selected=[hit.manifest() for hit in selected],
                token_cost=total,
                duration_ms=duration_ms,
                created_at=self.clock(),
            )
        return selection
