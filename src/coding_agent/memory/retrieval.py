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

# Question scaffolding and general coding-agent vocabulary carry little evidence that a
# record answers a query.  Keeping a small non-zero weight lets short queries remain
# usable without allowing a shared "what/the/user" token to trigger retrieval.
_COMMON_TERMS = frozenset(
    {
        "a",
        "an",
        "and",
        "be",
        "do",
        "for",
        "how",
        "is",
        "it",
        "must",
        "of",
        "or",
        "required",
        "should",
        "the",
        "this",
        "to",
        "user",
        "what",
        "when",
        "where",
        "which",
        "with",
    }
)
_COMMON_WEIGHT = 0.1
_FINAL_INFORMATIVE_BOOST = 1.5
_MIN_RELEVANCE = 0.60
_RELATIVE_SCORE_FLOOR = 0.72
_SCOPE_PRIORITY = {
    MemoryScope.SESSION: 3,
    MemoryScope.REPOSITORY: 2,
    MemoryScope.USER: 1,
}


def _normalize_term(term: str) -> str:
    value = term.casefold()
    # Small lexical equivalence classes cover common inflection and wording without
    # turning this deterministic retriever into a semantic/vector system.
    aliases = {
        "included": "include",
        "includes": "include",
        "including": "include",
        "use": "include",
        "used": "include",
        "uses": "include",
        "using": "include",
        "rounded": "round",
        "rounding": "round",
    }
    if value in aliases:
        return aliases[value]
    if len(value) > 4 and value.endswith("ies"):
        return value[:-3] + "y"
    if len(value) > 3 and value.endswith("s") and not value.endswith(("ss", "us", "is")):
        return value[:-1]
    return value


def _lexical_sequence(value: str) -> tuple[str, ...]:
    return tuple(_normalize_term(term) for term in _TERM.findall(value))


def lexical_terms(value: str) -> frozenset[str]:
    return frozenset(_lexical_sequence(value))


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
        query_sequence = _lexical_sequence(query.text)
        query_terms = frozenset(query_sequence)
        informative = tuple(term for term in query_sequence if term not in _COMMON_TERMS)
        final_informative = informative[-1] if informative else None

        def query_weight(term: str) -> float:
            if term in _COMMON_TERMS:
                return _COMMON_WEIGHT
            if term == final_informative:
                return _FINAL_INFORMATIVE_BOOST
            return 1.0

        total_query_weight = sum(query_weight(term) for term in query_terms)
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
            matched_informative = (query_terms - _COMMON_TERMS) & record_terms
            if not matched or not matched_informative:
                continue
            query_coverage = sum(query_weight(term) for term in matched) / max(
                _COMMON_WEIGHT,
                total_query_weight,
            )
            informative_record_terms = record_terms - _COMMON_TERMS
            record_precision = len(matched_informative & informative_record_terms) / max(
                1,
                len(informative_record_terms),
            )
            lexical_score = (0.85 * query_coverage) + (0.15 * record_precision)
            if lexical_score < _MIN_RELEVANCE:
                continue
            score = round(lexical_score * record.confidence, 8)
            candidates.append(
                MemoryHit(
                    record=record,
                    score=score,
                    token_cost=estimate_tokens(record.content),
                    matched_terms=matched,
                )
            )
        candidates.sort(
            key=lambda hit: (
                -hit.score,
                -_SCOPE_PRIORITY[hit.record.scope],
                hit.token_cost,
                hit.record.memory_id,
            )
        )
        selected: list[MemoryHit] = []
        total = 0
        dynamic_floor = (
            candidates[0].score * _RELATIVE_SCORE_FLOOR if candidates else 0.0
        )
        for hit in candidates:
            if len(selected) >= query.top_k:
                break
            # Once marginal relevance drops materially below the best hit, spending
            # more Context tokens is more likely to inject topical noise than evidence.
            if hit.score < dynamic_floor:
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
