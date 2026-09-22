"""Private M1 Product repository port and legacy compatibility adapter."""

from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

from coding_agent.domain import Message, RunPolicy, RuntimeSnapshot
from coding_agent.product_domain import (
    ProjectScope,
    RepositoryDescriptor,
    RepositoryIdentity,
    RuntimeExecution,
    TurnAdmission,
    WorkspaceBinding,
    WorkspaceWriterClaim,
)


@runtime_checkable
class ProductRepository(Protocol):
    """The M1 Product persistence boundary; SQLite remains its sole implementation."""

    def register_repository_identity(
        self, descriptors: Mapping[str, str], *, repository_id: str | None = None
    ) -> str:
        ...

    def get_repository_identity(self, repository_id: str) -> RepositoryIdentity | None:
        ...

    def list_repository_descriptors(self, repository_id: str) -> list[RepositoryDescriptor]:
        ...

    def ensure_legacy_product_mapping(self, session_id: str) -> RuntimeExecution:
        ...

    def backfill_legacy_product_mappings(self) -> list[RuntimeExecution]:
        ...

    def get_legacy_product_mapping(self, session_id: str) -> RuntimeExecution | None:
        ...

    def inspect_legacy_product_mapping(self, session_id: str) -> dict[str, object] | None:
        ...

    def list_conversation_semantic_events(self, session_id: str) -> list[dict[str, object]]:
        ...

    def record_legacy_product_mapping_failure(self, session_id: str, error: Exception) -> None:
        ...

    def get_legacy_product_mapping_failure(self, session_id: str) -> dict[str, object] | None:
        ...

    # M2 is deliberately a narrow persistence/use-case boundary.  It exposes
    # lifecycle publication and inspection, never an alternate Runtime FSM.
    def admit_turn(
        self,
        *,
        operation_id: str,
        payload_digest: str,
        initial_request: str,
        repository_id: str,
        project_scope_id: str,
        workspace_binding_id: str,
        snapshot: RuntimeSnapshot,
        policy: RunPolicy,
        conversation_id: str | None = None,
        expected_conversation_version: int | None = None,
    ) -> TurnAdmission:
        ...

    def append_product_input(
        self, *, operation_id: str, payload_digest: str, conversation_id: str,
        input_kind: str, payload: Mapping[str, object], expected_conversation_version: int,
        correlation_id: str | None = None,
    ) -> str:
        ...

    def request_ordinary_input(
        self, *, operation_id: str, payload_digest: str, conversation_id: str,
        request_id: str, payload: Mapping[str, object], expected_conversation_version: int,
    ) -> str:
        ...

    def consume_steering_inputs(
        self, *, conversation_id: str, legacy_session_id: str, expected_conversation_version: int,
    ) -> list[str]:
        ...

    def consume_current_steering_inputs(
        self, *, conversation_id: str, legacy_session_id: str,
    ) -> list[str]:
        """Consume the current ordered batch at an internal Runtime boundary."""
        ...

    def apply_cancel_at_safe_boundary(
        self, *, conversation_id: str, legacy_session_id: str, operation_id: str,
        expected_conversation_version: int,
    ) -> None:
        ...

    def rebind_conversation(
        self, *, operation_id: str, payload_digest: str, conversation_id: str,
        workspace_binding_id: str, expected_version: int,
        observation: Mapping[str, object],
    ) -> int:
        ...

    def get_turn_admission(self, operation_id: str) -> TurnAdmission | None:
        ...

    def inspect_conversation(self, conversation_id: str) -> dict[str, object] | None:
        ...

    def resume_execution(self, operation_id: str) -> TurnAdmission:
        ...

    def inspect_workspace_startup(self, workspace_binding_id: str) -> dict[str, object]:
        ...

    def list_messages(self, session_id: str) -> list[Message]:
        ...

    def register_project_scope(self, repository_id: str, relative_path: str = ".") -> ProjectScope:
        ...

    def register_workspace_binding(
        self, *, repository_id: str, project_scope_id: str, binding_kind: str,
        locator: str, observation: Mapping[str, object] | None = None,
    ) -> WorkspaceBinding:
        ...

    def finalize_admitted_turn(self, operation_id: str, *, repair_provenance: str = "m2_runtime_observation") -> None:
        ...

    def claim_workspace_writer(
        self, *, operation_id: str, payload_digest: str, workspace_binding_id: str,
        owner_id: str, expected_epoch: int, expected_observation_digest: str,
        lease_seconds: float, observation: Mapping[str, object], runtime_execution_id: str | None = None,
    ) -> WorkspaceWriterClaim:
        ...

    def renew_workspace_writer(
        self, *, operation_id: str, payload_digest: str, claim_id: str, owner_id: str,
        expected_epoch: int, expected_observation_digest: str, lease_seconds: float,
    ) -> WorkspaceWriterClaim:
        ...

    def release_workspace_writer(
        self, claim_id: str, *, operation_id: str, payload_digest: str, owner_id: str,
        expected_epoch: int, expected_observation_digest: str,
    ) -> None:
        ...

    def record_unknown_effect_recovery_barrier(
        self, *, operation_id: str, payload_digest: str, workspace_binding_id: str,
        uncertain_invocation_id: str, resolver_kind: str, evidence_digest: str,
    ) -> str:
        ...

    def resolve_unknown_effect_recovery_barrier(
        self, *, operation_id: str, payload_digest: str, barrier_id: str,
        workspace_binding_id: str, uncertain_invocation_id: str, resolver_kind: str,
        evidence_digest: str,
    ) -> None:
        ...


class LegacySessionProductCompatibilityAdapter:
    """Preserve legacy RunResult semantics while scheduling M1 mapping recovery."""

    def __init__(self, repository: ProductRepository):
        self.repository = repository

    def map_after_legacy_run(self, session_id: str) -> None:
        try:
            self.repository.ensure_legacy_product_mapping(session_id)
        except Exception as error:
            try:
                self.repository.record_legacy_product_mapping_failure(session_id, error)
            except Exception:
                # A legacy Runtime outcome remains authoritative if its durable
                # product-recovery audit is itself unavailable.
                pass
