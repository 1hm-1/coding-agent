from __future__ import annotations

from typing import Protocol

from coding_agent.domain import ModelRequest, ModelResponse


class ModelBackend(Protocol):
    """Provider-neutral model boundary used by the runtime."""

    @property
    def name(self) -> str:
        ...

    def complete(self, request: ModelRequest) -> ModelResponse:
        ...

