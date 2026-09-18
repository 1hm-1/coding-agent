"""Explicit offline candidate retrievers used by the S3.7 development spike.

Nothing in this module is wired into AgentApplication, headless Runtime, or the
production ``LexicalMemoryRetriever``.  Candidate requests deliberately contain
only production-available query fields and already-eligible Memory records.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
import re
import time
from typing import Any, Protocol, cast

from coding_agent.domain import JsonObject
from coding_agent.memory.domain import MemoryQuery, MemoryRecord, MemoryScope, stable_json
from coding_agent.memory.retrieval import (
    LexicalMemoryRetriever,
    _COMMON_TERMS,
    _COMMON_WEIGHT,
    _FINAL_INFORMATIVE_BOOST,
    _MIN_RELEVANCE,
    _RELATIVE_SCORE_FLOOR,
    _SCOPE_PRIORITY,
    _lexical_sequence,
    estimate_tokens,
    lexical_terms,
)
from coding_agent.memory.sqlite import SQLiteMemoryStore


_METADATA_KEYS = frozenset(
    {
        "fact_type",
        "entities",
        "concept_keys",
        "repository_component",
        "validity",
        "revision",
    }
)
_INDEXED_METADATA_FIELDS = (
    "fact_type",
    "entities",
    "concept_keys",
    "repository_component",
)
_METADATA_ATOM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_CONTROL_WORDS = frozenset(
    {
        "assistant",
        "command",
        "disregard",
        "execute",
        "ignore",
        "instruction",
        "instructions",
        "override",
        "permission",
        "permissions",
        "shell",
        "system",
        "tool",
    }
)
_VALIDITY_VALUES = frozenset(
    {"current", "stale", "deleted", "policy_rejected", "revision_mismatch"}
)


class CandidateInputError(ValueError):
    """Candidate input violates the label or metadata isolation contract."""


@dataclass(frozen=True)
class TrustedRecordMetadata:
    fact_type: str
    entities: tuple[str, ...]
    concept_keys: tuple[str, ...]
    repository_component: str
    validity: str
    revision: str

    def indexed_values(self) -> tuple[str, ...]:
        return (
            self.fact_type,
            *self.entities,
            *self.concept_keys,
            self.repository_component,
        )

    def to_dict(self) -> JsonObject:
        return {
            "fact_type": self.fact_type,
            "entities": list(self.entities),
            "concept_keys": list(self.concept_keys),
            "repository_component": self.repository_component,
            "validity": self.validity,
            "revision": self.revision,
        }


def _validated_atom(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _METADATA_ATOM.fullmatch(value):
        raise CandidateInputError(f"{field} must be a bounded metadata atom")
    pieces = frozenset(piece for piece in re.split(r"[._/-]+", value.casefold()) if piece)
    if pieces & _CONTROL_WORDS:
        raise CandidateInputError(f"{field} contains control or permission vocabulary")
    return value


def _validated_atoms(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise CandidateInputError(f"{field} must be an array of at most 32 atoms")
    result = tuple(_validated_atom(item, field) for item in value)
    if len(result) != len(set(result)):
        raise CandidateInputError(f"{field} cannot contain duplicates")
    return result


def validate_record_metadata(raw: Mapping[str, Any]) -> TrustedRecordMetadata:
    """Validate fact-shaped metadata without granting it authority.

    Scope, lifecycle status, expiry, and revision eligibility remain properties of
    ``MemoryRecord`` and ``SQLiteMemoryStore``.  The metadata revision/validity
    values are validated for auditability but are never used to admit a record.
    """

    if set(raw) != _METADATA_KEYS:
        raise CandidateInputError("record metadata must use the fixed fact schema")
    validity = _validated_atom(raw.get("validity"), "validity")
    if validity not in _VALIDITY_VALUES:
        raise CandidateInputError("metadata validity is not recognized")
    return TrustedRecordMetadata(
        fact_type=_validated_atom(raw.get("fact_type"), "fact_type"),
        entities=_validated_atoms(raw.get("entities"), "entities"),
        concept_keys=_validated_atoms(raw.get("concept_keys"), "concept_keys"),
        repository_component=_validated_atom(
            raw.get("repository_component"), "repository_component"
        ),
        validity=validity,
        revision=_validated_atom(raw.get("revision"), "revision"),
    )


@dataclass(frozen=True)
class ProductionQueryMetadata:
    """Metadata derived only from query text and repository component names."""

    concept_terms: tuple[str, ...]
    identifier_terms: tuple[str, ...]
    repository_components: tuple[str, ...]

    def indexed_values(self) -> tuple[str, ...]:
        return (*self.concept_terms, *self.identifier_terms, *self.repository_components)

    def to_dict(self) -> JsonObject:
        return {
            "concept_terms": list(self.concept_terms),
            "identifier_terms": list(self.identifier_terms),
            "repository_components": list(self.repository_components),
        }


def derive_query_metadata(
    query_text: str,
    *,
    repository_components: Sequence[str],
) -> ProductionQueryMetadata:
    """Derive query facets without suite labels, oracles, or expected answers."""

    sequence = _lexical_sequence(query_text)
    concepts = tuple(dict.fromkeys(term for term in sequence if term not in _COMMON_TERMS))
    identifiers = tuple(
        dict.fromkeys(term for term in concepts if "_" in term or "-" in term or "." in term)
    )
    query_terms = frozenset(concepts)
    matched_components: list[str] = []
    for raw_component in sorted(repository_components):
        component = _validated_atom(raw_component, "query repository_component")
        component_terms = frozenset(_lexical_sequence(component.replace("/", " ")))
        if query_terms & component_terms:
            matched_components.append(component)
    return ProductionQueryMetadata(
        concept_terms=concepts,
        identifier_terms=identifiers,
        repository_components=tuple(matched_components),
    )


@dataclass(frozen=True)
class CandidateRecord:
    record: MemoryRecord
    metadata: TrustedRecordMetadata

    def to_dict(self) -> JsonObject:
        return {
            "memory_id": self.record.memory_id,
            "scope": self.record.scope.value,
            "scope_id": self.record.scope_id,
            "repository_revision": self.record.repository_revision,
            "kind": self.record.kind.value,
            "content": self.record.content,
            "confidence": self.record.confidence,
            "metadata": self.metadata.to_dict(),
        }


@dataclass(frozen=True)
class CandidateRequest:
    query: MemoryQuery
    query_metadata: ProductionQueryMetadata
    records: tuple[CandidateRecord, ...]

    def __post_init__(self) -> None:
        ids = [item.record.memory_id for item in self.records]
        if len(ids) != len(set(ids)):
            raise CandidateInputError("candidate request contains duplicate memory IDs")

    def to_dict(self) -> JsonObject:
        """Return the complete backend-visible payload for isolation assertions."""

        return {
            "query": {
                "text": self.query.text,
                "session_id": self.query.session_id,
                "repository_id": self.query.repository_id,
                "user_id": self.query.user_id,
                "repository_revision": self.query.repository_revision,
                "top_k": self.query.top_k,
                "token_budget": self.query.token_budget,
                "kinds": [kind.value for kind in self.query.kinds],
            },
            "query_metadata": self.query_metadata.to_dict(),
            "records": [item.to_dict() for item in self.records],
        }


def eligible_candidate_records(
    store: SQLiteMemoryStore,
    query: MemoryQuery,
    metadata_by_id: Mapping[str, TrustedRecordMetadata],
    *,
    now: str,
) -> tuple[CandidateRecord, ...]:
    """Reuse SQLite scope/status authority, then apply production expiry/revision gates."""

    parsed_now = datetime.fromisoformat(now)
    if parsed_now.tzinfo is None:
        parsed_now = parsed_now.replace(tzinfo=timezone.utc)
    eligible: list[CandidateRecord] = []
    for record in store.list_candidates(query):
        if record.expires_at is not None and datetime.fromisoformat(record.expires_at) <= parsed_now:
            continue
        if record.scope is MemoryScope.REPOSITORY:
            if query.repository_revision is None:
                continue
            if record.repository_revision != query.repository_revision:
                continue
        try:
            metadata = metadata_by_id[record.memory_id]
        except KeyError as exc:
            raise CandidateInputError(
                f"eligible record has no validated metadata: {record.memory_id}"
            ) from exc
        eligible.append(CandidateRecord(record=record, metadata=metadata))
    return tuple(eligible)


@dataclass(frozen=True)
class ScoredRecord:
    record: MemoryRecord
    score: float
    components: Mapping[str, float | str]
    rejection: str | None = None

    def to_dict(self) -> JsonObject:
        stable_components = {
            key: round(value, 12) if isinstance(value, float) else value
            for key, value in self.components.items()
        }
        return {
            "memory_id": self.record.memory_id,
            "score": self.score,
            "token_cost": estimate_tokens(self.record.content),
            "components": stable_components,
            "rejection": self.rejection,
        }


@dataclass(frozen=True)
class CandidateResult:
    backend: str
    availability: str
    parameters: Mapping[str, Any]
    eligible_ids: tuple[str, ...]
    scored_records: tuple[ScoredRecord, ...]
    ranked_ids: tuple[str, ...]
    threshold_rejected_ids: tuple[str, ...]
    relative_floor_rejected_ids: tuple[str, ...]
    token_rejected_ids: tuple[str, ...]
    top_k_rejected_ids: tuple[str, ...]
    selected_ids: tuple[str, ...]
    selected_token_cost: int
    latency_ms: float
    unavailable_reason: str | None = None

    def deterministic_dict(self) -> JsonObject:
        return {
            "backend": self.backend,
            "availability": self.availability,
            "parameters": dict(self.parameters),
            "eligible_ids": list(self.eligible_ids),
            "score_components": [item.to_dict() for item in self.scored_records],
            "ranked_ids": list(self.ranked_ids),
            "threshold_rejected_ids": list(self.threshold_rejected_ids),
            "relative_floor_rejected_ids": list(self.relative_floor_rejected_ids),
            "token_rejected_ids": list(self.token_rejected_ids),
            "top_k_rejected_ids": list(self.top_k_rejected_ids),
            "selected_ids": list(self.selected_ids),
            "selected_token_cost": self.selected_token_cost,
            "unavailable_reason": self.unavailable_reason,
        }

    def to_dict(self) -> JsonObject:
        result = self.deterministic_dict()
        result["latency_ms"] = self.latency_ms
        return result


class CandidateBackend(Protocol):
    name: str

    @property
    def parameters(self) -> Mapping[str, Any]: ...

    def retrieve(self, request: CandidateRequest) -> CandidateResult: ...


def _sort_key(item: ScoredRecord) -> tuple[float, int, int, str]:
    return (
        -item.score,
        -_SCOPE_PRIORITY[item.record.scope],
        estimate_tokens(item.record.content),
        item.record.memory_id,
    )


def _finalize(
    *,
    backend: str,
    parameters: Mapping[str, Any],
    request: CandidateRequest,
    scores: Sequence[ScoredRecord],
    relative_floor: float,
    started: float,
) -> CandidateResult:
    threshold_rejected = tuple(
        sorted(item.record.memory_id for item in scores if item.rejection is not None)
    )
    ranked = sorted((item for item in scores if item.rejection is None), key=_sort_key)
    dynamic_floor = ranked[0].score * relative_floor if ranked else 0.0
    selected: list[ScoredRecord] = []
    relative_rejected: list[str] = []
    token_rejected: list[str] = []
    top_k_rejected: list[str] = []
    token_total = 0
    for index, item in enumerate(ranked):
        if item.score < dynamic_floor:
            relative_rejected.extend(rest.record.memory_id for rest in ranked[index:])
            break
        if len(selected) >= request.query.top_k:
            top_k_rejected.extend(rest.record.memory_id for rest in ranked[index:])
            break
        token_cost = estimate_tokens(item.record.content)
        if token_total + token_cost > request.query.token_budget:
            token_rejected.append(item.record.memory_id)
            continue
        selected.append(item)
        token_total += token_cost
    return CandidateResult(
        backend=backend,
        availability="available",
        parameters=parameters,
        eligible_ids=tuple(sorted(item.record.memory_id for item in request.records)),
        scored_records=tuple(sorted(scores, key=lambda item: item.record.memory_id)),
        ranked_ids=tuple(item.record.memory_id for item in ranked),
        threshold_rejected_ids=threshold_rejected,
        relative_floor_rejected_ids=tuple(relative_rejected),
        token_rejected_ids=tuple(token_rejected),
        top_k_rejected_ids=tuple(top_k_rejected),
        selected_ids=tuple(item.record.memory_id for item in selected),
        selected_token_cost=token_total,
        latency_ms=max(0.0, (time.monotonic() - started) * 1000.0),
    )


class _EligibleStore:
    def __init__(self, records: Sequence[CandidateRecord]):
        self.records = [item.record for item in records]

    def list_candidates(self, _query: MemoryQuery) -> list[MemoryRecord]:
        return list(self.records)

    def record_retrieval(self, **_kwargs: Any) -> None:
        raise AssertionError("lexical control must use preview without audit writes")


class LexicalControlBackend:
    """Trace the frozen policy, then verify selection with the production class."""

    name = "lexical-control"

    def __init__(self, *, now: str):
        self.now = now

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            "implementation": "LexicalMemoryRetriever@f1d03cf",
            "minimum_relevance": _MIN_RELEVANCE,
            "relative_score_floor": _RELATIVE_SCORE_FLOOR,
            "common_weight": _COMMON_WEIGHT,
            "final_informative_boost": _FINAL_INFORMATIVE_BOOST,
        }

    def retrieve(self, request: CandidateRequest) -> CandidateResult:
        started = time.monotonic()
        sequence = _lexical_sequence(request.query.text)
        query_terms = frozenset(sequence)
        informative = tuple(term for term in sequence if term not in _COMMON_TERMS)
        final_informative = informative[-1] if informative else None

        def weight(term: str) -> float:
            if term in _COMMON_TERMS:
                return _COMMON_WEIGHT
            if term == final_informative:
                return _FINAL_INFORMATIVE_BOOST
            return 1.0

        total_weight = sum(weight(term) for term in query_terms)
        scores: list[ScoredRecord] = []
        for item in request.records:
            record_terms = lexical_terms(item.record.content)
            matched = query_terms & record_terms
            informative_match = (query_terms - _COMMON_TERMS) & record_terms
            if not matched or not informative_match:
                scores.append(
                    ScoredRecord(
                        record=item.record,
                        score=0.0,
                        components={"reason": "no_lexical_overlap"},
                        rejection="no_lexical_overlap",
                    )
                )
                continue
            coverage = sum(weight(term) for term in matched) / max(
                _COMMON_WEIGHT, total_weight
            )
            informative_record_terms = record_terms - _COMMON_TERMS
            precision = len(informative_match & informative_record_terms) / max(
                1, len(informative_record_terms)
            )
            lexical_score = (0.85 * coverage) + (0.15 * precision)
            final_score = round(lexical_score * item.record.confidence, 8)
            scores.append(
                ScoredRecord(
                    record=item.record,
                    score=final_score,
                    components={
                        "query_coverage": coverage,
                        "record_precision": precision,
                        "lexical_score": lexical_score,
                        "confidence": item.record.confidence,
                    },
                    rejection=(
                        "below_absolute_threshold"
                        if lexical_score < _MIN_RELEVANCE
                        else None
                    ),
                )
            )
        traced = _finalize(
            backend=self.name,
            parameters=self.parameters,
            request=request,
            scores=scores,
            relative_floor=_RELATIVE_SCORE_FLOOR,
            started=started,
        )
        control_store = cast(SQLiteMemoryStore, _EligibleStore(request.records))
        control = LexicalMemoryRetriever(control_store, clock=lambda: self.now).preview(request.query)
        actual_ids = tuple(hit.record.memory_id for hit in control.hits)
        if actual_ids != traced.selected_ids:
            raise AssertionError("lexical trace diverged from frozen production retriever")
        return traced


def _bm25_scores(
    query_terms: Sequence[str],
    documents: Sequence[Sequence[str]],
    *,
    k1: float,
    b: float,
) -> list[float]:
    if not documents:
        return []
    lengths = [len(document) for document in documents]
    average_length = sum(lengths) / len(lengths) if lengths else 1.0
    query_set = frozenset(query_terms)
    frequencies = [Counter(document) for document in documents]
    document_frequency = {
        term: sum(1 for frequency in frequencies if frequency.get(term, 0) > 0)
        for term in query_set
    }
    scores: list[float] = []
    count = len(documents)
    for length, frequency in zip(lengths, frequencies):
        score = 0.0
        for term in query_set:
            term_frequency = frequency.get(term, 0)
            if term_frequency == 0:
                continue
            seen = document_frequency[term]
            inverse_frequency = math.log(1.0 + ((count - seen + 0.5) / (seen + 0.5)))
            normalization = term_frequency + k1 * (
                1.0 - b + b * (length / max(1.0, average_length))
            )
            score += inverse_frequency * (term_frequency * (k1 + 1.0)) / normalization
        scores.append(score)
    return scores


class BM25ContentBackend:
    name = "bm25-content"

    def __init__(
        self,
        *,
        k1: float = 1.2,
        b: float = 0.75,
        minimum_score: float = 0.10,
        relative_floor: float = 0.35,
    ):
        self.k1 = k1
        self.b = b
        self.minimum_score = minimum_score
        self.relative_floor = relative_floor

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            "k1": self.k1,
            "b": self.b,
            "minimum_score": self.minimum_score,
            "relative_score_floor": self.relative_floor,
            "indexed_fields": ["content"],
        }

    def retrieve(self, request: CandidateRequest) -> CandidateResult:
        started = time.monotonic()
        query_terms = tuple(
            term for term in _lexical_sequence(request.query.text) if term not in _COMMON_TERMS
        )
        documents = [
            tuple(term for term in _lexical_sequence(item.record.content) if term not in _COMMON_TERMS)
            for item in request.records
        ]
        raw_scores = _bm25_scores(query_terms, documents, k1=self.k1, b=self.b)
        scores = [
            ScoredRecord(
                record=item.record,
                score=round(raw_score * item.record.confidence, 8),
                components={
                    "content_bm25": raw_score,
                    "confidence": item.record.confidence,
                },
                rejection=("below_absolute_threshold" if raw_score < self.minimum_score else None),
            )
            for item, raw_score in zip(request.records, raw_scores)
        ]
        return _finalize(
            backend=self.name,
            parameters=self.parameters,
            request=request,
            scores=scores,
            relative_floor=self.relative_floor,
            started=started,
        )


class StructuredBM25Backend(BM25ContentBackend):
    name = "structured-bm25"

    def __init__(self, *, metadata_weight: float = 0.50, **kwargs: float):
        super().__init__(**kwargs)
        self.metadata_weight = metadata_weight

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            **super().parameters,
            "metadata_weight": self.metadata_weight,
            "indexed_fields": ["content", *_INDEXED_METADATA_FIELDS],
            "query_metadata_source": "deterministic_query_text_and_repository_components",
            "authority_fields": [],
        }

    def retrieve(self, request: CandidateRequest) -> CandidateResult:
        started = time.monotonic()
        content_query = tuple(
            term for term in _lexical_sequence(request.query.text) if term not in _COMMON_TERMS
        )
        metadata_query = tuple(
            term
            for value in request.query_metadata.indexed_values()
            for term in _lexical_sequence(value.replace("/", " "))
            if term not in _COMMON_TERMS
        )
        content_documents = [
            tuple(term for term in _lexical_sequence(item.record.content) if term not in _COMMON_TERMS)
            for item in request.records
        ]
        metadata_documents = [
            tuple(
                term
                for value in item.metadata.indexed_values()
                for term in _lexical_sequence(value.replace("/", " "))
                if term not in _COMMON_TERMS
            )
            for item in request.records
        ]
        content_scores = _bm25_scores(
            content_query, content_documents, k1=self.k1, b=self.b
        )
        metadata_scores = _bm25_scores(
            metadata_query, metadata_documents, k1=self.k1, b=self.b
        )
        scores: list[ScoredRecord] = []
        for item, content_score, metadata_score in zip(
            request.records, content_scores, metadata_scores
        ):
            combined = content_score + (self.metadata_weight * metadata_score)
            scores.append(
                ScoredRecord(
                    record=item.record,
                    score=round(combined * item.record.confidence, 8),
                    components={
                        "content_bm25": content_score,
                        "metadata_bm25": metadata_score,
                        "metadata_weight": self.metadata_weight,
                        "confidence": item.record.confidence,
                    },
                    rejection=(
                        "below_absolute_threshold"
                        if combined < self.minimum_score
                        else None
                    ),
                )
            )
        return _finalize(
            backend=self.name,
            parameters=self.parameters,
            request=request,
            scores=scores,
            relative_floor=self.relative_floor,
            started=started,
        )


class EmbeddingAdapter(Protocol):
    """Explicit adapter boundary; implementations must return real model vectors."""

    @property
    def backend_id(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class EmbeddingHybridBackend(BM25ContentBackend):
    name = "embedding-hybrid"

    def __init__(
        self,
        *,
        adapter: EmbeddingAdapter | None = None,
        embedding_weight: float = 0.65,
        **kwargs: float,
    ):
        super().__init__(**kwargs)
        self.adapter = adapter
        self.embedding_weight = embedding_weight

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            **super().parameters,
            "embedding_weight": self.embedding_weight,
            "adapter": self.adapter.backend_id if self.adapter is not None else None,
            "fallback_simulation": False,
        }

    def retrieve(self, request: CandidateRequest) -> CandidateResult:
        if self.adapter is None:
            return CandidateResult(
                backend=self.name,
                availability="unavailable",
                parameters=self.parameters,
                eligible_ids=tuple(sorted(item.record.memory_id for item in request.records)),
                scored_records=(),
                ranked_ids=(),
                threshold_rejected_ids=(),
                relative_floor_rejected_ids=(),
                token_rejected_ids=(),
                top_k_rejected_ids=(),
                selected_ids=(),
                selected_token_cost=0,
                latency_ms=0.0,
                unavailable_reason="no_real_embedding_adapter_configured",
            )
        started = time.monotonic()
        texts = [request.query.text, *(item.record.content for item in request.records)]
        vectors = [tuple(vector) for vector in self.adapter.embed(texts)]
        if len(vectors) != len(texts) or not vectors or not vectors[0]:
            raise CandidateInputError("embedding adapter returned an invalid vector count")
        dimension = len(vectors[0])
        if any(len(vector) != dimension for vector in vectors):
            raise CandidateInputError("embedding adapter returned inconsistent dimensions")
        if any(not math.isfinite(value) for vector in vectors for value in vector):
            raise CandidateInputError("embedding adapter returned a non-finite value")

        def cosine(left: Sequence[float], right: Sequence[float]) -> float:
            left_norm = math.sqrt(sum(value * value for value in left))
            right_norm = math.sqrt(sum(value * value for value in right))
            if left_norm == 0.0 or right_norm == 0.0:
                return 0.0
            return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)

        content_query = tuple(
            term for term in _lexical_sequence(request.query.text) if term not in _COMMON_TERMS
        )
        content_documents = [
            tuple(term for term in _lexical_sequence(item.record.content) if term not in _COMMON_TERMS)
            for item in request.records
        ]
        lexical_scores = _bm25_scores(
            content_query, content_documents, k1=self.k1, b=self.b
        )
        scores: list[ScoredRecord] = []
        for item, lexical_score, vector in zip(request.records, lexical_scores, vectors[1:]):
            semantic_score = max(0.0, cosine(vectors[0], vector))
            combined = ((1.0 - self.embedding_weight) * lexical_score) + (
                self.embedding_weight * semantic_score
            )
            scores.append(
                ScoredRecord(
                    record=item.record,
                    score=round(combined * item.record.confidence, 8),
                    components={
                        "content_bm25": lexical_score,
                        "embedding_cosine": semantic_score,
                        "embedding_weight": self.embedding_weight,
                        "confidence": item.record.confidence,
                    },
                    rejection=(
                        "below_absolute_threshold"
                        if combined < self.minimum_score
                        else None
                    ),
                )
            )
        return _finalize(
            backend=self.name,
            parameters=self.parameters,
            request=request,
            scores=scores,
            relative_floor=self.relative_floor,
            started=started,
        )


def deterministic_result_digest(results: Sequence[CandidateResult]) -> str:
    payload = stable_json([result.deterministic_dict() for result in results]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
