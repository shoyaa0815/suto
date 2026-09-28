from ai.providers.model import ChatModelAdapter
from llm.types import ModelRequest


async def test_existing_chat_response_becomes_provider_independent_values(monkeypatch):
    seen = {}

    async def fake_chat(session, messages, tools, think=False):
        seen.update({"session": session, "messages": messages, "tools": tools, "think": think})
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "function": {"name": "test.echo", "arguments": {"value": "ok"}},
                }],
            },
            "prompt_eval_count": 7,
            "eval_count": 2,
        }

    monkeypatch.setattr("ai.providers.model.client.chat", fake_chat)
    session = object()
    messages = [{"role": "user", "content": "echo"}]
    schemas = [{"type": "function", "function": {"name": "test.echo"}}]
    response = await ChatModelAdapter(session).generate(
        ModelRequest(messages, schemas, {"think": True})
    )

    assert seen == {"session": session, "messages": messages, "tools": schemas, "think": True}
    assert response.tool_calls[0].name == "test.echo"
    assert response.tool_calls[0].arguments == {"value": "ok"}
    assert response.tool_calls[0].id == "call-1"
    assert response.usage.prompt_tokens == 7
    assert response.usage.output_tokens == 2
