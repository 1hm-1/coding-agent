"""M1 product-layer identity records.

These value records deliberately contain no Runtime FSM state.  The durable
identity authority is the generated ``repository_id`` persisted by the M1
registry; descriptors are discovery evidence only.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Mapping


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


# M2 lifecycle records are Product projections and commands.  In particular,
# they intentionally do not encode RuntimeState: ``sessions`` remains the
# Runtime FSM authority throughout the compatibility window.
@dataclass(frozen=True)
class ProductInput:
    input_id: str
    operation_id: str
    payload_digest: str
    conversation_id: str
    sequence: int
    input_kind: str
    payload: Mapping[str, object]
    turn_id: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "input_id", "operation_id", "payload_digest", "conversation_id", "input_kind",
        ):
            _require_text(str(getattr(self, field)), field)
        if self.sequence < 1:
            raise ValueError("input sequence must be positive")
        if self.input_kind not in {"initial_request", "ordinary_input_request", "steering", "reply", "cancel"}:
            raise ValueError("unsupported M2 input kind")


@dataclass(frozen=True)
class TurnStartCodeCheckpoint:
    checkpoint_id: str
    observation_digest: str
    coverage_state: str
    excluded_state: str
    observation_frontier: str = "pre_admission_read_only"
    baseline_references: Mapping[str, object] = dataclass_field(default_factory=dict)
    exclusions: Mapping[str, object] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        for field in ("checkpoint_id", "observation_digest", "coverage_state", "excluded_state"):
            _require_text(str(getattr(self, field)), field)
        if self.coverage_state != "m2_observation_only":
            raise ValueError("M2 checkpoint must remain observation-only")
        if not self.excluded_state or "m4" not in self.excluded_state:
            raise ValueError("M2 checkpoint must explicitly exclude M4 mutation coverage")


@dataclass(frozen=True)
class InstructionManifest:
    instruction_manifest_id: str
    placeholder_kind: str

    def __post_init__(self) -> None:
        _require_text(self.instruction_manifest_id, "instruction_manifest_id")
        if self.placeholder_kind != "m2_placeholder_no_discovery":
            raise ValueError("M2 cannot claim instruction discovery")


@dataclass(frozen=True)
class PolicyEpoch:
    policy_epoch_id: str
    mode: str

    def __post_init__(self) -> None:
        _require_text(self.policy_epoch_id, "policy_epoch_id")
        if self.mode != "m2_read_only_direct_tree":
            raise ValueError("M2 policy epoch must keep direct trees read-only")


@dataclass(frozen=True)
class TurnAdmission:
    operation_id: str
    payload_digest: str
    conversation_id: str
    turn_id: str
    runtime_execution_id: str
    legacy_session_id: str
    checkpoint: TurnStartCodeCheckpoint
    instruction_manifest: InstructionManifest
    policy_epoch: PolicyEpoch
    idempotent: bool = False

    def __post_init__(self) -> None:
        for field in (
            "operation_id", "payload_digest", "conversation_id", "turn_id",
            "runtime_execution_id", "legacy_session_id",
        ):
            _require_text(str(getattr(self, field)), field)


@dataclass(frozen=True)
class WorkspaceWriterClaim:
    claim_id: str
    workspace_binding_id: str
    owner_id: str
    claim_epoch: int
    expires_at: str

    def __post_init__(self) -> None:
        for field in ("claim_id", "workspace_binding_id", "owner_id", "expires_at"):
            _require_text(str(getattr(self, field)), field)
        if self.claim_epoch < 1:
            raise ValueError("claim epoch must be positive")
