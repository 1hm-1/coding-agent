"""M1 product-layer identity records.

These value records deliberately contain no Runtime FSM state.  The durable
identity authority is the generated ``repository_id`` persisted by the M1
registry; descriptors are discovery evidence only.
"""

from __future__ import annotations

from dataclasses import dataclass


def _require_text(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} cannot be empty")


@dataclass(frozen=True)
class RepositoryIdentity:
    repository_id: str
    created_at: str

    def __post_init__(self) -> None:
        _require_text(self.repository_id, "repository_id")
        _require_text(self.created_at, "created_at")


@dataclass(frozen=True)
class RepositoryDescriptor:
    descriptor_id: str
    repository_id: str
    descriptor_kind: str
    descriptor_value: str

    def __post_init__(self) -> None:
        for field in ("descriptor_id", "repository_id", "descriptor_kind", "descriptor_value"):
            _require_text(str(getattr(self, field)), field)


@dataclass(frozen=True)
class ProjectScope:
    project_scope_id: str
    repository_id: str
    relative_path: str

    def __post_init__(self) -> None:
        _require_text(self.project_scope_id, "project_scope_id")
        _require_text(self.repository_id, "repository_id")
        if not self.relative_path or self.relative_path.startswith("/"):
            raise ValueError("relative_path must be repository-relative")


@dataclass(frozen=True)
class WorkspaceBinding:
    workspace_binding_id: str
    repository_id: str
    project_scope_id: str
    binding_kind: str
    locator: str

    def __post_init__(self) -> None:
        for field in ("workspace_binding_id", "repository_id", "project_scope_id", "binding_kind", "locator"):
            _require_text(str(getattr(self, field)), field)


@dataclass(frozen=True)
class Conversation:
    conversation_id: str
    repository_id: str
    project_scope_id: str
    default_workspace_binding_id: str
    provenance_kind: str

    def __post_init__(self) -> None:
        for field in (
            "conversation_id", "repository_id", "project_scope_id",
            "default_workspace_binding_id", "provenance_kind",
        ):
            _require_text(str(getattr(self, field)), field)


@dataclass(frozen=True)
class Turn:
    turn_id: str
    conversation_id: str
    ordinal: int
    provenance_kind: str

    def __post_init__(self) -> None:
        _require_text(self.turn_id, "turn_id")
        _require_text(self.conversation_id, "conversation_id")
        _require_text(self.provenance_kind, "provenance_kind")
        if self.ordinal < 1:
            raise ValueError("turn ordinal must be positive")


@dataclass(frozen=True)
class RuntimeExecution:
    runtime_execution_id: str
    legacy_session_id: str
    turn_id: str
    workspace_binding_id: str

    def __post_init__(self) -> None:
        for field in ("runtime_execution_id", "legacy_session_id", "turn_id", "workspace_binding_id"):
            _require_text(str(getattr(self, field)), field)


@dataclass(frozen=True)
class ConversationSemanticEvent:
    conversation_event_id: str
    conversation_id: str
    sequence: int
    event_type: str
    provenance_kind: str

    def __post_init__(self) -> None:
        for field in ("conversation_event_id", "conversation_id", "event_type", "provenance_kind"):
            _require_text(str(getattr(self, field)), field)
        if self.sequence < 1:
            raise ValueError("semantic event sequence must be positive")
