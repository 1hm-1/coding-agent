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
        if self.placeholder_kind not in {
            "m2_placeholder_no_discovery", "m3_resolved_instruction_manifest",
        }:
            raise ValueError("unsupported instruction manifest representation")


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


# M3 immutable context records. They model Product evidence only: RuntimeState
# and provider wire representations deliberately remain outside this module.
@dataclass(frozen=True)
class InstructionSnapshot:
    snapshot_id: str
    content_digest: str
    content: str

    def __post_init__(self) -> None:
        for field in ("snapshot_id", "content_digest"):
            _require_text(str(getattr(self, field)), field)


@dataclass(frozen=True)
class FrozenModelRequest:
    request_id: str
    request_digest: str
    context_manifest_id: str
    normalized_payload: Mapping[str, object]

    def __post_init__(self) -> None:
        for field in ("request_id", "request_digest", "context_manifest_id"):
            _require_text(str(getattr(self, field)), field)


@dataclass(frozen=True)
class ContextManifest:
    context_manifest_id: str
    request_id: str
    request_digest: str
    manifest_digest: str
    policy_version: str

    def __post_init__(self) -> None:
        for field in ("context_manifest_id", "request_id", "request_digest", "manifest_digest", "policy_version"):
            _require_text(str(getattr(self, field)), field)


@dataclass(frozen=True)
class ProviderAttempt:
    attempt_id: str
    request_id: str
    ordinal: int
    status: str

    def __post_init__(self) -> None:
        for field in ("attempt_id", "request_id", "status"):
            _require_text(str(getattr(self, field)), field)
        if self.ordinal < 1:
            raise ValueError("attempt ordinal must be positive")


@dataclass(frozen=True)
class InstructionSourceRecord:
    source_id: str
    locator: str
    normalized_path: str
    scope_kind: str
    authority_rank: int
    specificity: int
    revision_digest: str
    disposition: str
    reason: str | None = None
    snapshot: InstructionSnapshot | None = None

    def __post_init__(self) -> None:
        for field in ("source_id", "locator", "normalized_path", "scope_kind", "revision_digest", "disposition"):
            _require_text(str(getattr(self, field)), field)
        if self.authority_rank < 0 or self.specificity < 0:
            raise ValueError("instruction rank and specificity must be non-negative")
        if self.disposition == "effective" and self.snapshot is None:
            raise ValueError("effective instruction source requires an immutable snapshot")


@dataclass(frozen=True)
class InstructionManifestEntry:
    entry_id: str
    source: InstructionSourceRecord
    sequence: int
    trust_disposition: str
    disposition: str
    reason: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.entry_id, "entry_id")
        _require_text(self.trust_disposition, "trust_disposition")
        _require_text(self.disposition, "disposition")
        if self.sequence < 1:
            raise ValueError("manifest entry sequence must be positive")


@dataclass(frozen=True)
class InstructionOverrideEdge:
    edge_id: str
    winner_entry_id: str
    loser_entry_id: str
    conflict_key: str
    resolution_kind: str

    def __post_init__(self) -> None:
        for field in ("edge_id", "winner_entry_id", "loser_entry_id", "conflict_key", "resolution_kind"):
            _require_text(str(getattr(self, field)), field)
        if self.winner_entry_id == self.loser_entry_id:
            raise ValueError("instruction override edge cannot be self-referential")


@dataclass(frozen=True)
class ContextSelection:
    source_class: str
    source_id: str
    disposition: str
    reason: str
    token_count: int
    source_digest: str

    def __post_init__(self) -> None:
        for field in ("source_class", "source_id", "disposition", "reason", "source_digest"):
            _require_text(str(getattr(self, field)), field)
        if self.token_count < 0:
            raise ValueError("selection token count must be non-negative")


@dataclass(frozen=True)
class FrozenContextPackage:
    messages: tuple[Mapping[str, object], ...]
    selections: tuple[ContextSelection, ...]
    policy_version: str
    token_counter_version: str
    required_tokens: int
    optional_tokens: int
    total_budget: int
    memory_status: str = "absent"

    def __post_init__(self) -> None:
        if self.memory_status != "absent":
            raise ValueError("M3 core Context must keep Memory absent")
        if min(self.required_tokens, self.optional_tokens, self.total_budget) < 0:
            raise ValueError("context token counts must be non-negative")
        # Section counts are allocation estimates. The complete normalized
        # request is checked with the provider/request counter before freeze.


@dataclass(frozen=True)
class ProviderAttemptOutcome:
    outcome_id: str
    attempt_id: str
    outcome_kind: str
    usage_classification: str
    coverage_status: str
    payload: Mapping[str, object] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        for field in ("outcome_id", "attempt_id", "outcome_kind", "usage_classification", "coverage_status"):
            _require_text(str(getattr(self, field)), field)


@dataclass(frozen=True)
class SummaryClaim:
    claim_id: str
    claim_kind: str
    text: str
    source_start_sequence: int
    source_end_sequence: int
    source_artifact_id: str | None = None
    source_revision: str | None = None
    status: str = "valid"

    def __post_init__(self) -> None:
        for field in ("claim_id", "claim_kind", "text", "status"):
            _require_text(str(getattr(self, field)), field)
        if self.source_start_sequence < 1 or self.source_end_sequence < self.source_start_sequence:
            raise ValueError("summary claim source range is invalid")


@dataclass(frozen=True)
class SummaryArtifact:
    summary_artifact_id: str
    conversation_id: str
    source_start_sequence: int
    source_end_sequence: int
    source_digest: str
    content: str
    content_digest: str
    policy_version: str
    generator_version: str
    claims: tuple[SummaryClaim, ...] = ()
    status: str = "valid"

    def __post_init__(self) -> None:
        for field in (
            "summary_artifact_id", "conversation_id", "source_digest", "content_digest",
            "policy_version", "generator_version", "status",
        ):
            _require_text(str(getattr(self, field)), field)
        if self.source_start_sequence < 1 or self.source_end_sequence < self.source_start_sequence:
            raise ValueError("summary source range is invalid")
        for claim in self.claims:
            if claim.source_start_sequence < self.source_start_sequence or claim.source_end_sequence > self.source_end_sequence:
                raise ValueError("summary claim escapes artifact source range")


@dataclass(frozen=True)
class ToolResultArtifact:
    artifact_id: str
    tool_call_id: str
    channel: str
    mime_type: str
    encoding: str
    content_digest: str
    captured_size: int
    range_start: int
    range_end: int
    capture_limit: int
    capture_completeness: str
    content: bytes | None = None

    def __post_init__(self) -> None:
        for field in (
            "artifact_id", "tool_call_id", "channel", "mime_type", "encoding",
            "content_digest", "capture_completeness",
        ):
            _require_text(str(getattr(self, field)), field)
        if min(self.captured_size, self.range_start, self.range_end, self.capture_limit) < 0:
            raise ValueError("artifact ranges must be non-negative")
        if self.range_end < self.range_start:
            raise ValueError("artifact range is invalid")


@dataclass(frozen=True)
class NormalizedObservation:
    observation_id: str
    artifact_id: str
    status_kind: str
    semantic: Mapping[str, object]
    excerpt: str | None
    prompt_truncated: bool
    projection_completeness: str

    def __post_init__(self) -> None:
        for field in ("observation_id", "artifact_id", "status_kind", "projection_completeness"):
            _require_text(str(getattr(self, field)), field)


@dataclass(frozen=True)
class FileContextItem:
    file_context_item_id: str
    workspace_binding_id: str
    normalized_path: str
    file_type: str
    content_hash: str
    revision: str
    byte_start: int
    byte_end: int
    range_digest: str
    origin_kind: str
    origin_id: str
    status: str = "current"
    content: bytes | None = None
    encoding: str = "binary"
    capture_completeness: str = "unknown"

    def __post_init__(self) -> None:
        for field in (
            "file_context_item_id", "workspace_binding_id", "normalized_path", "file_type",
            "content_hash", "revision", "range_digest", "origin_kind", "origin_id", "status",
        ):
            _require_text(str(getattr(self, field)), field)
        if self.byte_start < 0 or self.byte_end < self.byte_start:
            raise ValueError("file context byte range is invalid")
        if self.content is not None and len(self.content) != self.byte_end - self.byte_start:
            raise ValueError("file context content does not match the selected range")
