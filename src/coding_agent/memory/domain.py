from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from coding_agent.domain import JsonObject


MEMORY_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class MemoryScope(str, Enum):
    SESSION = "session"
    REPOSITORY = "repository"
    USER = "user"


class MemoryKind(str, Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


class MemoryStatus(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    STALE = "stale"
    REJECTED = "rejected"
    DELETED = "deleted"


class MemoryValidationError(ValueError):
    """A memory record is malformed or violates a domain invariant."""


def canonical_content(content: str) -> str:
    return " ".join(content.split())


def content_hash(content: str) -> str:
    return hashlib.sha256(canonical_content(content).encode("utf-8")).hexdigest()


def _validate_timestamp(value: str, field_name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise MemoryValidationError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise MemoryValidationError(f"{field_name} must include a timezone")


def _validate_identifier(value: str, field_name: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise MemoryValidationError(f"{field_name} is invalid")


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    schema_version: int
    scope: MemoryScope
    scope_id: str
    kind: MemoryKind
    content: str
    source_run_id: str
    source_agent_id: str
    source_event_refs: tuple[str, ...]
    repository_revision: str | None
    confidence: float
    created_at: str
    expires_at: str | None
    status: MemoryStatus
    supersedes: str | None
    content_hash: str
    version: int = 0
    updated_at: str = ""

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != MEMORY_SCHEMA_VERSION
        ):
            raise MemoryValidationError("unsupported memory schema version")
        if not isinstance(self.scope, MemoryScope):
            raise MemoryValidationError("invalid memory scope")
        if not isinstance(self.kind, MemoryKind):
            raise MemoryValidationError("invalid memory kind")
        if not isinstance(self.status, MemoryStatus):
            raise MemoryValidationError("invalid memory status")
        _validate_identifier(self.memory_id, "memory_id")
        _validate_identifier(self.scope_id, "scope_id")
        _validate_identifier(self.source_run_id, "source_run_id")
        _validate_identifier(self.source_agent_id, "source_agent_id")
        if not self.source_event_refs:
            raise MemoryValidationError("source_event_refs cannot be empty")
        if len(set(self.source_event_refs)) != len(self.source_event_refs):
            raise MemoryValidationError("source_event_refs cannot contain duplicates")
        for ref in self.source_event_refs:
            _validate_identifier(ref, "source_event_ref")
        normalized = canonical_content(self.content)
        if self.status is MemoryStatus.DELETED:
            if self.content:
                raise MemoryValidationError("deleted memory tombstone cannot retain content")
        else:
            if not normalized:
                raise MemoryValidationError("memory content cannot be empty")
            if len(self.content.encode("utf-8")) > 16_384:
                raise MemoryValidationError("memory content exceeds 16384 bytes")
            if self.content_hash != content_hash(self.content):
                raise MemoryValidationError("memory content_hash does not match content")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise MemoryValidationError("memory confidence must be numeric")
        if not 0.0 <= self.confidence <= 1.0:
            raise MemoryValidationError("memory confidence must be between 0 and 1")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise MemoryValidationError("memory version cannot be negative")
        _validate_timestamp(self.created_at, "created_at")
        if self.updated_at:
            _validate_timestamp(self.updated_at, "updated_at")
        if self.expires_at is not None:
            _validate_timestamp(self.expires_at, "expires_at")
            if datetime.fromisoformat(self.expires_at) <= datetime.fromisoformat(self.created_at):
                raise MemoryValidationError("expires_at must be after created_at")
        if self.scope is MemoryScope.REPOSITORY and not self.repository_revision:
            raise MemoryValidationError("repository memory requires repository_revision")
        if self.scope is not MemoryScope.REPOSITORY and self.repository_revision is not None:
            raise MemoryValidationError("repository_revision is only valid for repository memory")
        if self.supersedes is not None:
            _validate_identifier(self.supersedes, "supersedes")
            if self.supersedes == self.memory_id:
                raise MemoryValidationError("memory cannot supersede itself")

    @classmethod
    def proposed(
        cls,
        *,
        memory_id: str,
        scope: MemoryScope,
        scope_id: str,
        kind: MemoryKind,
        content: str,
        source_run_id: str,
        source_agent_id: str,
        source_event_refs: Sequence[str],
        repository_revision: str | None,
        confidence: float,
        created_at: str,
        expires_at: str | None = None,
        supersedes: str | None = None,
    ) -> "MemoryRecord":
        return cls(
            memory_id=memory_id,
            schema_version=MEMORY_SCHEMA_VERSION,
            scope=scope,
            scope_id=scope_id,
            kind=kind,
            content=content,
            source_run_id=source_run_id,
            source_agent_id=source_agent_id,
            source_event_refs=tuple(source_event_refs),
            repository_revision=repository_revision,
            confidence=confidence,
            created_at=created_at,
            expires_at=expires_at,
            status=MemoryStatus.PROPOSED,
            supersedes=supersedes,
            content_hash=content_hash(content),
            updated_at=created_at,
        )

    def with_status(self, status: MemoryStatus, *, updated_at: str) -> "MemoryRecord":
        return replace(
            self,
            status=status,
            content="" if status is MemoryStatus.DELETED else self.content,
            version=self.version + 1,
            updated_at=updated_at,
        )

    def to_dict(self) -> JsonObject:
        return {
            "memory_id": self.memory_id,
            "schema_version": self.schema_version,
            "scope": self.scope.value,
            "scope_id": self.scope_id,
            "kind": self.kind.value,
            "content": self.content,
            "source_run_id": self.source_run_id,
            "source_agent_id": self.source_agent_id,
            "source_event_refs": list(self.source_event_refs),
            "repository_revision": self.repository_revision,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status.value,
            "supersedes": self.supersedes,
            "content_hash": self.content_hash,
            "version": self.version,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MemoryRecord":
        refs = raw.get("source_event_refs")
        if not isinstance(refs, (list, tuple)) or any(not isinstance(ref, str) for ref in refs):
            raise MemoryValidationError("source_event_refs must be an array of strings")
        if isinstance(raw.get("confidence"), bool):
            raise MemoryValidationError("memory confidence must be numeric")
        try:
            return cls(
                memory_id=str(raw["memory_id"]),
                schema_version=int(raw["schema_version"]),
                scope=MemoryScope(str(raw["scope"])),
                scope_id=str(raw["scope_id"]),
                kind=MemoryKind(str(raw["kind"])),
                content=str(raw["content"]),
                source_run_id=str(raw["source_run_id"]),
                source_agent_id=str(raw["source_agent_id"]),
                source_event_refs=tuple(refs),
                repository_revision=(
                    str(raw["repository_revision"])
                    if raw.get("repository_revision") is not None
                    else None
                ),
                confidence=float(raw["confidence"]),
                created_at=str(raw["created_at"]),
                expires_at=str(raw["expires_at"]) if raw.get("expires_at") is not None else None,
                status=MemoryStatus(str(raw["status"])),
                supersedes=str(raw["supersedes"]) if raw.get("supersedes") else None,
                content_hash=str(raw["content_hash"]),
                version=int(raw.get("version", 0)),
                updated_at=str(raw.get("updated_at", raw["created_at"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, MemoryValidationError):
                raise
            raise MemoryValidationError("invalid memory record") from exc


@dataclass(frozen=True)
class MemoryAuditEvent:
    event_id: str
    memory_id: str
    event_type: str
    actor_id: str
    from_status: MemoryStatus | None
    to_status: MemoryStatus
    payload: JsonObject
    created_at: str

    def to_dict(self) -> JsonObject:
        return {
            "event_id": self.event_id,
            "memory_id": self.memory_id,
            "event_type": self.event_type,
            "actor_id": self.actor_id,
            "from_status": self.from_status.value if self.from_status else None,
            "to_status": self.to_status.value,
            "payload": self.payload,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class MemoryQuery:
    text: str
    session_id: str | None = None
    repository_id: str | None = None
    user_id: str | None = None
    repository_revision: str | None = None
    top_k: int = 5
    token_budget: int = 512
    kinds: tuple[MemoryKind, ...] = (MemoryKind.EPISODIC, MemoryKind.SEMANTIC)

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise MemoryValidationError("memory query text cannot be empty")
        if self.top_k <= 0 or self.top_k > 100:
            raise MemoryValidationError("memory query top_k must be between 1 and 100")
        if self.token_budget <= 0:
            raise MemoryValidationError("memory query token_budget must be positive")
        if not self.kinds:
            raise MemoryValidationError("memory query kinds cannot be empty")


@dataclass(frozen=True)
class MemoryHit:
    record: MemoryRecord
    score: float
    token_cost: int
    matched_terms: tuple[str, ...]

    def manifest(self) -> JsonObject:
        return {
            "memory_id": self.record.memory_id,
            "schema_version": self.record.schema_version,
            "record_version": self.record.version,
            "scope": self.record.scope.value,
            "kind": self.record.kind.value,
            "score": self.score,
            "token_cost": self.token_cost,
            "source_run_id": self.record.source_run_id,
            "source_event_refs": list(self.record.source_event_refs),
            "repository_revision": self.record.repository_revision,
        }


@dataclass(frozen=True)
class MemorySelection:
    retrieval_id: str
    query_hash: str
    hits: tuple[MemoryHit, ...]
    total_token_cost: int
    duration_ms: float

    def manifest(self) -> JsonObject:
        return {
            "retrieval_id": self.retrieval_id,
            "query_hash": self.query_hash,
            "records": [hit.manifest() for hit in self.hits],
            "total_token_cost": self.total_token_cost,
            "duration_ms": self.duration_ms,
        }


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
