from __future__ import annotations

from typing import Any, Mapping

from coding_agent.domain import BackendError, Message, ModelRequest, ModelResponse, ToolCall, Usage
from coding_agent.models.errors import safe_provider_metadata
from coding_agent.models.http import post_json, secret_from_environment


class AnthropicBackend:
    """Anthropic Messages API adapter kept behind the provider boundary."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "https://api.anthropic.com/v1",
        api_key_env: str = "ANTHROPIC_API_KEY",
        anthropic_version: str = "2023-06-01",
        timeout: float = 60.0,
        transport: Any | None = None,
    ):
        if not model.strip():
            raise ValueError("model cannot be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.anthropic_version = anthropic_version
        self.timeout = timeout
        self.transport = transport

    @property
    def name(self) -> str:
        return "anthropic"

    def complete(self, request: ModelRequest) -> ModelResponse:
        api_key = secret_from_environment(self.api_key_env)
        body = post_json(
            url=f"{self.base_url}/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": self.anthropic_version,
                "Content-Type": "application/json",
            },
            payload=self._payload(request),
            timeout=self.timeout,
            transport=self.transport,
            provider=self.name,
        )
        return self._response(body)

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        system: list[str] = []
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "system":
                system.append(message.content)
            else:
                messages.append(_message_to_anthropic(message))
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
        }
        if system:
            payload["system"] = "\n\n".join(system)
        if request.tools:
            payload["tools"] = [_tool_to_anthropic(tool) for tool in request.tools]
        return payload

    def _response(self, body: Mapping[str, Any]) -> ModelResponse:
        content = body.get("content")
        if not isinstance(content, list):
            raise BackendError("Anthropic response content is malformed", kind="protocol_error")
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in content:
            if not isinstance(block, Mapping):
                raise BackendError("Anthropic content block is malformed", kind="protocol_error")
            block_type = block.get("type")
            if block_type == "text":
                if not isinstance(block.get("text", ""), str):
                    raise BackendError("Anthropic text block is malformed", kind="protocol_error")
                text_parts.append(str(block.get("text", "")))
            elif block_type == "tool_use":
                raw_input = block.get("input", {})
                if not isinstance(raw_input, Mapping) or not block.get("id") or not block.get("name"):
                    raise BackendError("Anthropic tool_use block is malformed", kind="protocol_error")
                calls.append(
                    ToolCall(
                        id=str(block["id"]),
                        name=str(block["name"]),
                        arguments=dict(raw_input),
                    )
                )
            else:
                raise BackendError(
                    f"unsupported Anthropic content block: {block_type!r}",
                    kind="protocol_error",
                )
        usage = body.get("usage", {})
        if not isinstance(usage, Mapping):
            raise BackendError("Anthropic usage is malformed", kind="protocol_error")
        stop_reason = str(body.get("stop_reason") or "end_turn")
        return ModelResponse(
            text="".join(text_parts),
            tool_calls=tuple(calls),
            usage=Usage(
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
            ),
            finish_reason=stop_reason,
            provider_metadata=safe_provider_metadata(
                self.name, model=self.model, finish_reason=stop_reason
            ),
        )


def _message_to_anthropic(message: Message) -> dict[str, Any]:
    if message.role == "tool":
        if message.tool_call_id is None:
            raise BackendError("tool message has no tool_call_id", kind="protocol_error")
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                }
            ],
        }
    if message.role == "assistant":
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        raw_calls = message.metadata.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise BackendError("assistant tool_calls metadata is malformed", kind="protocol_error")
        for call in raw_calls:
            if not isinstance(call, Mapping) or not call.get("id") or not call.get("name"):
                raise BackendError("assistant tool call metadata is malformed", kind="protocol_error")
            arguments = call.get("arguments", {})
            if not isinstance(arguments, Mapping):
                raise BackendError("assistant tool arguments are malformed", kind="protocol_error")
            blocks.append(
                {
                    "type": "tool_use",
                    "id": str(call["id"]),
                    "name": str(call["name"]),
                    "input": dict(arguments),
                }
            )
        return {"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]}
    if message.role not in {"user"}:
        raise BackendError(f"unsupported Anthropic message role: {message.role}", kind="protocol_error")
    return {"role": "user", "content": message.content}


def _tool_to_anthropic(schema: Mapping[str, Any]) -> dict[str, Any]:
    name = schema.get("name")
    input_schema = schema.get("input_schema")
    if not isinstance(name, str) or not name or not isinstance(input_schema, Mapping):
        raise BackendError("tool schema cannot be converted to Anthropic format", kind="protocol_error")
    return {
        "name": name,
        "description": str(schema.get("description", "")),
        "input_schema": dict(input_schema),
    }


AnthropicAdapter = AnthropicBackend
