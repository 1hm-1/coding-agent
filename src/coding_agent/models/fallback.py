from __future__ import annotations

from typing import Iterable

from coding_agent.domain import BackendError, ModelRequest, ModelResponse


class FallbackBackend:
    """A provider-independent ordered backend chain.

    It never retries by itself. Runtime owns retry budgets and asks this object for
    the next backend only after a classified, retryable infrastructure failure.
    """

    def __init__(self, backends: Iterable[object]):
        self.backends = tuple(backends)
        if not self.backends:
            raise ValueError("at least one backend is required")
        if any(not hasattr(backend, "complete") or not hasattr(backend, "name") for backend in self.backends):
            raise TypeError("fallback entries must implement ModelBackend")
        self._index = 0

    @property
    def name(self) -> str:
        return str(self.backends[self._index].name)

    def complete(self, request: ModelRequest) -> ModelResponse:
        return self.backends[self._index].complete(request)

    def fallback_for(self, error: BackendError) -> object | None:
        if not error.retryable or self._index + 1 >= len(self.backends):
            return None
        self._index += 1
        # Keep the chain as the active backend so a later retry can select the
        # next entry as well.  The adapter's name exposes the selected entry.
        return self

    def restore_state(self, retry_metadata: dict[str, object]) -> None:
        target = retry_metadata.get("backend")
        if target is None:
            return
        for index, backend in enumerate(self.backends):
            if str(backend.name) == str(target):
                self._index = index
                return


FallbackAdapter = FallbackBackend
