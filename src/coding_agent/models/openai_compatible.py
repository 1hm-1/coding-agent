from __future__ import annotations

import json
from typing import Any, Mapping

from coding_agent.domain import BackendError, Message, ModelRequest, ModelResponse, ToolCall, Usage
from coding_agent.models.http import post_json, secret_from_environment
from coding_agent.models.errors import safe_provider_metadata


class OpenAICompatibleBackend:
    """Provider-neutral OpenAI chat-completions wire adapter."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key_env: str = "OPENAI_API_KEY",
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
        self.timeout = timeout
        self.transport = transport

    @property
    def name(self) -> str:
        return "openai-compatible"

    def complete(self, request: ModelRequest) -> ModelResponse:
        api_key = secret_from_environment(self.api_key_env)
        body = post_json(
            url=f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            payload=self._payload(request),
            timeout=self.timeout,
            transport=self.transport,
            provider=self.name,
        )
        return self._response(body)

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_message_to_openai(message) for message in request.messages],
            "max_tokens": request.max_output_tokens,
        }
        if request.tools:
            payload["tools"] = [_tool_to_openai(tool) for tool in request.tools]
        # The model is supplied by the adapter, but keeping request metadata out of the
        # provider payload prevents session internals from becoming provider-specific.
        return payload

    def _response(self, body: Mapping[str, Any]) -> ModelResponse:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise BackendError("OpenAI response has no choice", kind="protocol_error")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise BackendError("OpenAI response choice has no message", kind="protocol_error")
        content = message.get("content", "")
        if content is None:
            text = ""
        elif isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, Mapping) and part.get("type") == "text"
            )
        else:
            raise BackendError("OpenAI message content is malformed", kind="protocol_error")
        raw_calls = message.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise BackendError("OpenAI tool_calls is malformed", kind="protocol_error")
        calls = tuple(_tool_call_from_openai(raw_call) for raw_call in raw_calls)
        usage_raw = body.get("usage", {})
        if not isinstance(usage_raw, Mapping):
            raise BackendError("OpenAI usage is malformed", kind="protocol_error")
        finish_reason = str(choice.get("finish_reason") or "stop")
        return ModelResponse(
            text=text,
            tool_calls=calls,
            usage=Usage(
                input_tokens=int(usage_raw.get("prompt_tokens", usage_raw.get("input_tokens", 0))),
                output_tokens=int(
                    usage_raw.get("completion_tokens", usage_raw.get("output_tokens", 0))
                ),
            ),
            finish_reason=finish_reason,
            provider_metadata=safe_provider_metadata(
                self.name, model=self.model, finish_reason=finish_reason
            ),
        )


def _message_to_openai(message: Message) -> dict[str, Any]:
    result: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_call_id is not None:
        result["tool_call_id"] = message.tool_call_id
    raw_calls = message.metadata.get("tool_calls")
    if message.role == "assistant" and isinstance(raw_calls, list) and raw_calls:
        result["tool_calls"] = [
            {
                "id": str(call["id"]),
                "type": "function",
                "function": {
                    "name": str(call["name"]),
                    "arguments": json.dumps(
                        call.get("arguments", {}), ensure_ascii=False, sort_keys=True
                    ),
                },
            }
            for call in raw_calls
            if isinstance(call, Mapping) and "id" in call and "name" in call
        ]
    return result


def _tool_to_openai(schema: Mapping[str, Any]) -> dict[str, Any]:
    name = schema.get("name")
    description = schema.get("description", "")
    parameters = schema.get("input_schema")
    if not isinstance(name, str) or not name or not isinstance(parameters, Mapping):
        raise BackendError("tool schema cannot be converted to OpenAI format", kind="protocol_error")
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": str(description),
            "parameters": dict(parameters),
        },
    }


def _tool_call_from_openai(raw: Any) -> ToolCall:
    if not isinstance(raw, Mapping):
        raise BackendError("OpenAI tool call is malformed", kind="protocol_error")
    function = raw.get("function")
    if not isinstance(function, Mapping) or not raw.get("id") or not function.get("name"):
        raise BackendError("OpenAI tool call is missing id or function", kind="protocol_error")
    arguments = function.get("arguments", "{}")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError) as exc:
            raise BackendError("OpenAI tool arguments are invalid JSON", kind="protocol_error") from exc
    if not isinstance(arguments, Mapping):
        raise BackendError("OpenAI tool arguments must be an object", kind="protocol_error")
    return ToolCall(id=str(raw["id"]), name=str(function["name"]), arguments=dict(arguments))


OpenAICompatibleAdapter = OpenAICompatibleBackend
