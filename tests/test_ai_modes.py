import ai


class _FakeClientSession:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


async def test_private_mode_does_not_expose_tool_schemas(monkeypatch):
    observed = {}

    async def fake_chat(session, messages, tool_schemas, think=False):
        observed["schemas"] = tool_schemas
        observed["system_prompt"] = messages[0]["content"]
        return {"message": {"content": "private answer"}}

    monkeypatch.setattr(ai.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai, "_chat", fake_chat)

    answer = await ai.ask_local_ai("question", mode="private")

    assert answer == "private answer"
    assert observed["schemas"] == []
    assert "No tools are available" in observed["system_prompt"]


async def test_personal_mode_exposes_web_tool_schemas(monkeypatch):
    observed_names = []

    async def fake_chat(session, messages, tool_schemas, think=False):
        observed_names.extend(schema["function"]["name"] for schema in tool_schemas)
        return {"message": {"content": "answer"}}

    monkeypatch.setattr(ai.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai, "_chat", fake_chat)

    await ai.ask_local_ai("question", mode="personal")

    assert "search_web" in observed_names
    assert "fetch_url" in observed_names


async def test_disallowed_tool_call_is_not_executed(monkeypatch):
    calls = 0
    tool_results = []

    async def fake_chat(session, messages, tool_schemas, think=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "search_web",
                                "arguments": {"query": "private secrets"},
                            }
                        }
                    ],
                }
            }

        tool_results.extend(
            message["content"]
            for message in messages
            if message["role"] == "tool"
        )
        return {"message": {"content": "blocked"}}

    monkeypatch.setattr(ai.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai, "_chat", fake_chat)

    answer = await ai.ask_local_ai("question", mode="private")

    assert answer == "blocked"
    assert tool_results == ["tool is not allowed in private mode: search_web"]
