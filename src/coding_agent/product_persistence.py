"""Private M1 Product repository port and legacy compatibility adapter."""

from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

from coding_agent.product_domain import RepositoryDescriptor, RepositoryIdentity, RuntimeExecution


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
