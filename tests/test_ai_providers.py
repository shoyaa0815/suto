import json

import pytest

from ai import config
from ai.providers.base import ProviderTransientError
from ai.providers.factory import build_provider
from ai.providers.ollama import OllamaProvider
from ai.providers.openai_compatible import OpenAICompatibleProvider


class FakeResponse:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status
        self.raise_called = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self.data

    def raise_for_status(self):
        self.raise_called = True


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


@pytest.mark.asyncio
async def test_ollama_provider_uses_native_chat_payload():
    response_data = {
        "message": {"role": "assistant", "content": "done"},
        "prompt_eval_count": 7,
        "eval_count": 3,
    }
    session = FakeSession(FakeResponse(response_data))
    provider = OllamaProvider("http://localhost:11434", "local-model", 0.4)

    result = await provider.chat(
        session,
        [{"role": "user", "content": "hello"}],
        [{"type": "function", "function": {"name": "clock"}}],
        think=True,
        max_output_tokens=50,
    )

    url, request = session.calls[0]
    assert url == "http://localhost:11434/api/chat"
    assert request["json"]["model"] == "local-model"
    assert request["json"]["think"] is True
    assert request["json"]["options"] == {
        "temperature": 0.4,
        "num_predict": 50,
    }
    assert request["json"]["tools"][0]["function"]["name"] == "clock"
    assert result == response_data


@pytest.mark.asyncio
async def test_openai_compatible_provider_normalizes_tool_calls_and_usage():
    response = FakeResponse(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "clock",
                                    "arguments": '{"timezone":"Asia/Bangkok"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 5},
        }
    )
    session = FakeSession(response)
    provider = OpenAICompatibleProvider(
        "https://example.test/v1/", "cloud-model", "secret", 0.2
    )
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "earlier_call",
                    "type": "function",
                    "function": {"name": "clock", "arguments": {"city": "BKK"}},
                }
            ],
        }
    ]

    result = await provider.chat(session, messages, [], max_output_tokens=80)

    url, request = session.calls[0]
    assert url == "https://example.test/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer secret"
    assert request["json"]["max_tokens"] == 80
    sent_arguments = request["json"]["messages"][0]["tool_calls"][0][
        "function"
    ]["arguments"]
    assert json.loads(sent_arguments) == {"city": "BKK"}
    assert result["message"]["tool_calls"][0]["id"] == "call_123"
    assert result["message"]["tool_calls"][0]["function"]["arguments"] == {
        "timezone": "Asia/Bangkok"
    }
    assert result["prompt_eval_count"] == 11
    assert result["eval_count"] == 5


@pytest.mark.asyncio
async def test_transient_provider_status_is_retryable():
    session = FakeSession(FakeResponse({}, status=429))
    provider = OllamaProvider("http://localhost:11434", "model", 0.2)

    with pytest.raises(ProviderTransientError):
        await provider.chat(session, [], [])


def test_factory_selects_openai_compatible_provider(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "openai-compatible")
    monkeypatch.setattr(config, "AI_BASE_URL", "https://provider.test/v1")
    monkeypatch.setattr(config, "AI_MODEL", "model")
    monkeypatch.setattr(config, "AI_API_KEY", "")

    assert isinstance(build_provider(), OpenAICompatibleProvider)


def test_openai_provider_requires_api_key(monkeypatch):
    monkeypatch.setattr(config, "AI_PROVIDER", "openai")
    monkeypatch.setattr(config, "AI_API_KEY", "")

    with pytest.raises(ValueError, match="AI_API_KEY"):
        build_provider()
