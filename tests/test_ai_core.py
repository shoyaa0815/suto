import asyncio

import pytest

import ai
from tests.ai_helpers import FakeClientSession as _FakeClientSession


def test_write_tool_audit_redacts_file_content():
    arguments = ai.tool_runtime.audit_tool_arguments(
        "apply_workspace_patch",
        {
            "path": "notes.txt",
            "content": "private content",
            "expected_sha256": "before",
        },
    )

    assert "content" not in arguments
    assert arguments["content_size"] == len(b"private content")
    assert len(arguments["content_sha256"]) == 64
    assert arguments["path"] == "notes.txt"


async def test_agent_mode_does_not_expose_tool_schemas(monkeypatch):
    observed = {}

    async def fake_chat(session, messages, tool_schemas, think=False):
        observed["schemas"] = tool_schemas
        observed["system_prompt"] = messages[0]["content"]
        return {"message": {"content": "agent answer"}}

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

    answer = await ai.ask_local_ai("What is the answer?", mode="agent")

    assert answer == "agent answer"
    assert observed["schemas"] == []
    assert "No tools are available" in observed["system_prompt"]


async def test_chat_mode_exposes_web_tool_schemas(monkeypatch):
    observed_names = []

    async def fake_chat(session, messages, tool_schemas, think=False):
        observed_names.extend(schema["function"]["name"] for schema in tool_schemas)
        return {"message": {"content": "answer"}}

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

    await ai.ask_local_ai("question", mode="chat")

    assert "search_web" in observed_names
    assert "fetch_url" in observed_names


async def test_progress_reports_model_usage_and_completion(monkeypatch):
    updates = []

    async def fake_chat(session, messages, tool_schemas, think=False):
        return {
            "message": {"content": "This is a complete answer."},
            "prompt_eval_count": 120,
            "eval_count": 30,
        }

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    monkeypatch.setattr(ai.response, "detect_language_code", lambda text: "en")

    answer = await ai.ask_local_ai(
        "question",
        mode="agent",
        reply_language=ai.ReplyLanguage("en", "English", "test"),
        progress_callback=updates.append,
    )

    assert answer == "This is a complete answer."
    assert any(update["activity"] == "model" for update in updates)
    assert updates[-1]["activity"] == "finished"
    assert updates[-1]["prompt_tokens"] == 120
    assert updates[-1]["output_tokens"] == 30
    assert updates[-1]["total_tokens"] == 150


async def test_structured_execution_result_contains_usage(monkeypatch):
    async def fake_chat(session, messages, tool_schemas, think=False):
        return {
            "message": {"content": "This is the result."},
            "prompt_eval_count": 50,
            "eval_count": 10,
        }

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    monkeypatch.setattr(ai.response, "detect_language_code", lambda text: "en")

    result = await ai.execute_local_ai(
        "question",
        mode="agent",
        reply_language=ai.ReplyLanguage("en", "English", "test"),
    )

    assert result.status == "completed"
    assert result.text == "This is the result."
    assert result.prompt_tokens == 50
    assert result.output_tokens == 10


async def test_cancelled_execution_reports_cancelled_progress(monkeypatch):
    started = asyncio.Event()
    updates = []

    async def fake_chat(session, messages, tool_schemas, think=False):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

    task = asyncio.create_task(
        ai.execute_local_ai(
            "long task",
            mode="agent",
            progress_callback=updates.append,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert updates[-1]["activity"] == "finished"
    assert updates[-1]["detail"] == "cancelled"


async def test_progress_reports_tool_and_loop(monkeypatch):
    calls = 0
    updates = []

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
                                "name": "get_current_datetime",
                                "arguments": {},
                            }
                        }
                    ],
                },
                "prompt_eval_count": 100,
                "eval_count": 10,
            }
        return {
            "message": {"content": "done"},
            "prompt_eval_count": 140,
            "eval_count": 20,
        }

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

    answer = await ai.ask_local_ai(
        "current time",
        mode="chat",
        progress_callback=updates.append,
    )

    assert answer == "done"
    tool_update = next(update for update in updates if update["activity"] == "tool")
    assert tool_update["round"] == 1
    assert "get_current_datetime" in tool_update["detail"]
    assert any(update["activity"] == "tool_done" for update in updates)
    assert updates[-1]["total_tokens"] == 270


async def test_tool_result_preserves_provider_tool_call_id(monkeypatch):
    calls = 0

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
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "get_current_datetime",
                                "arguments": {},
                            },
                        }
                    ],
                }
            }

        tool_message = next(
            message for message in messages if message["role"] == "tool"
        )
        assert tool_message["tool_call_id"] == "call_123"
        return {"message": {"role": "assistant", "content": "done"}}

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

    assert await ai.ask_local_ai("current time", mode="chat") == "done"
