from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from coding_agent.domain import (
    BackendError,
    ModelRequest,
    ModelResponse,
    ToolCall,
    Usage,
)


class ScriptedBackend:
    """Deterministic backend for tests, demos, and fault-injection evals."""

    def __init__(
        self,
        responses: Iterable[Mapping[str, Any]],
        *,
        start_index: int = 0,
    ):
        self._responses: list[Any] = list(responses)
        if start_index < 0 or start_index > len(self._responses):
            raise ValueError("script start_index is outside the response list")
        self._index = start_index
        self.requests: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return "scripted"

    @classmethod
    def from_file(cls, path: str | Path, *, start_index: int = 0) -> "ScriptedBackend":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("scripted backend file must contain a JSON array")
        return cls(raw, start_index=start_index)

    def restore(self, completed_model_calls: int) -> None:
        """Seek to the response ordinal already committed by a prior process."""

        if completed_model_calls < 0 or completed_model_calls > len(self._responses):
            raise ValueError("completed model call count is outside the script")
        self._index = completed_model_calls

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._index >= len(self._responses):
            raise BackendError(
                "scripted backend exhausted",
                kind="script_exhausted",
            )

        item = self._responses[self._index]
        self._index += 1
        try:
            if not isinstance(item, Mapping):
                raise TypeError("response must be an object")
            if "error" in item:
                if not isinstance(item["error"], Mapping):
                    raise TypeError("error must be an object")
                error = dict(item["error"])
                raise BackendError(
                    str(error.get("message", "scripted backend error")),
                    kind=str(error.get("kind", "scripted_error")),
                )

            raw_calls = item.get("tool_calls", [])
            if not isinstance(raw_calls, list):
                raise TypeError("tool_calls must be an array")
            calls = tuple(
                ToolCall(
                    id=str(raw.get("id", f"script-call-{self._index}-{position}")),
                    name=str(raw["name"]),
                    arguments=dict(raw.get("arguments", {})),
                )
                for position, raw in enumerate(raw_calls, start=1)
            )
            raw_usage = dict(item.get("usage", {}))
            return ModelResponse(
                text=str(item.get("final", item.get("text", ""))),
                tool_calls=calls,
                usage=Usage(
                    input_tokens=int(raw_usage.get("input_tokens", 0)),
                    output_tokens=int(raw_usage.get("output_tokens", 0)),
                ),
                finish_reason=str(
                    item.get("finish_reason", "tool_calls" if calls else "stop")
                ),
                provider_metadata={"script_index": self._index - 1},
            )
        except BackendError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise BackendError(
                f"invalid scripted response at index {self._index - 1}: {exc}",
                kind="invalid_script_response",
            ) from exc
