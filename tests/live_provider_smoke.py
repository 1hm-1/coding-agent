from __future__ import annotations

import os
import unittest
from urllib.parse import urlsplit

from coding_agent.domain import Message, ModelRequest
from coding_agent.models.anthropic import AnthropicBackend
from coding_agent.models.base import ModelBackend
from coding_agent.models.openai_compatible import OpenAICompatibleBackend


def _configured() -> bool:
    provider = os.environ.get("CODING_AGENT_LIVE_PROVIDER", "")
    model = os.environ.get("CODING_AGENT_LIVE_MODEL", "")
    if provider not in {"openai-compatible", "anthropic"} or not model:
        return False
    key_env = "OPENAI_API_KEY" if provider == "openai-compatible" else "ANTHROPIC_API_KEY"
    return bool(os.environ.get(key_env))


def _backend() -> ModelBackend:
    provider = os.environ["CODING_AGENT_LIVE_PROVIDER"]
    model = os.environ["CODING_AGENT_LIVE_MODEL"]
    base_url = os.environ.get("CODING_AGENT_LIVE_BASE_URL")
    timeout = float(os.environ.get("CODING_AGENT_LIVE_TIMEOUT", "30"))
    if provider == "openai-compatible":
        thinking = os.environ.get("CODING_AGENT_LIVE_THINKING")
        if thinking is None and urlsplit(base_url or "").hostname == "api.deepseek.com":
            thinking = "disabled"
        return OpenAICompatibleBackend(
            model=model,
            base_url=base_url or "https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            timeout=timeout,
            thinking=thinking,
        )
    return AnthropicBackend(
        model=model,
        base_url=base_url or "https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        timeout=timeout,
    )


@unittest.skipUnless(
    _configured(),
    "set CODING_AGENT_LIVE_PROVIDER, CODING_AGENT_LIVE_MODEL and the provider API key",
)
class LiveProviderSmokeTest(unittest.TestCase):
    """Explicit, credential-gated adapter smoke; never part of default CI."""

    def test_provider_returns_a_text_response(self) -> None:
        response = _backend().complete(
            ModelRequest(
                request_id="live-provider-smoke",
                messages=(Message(role="user", content="Reply with the single word OK."),),
                tools=(),
                max_output_tokens=256,
                metadata={},
            )
        )
        self.assertTrue(response.text.strip())
        self.assertFalse(response.tool_calls)
        self.assertGreaterEqual(response.usage.input_tokens, 0)
        self.assertGreaterEqual(response.usage.output_tokens, 0)


if __name__ == "__main__":
    unittest.main()
