from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_agent.domain import BackendError, Message, ModelRequest, ToolCall
from coding_agent.application import AgentApplication
from coding_agent.models.anthropic import AnthropicBackend
from coding_agent.models.fallback import FallbackBackend
from coding_agent.models.openai_compatible import OpenAICompatibleBackend


class FakeResponse:
    def __init__(self, body, status_code: int = 200, headers=None):
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.body


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def request() -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        messages=(
            Message(role="system", content="保持 Unicode：你好"),
            Message(role="user", content="Read the file."),
        ),
        tools=(
            {
                "name": "read_file",
                "description": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        ),
        max_output_tokens=123,
    )


class ModelAdapterTest(unittest.TestCase):
    def test_openai_compatible_text_and_tool_contract(self) -> None:
        transport = FakeTransport(
            FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "读取完成",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path":"value.txt"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 5},
                }
            )
        )
        with patch.dict(os.environ, {"TEST_OPENAI_KEY": "sk-secret"}, clear=False):
            backend = OpenAICompatibleBackend(
                model="test-model",
                base_url="https://provider.test/v1",
                api_key_env="TEST_OPENAI_KEY",
                transport=transport,
                thinking="disabled",
            )
            response = backend.complete(request())
        self.assertEqual(response.text, "读取完成")
        self.assertEqual(response.tool_calls, (ToolCall("call-1", "read_file", {"path": "value.txt"}),))
        self.assertEqual(response.usage.input_tokens, 7)
        self.assertEqual(transport.calls[0][0], "https://provider.test/v1/chat/completions")
        payload = transport.calls[0][1]["json"]
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["max_tokens"], 123)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertNotIn("sk-secret", str(response.provider_metadata))

    def test_anthropic_separates_system_and_round_trips_tool_blocks(self) -> None:
        transport = FakeTransport(
            FakeResponse(
                {
                    "content": [
                        {"type": "text", "text": "好的"},
                        {
                            "type": "tool_use",
                            "id": "call-2",
                            "name": "read_file",
                            "input": {"path": "值.txt"},
                        },
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 11, "output_tokens": 4},
                }
            )
        )
        with patch.dict(os.environ, {"TEST_ANTHROPIC_KEY": "secret"}, clear=False):
            backend = AnthropicBackend(
                model="claude-test",
                base_url="https://provider.test/v1",
                api_key_env="TEST_ANTHROPIC_KEY",
                transport=transport,
            )
            response = backend.complete(request())
        self.assertEqual(response.text, "好的")
        self.assertEqual(response.tool_calls[0].arguments, {"path": "值.txt"})
        payload = transport.calls[0][1]["json"]
        self.assertEqual(payload["model"], "claude-test")
        self.assertEqual(payload["system"], "保持 Unicode：你好")
        self.assertEqual(payload["messages"][0]["content"], "Read the file.")
        self.assertEqual(payload["tools"][0]["input_schema"]["type"], "object")

    def test_provider_http_errors_are_classified_without_secrets(self) -> None:
        cases = (
            (429, "rate_limit", True),
            (503, "provider_unavailable", True),
            (401, "authentication", False),
            (422, "invalid_request", False),
        )
        for status, kind, retryable in cases:
            with self.subTest(status=status):
                transport = FakeTransport(
                    FakeResponse(
                        {"error": {"message": "authorization Bearer sk-secret", "code": "bad"}},
                        status_code=status,
                        headers={"retry-after": "2"},
                    )
                )
                with patch.dict(os.environ, {"TEST_OPENAI_KEY": "sk-secret"}, clear=False):
                    backend = OpenAICompatibleBackend(
                        model="m",
                        api_key_env="TEST_OPENAI_KEY",
                        transport=transport,
                    )
                    with self.assertRaises(BackendError) as raised:
                        backend.complete(request())
                self.assertEqual(raised.exception.kind, kind)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertNotIn("sk-secret", str(raised.exception))

    def test_malformed_provider_payload_is_protocol_error(self) -> None:
        transport = FakeTransport(FakeResponse({"choices": []}))
        with patch.dict(os.environ, {"TEST_OPENAI_KEY": "key"}, clear=False):
            backend = OpenAICompatibleBackend(
                model="m", api_key_env="TEST_OPENAI_KEY", transport=transport
            )
            with self.assertRaises(BackendError) as raised:
                backend.complete(request())
        self.assertEqual(raised.exception.kind, "protocol_error")

    def test_provider_usage_requires_non_negative_integers(self) -> None:
        cases = (
            (
                OpenAICompatibleBackend,
                {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "done"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": None, "completion_tokens": 1},
                },
                "TEST_OPENAI_KEY",
            ),
            (
                AnthropicBackend,
                {
                    "content": [{"type": "text", "text": "done"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": -1, "output_tokens": 1},
                },
                "TEST_ANTHROPIC_KEY",
            ),
        )
        for backend_type, body, key_name in cases:
            with self.subTest(backend=backend_type.__name__):
                with patch.dict(os.environ, {key_name: "key"}, clear=False):
                    backend = backend_type(
                        model="m",
                        api_key_env=key_name,
                        transport=FakeTransport(FakeResponse(body)),
                    )
                    with self.assertRaises(BackendError) as raised:
                        backend.complete(request())
                self.assertEqual(raised.exception.kind, "protocol_error")

    def test_malformed_usage_closes_runtime_model_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            transport = FakeTransport(
                FakeResponse(
                    {
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": "done"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": None, "completion_tokens": 1},
                    }
                )
            )
            with patch.dict(os.environ, {"TEST_OPENAI_KEY": "key"}, clear=False):
                backend = OpenAICompatibleBackend(
                    model="m",
                    api_key_env="TEST_OPENAI_KEY",
                    transport=transport,
                )
                application = AgentApplication(root / "agent-home")
                result = application.run_task(source=source, task="answer", backend=backend)

            self.assertEqual(result.failure["kind"], "protocol_error")
            rows = application.journal.list_model_calls(result.session_id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "failed")
            self.assertEqual(rows[0]["error"]["kind"], "protocol_error")

    def test_unexpected_backend_exception_closes_model_journal(self) -> None:
        class BrokenBackend:
            name = "broken"

            def complete(self, model_request):
                raise TypeError("unexpected adapter bug")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            application = AgentApplication(root / "agent-home")
            result = application.run_task(
                source=source,
                task="answer",
                backend=BrokenBackend(),
            )

            self.assertEqual(result.failure["kind"], "protocol_error")
            rows = application.journal.list_model_calls(result.session_id)
            self.assertEqual(rows[0]["status"], "failed")
            self.assertEqual(rows[0]["error"]["kind"], "protocol_error")

    def test_fallback_does_not_handle_non_retryable_errors(self) -> None:
        class Primary:
            name = "primary"

            def complete(self, request):
                raise BackendError("bad request", kind="invalid_request")

        class Secondary:
            name = "secondary"

            def complete(self, request):
                raise AssertionError("must not be called")

        fallback = FallbackBackend([Primary(), Secondary()])
        with self.assertRaises(BackendError):
            fallback.complete(request())
        self.assertEqual(fallback.name, "primary")

    def test_secret_is_not_written_to_sqlite_trace_or_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            transport = FakeTransport(
                FakeResponse(
                    {
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": "完成"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }
                )
            )
            with patch.dict(os.environ, {"TRACE_OPENAI_KEY": "sk-trace-secret"}, clear=False):
                backend = OpenAICompatibleBackend(
                    model="m",
                    api_key_env="TRACE_OPENAI_KEY",
                    transport=transport,
                )
                application = AgentApplication(root / "agent-home")
                result = application.run_task(source=source, task="回答", backend=backend)
            self.assertEqual(result.state.value, "completed")
            database = (root / "agent-home" / "state.db").read_bytes()
            self.assertNotIn(b"sk-trace-secret", database)
            trace = Path(result.trace_path).read_text(encoding="utf-8")
            self.assertNotIn("sk-trace-secret", trace)


if __name__ == "__main__":
    unittest.main()
