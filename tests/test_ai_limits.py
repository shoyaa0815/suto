import asyncio

import ai
from automation.context import ExecutionContext, ExecutionLimits
from tests.ai_helpers import FakeClientSession as _FakeClientSession


async def test_agent_job_is_blocked_when_token_budget_is_exceeded(
    monkeypatch,
    tmp_path,
):
    async def fake_chat(session, messages, tool_schemas, think=False):
        return {
            "message": {"content": "finished"},
            "prompt_eval_count": 11,
        }

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    context = ExecutionContext(
        "job_budget",
        tmp_path,
        limits=ExecutionLimits(max_tokens=10),
    )

    result = await ai.execute_local_ai(
        "inspect",
        mode="agent",
        execution_context=context,
        reply_language=ai.ReplyLanguage("en", "English", "test"),
    )

    assert result.status == "blocked"
    assert result.error == "job token limit exceeded (10 tokens)"


async def test_agent_job_interrupts_work_at_elapsed_time_limit(
    monkeypatch,
    tmp_path,
):
    async def fake_chat(session, messages, tool_schemas, think=False):
        await asyncio.sleep(1)

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    context = ExecutionContext(
        "job_timeout",
        tmp_path,
        limits=ExecutionLimits(max_elapsed_seconds=0.01),
    )

    result = await ai.execute_local_ai(
        "inspect",
        mode="agent",
        execution_context=context,
        reply_language=ai.ReplyLanguage("en", "English", "test"),
    )

    assert result.status == "blocked"
    assert result.error == "job elapsed-time limit reached (0.01 seconds)"


async def test_agent_job_blocks_repeated_identical_tool_calls(
    monkeypatch,
    tmp_path,
):
    (tmp_path / "notes.txt").write_text("facts", encoding="utf-8")
    tool_events = []

    async def fake_chat(session, messages, tool_schemas, think=False):
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_workspace_file",
                            "arguments": {"path": "notes.txt"},
                        }
                    }
                ],
            }
        }

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    context = ExecutionContext(
        "job_loop",
        tmp_path,
        limits=ExecutionLimits(repeated_tool_call_limit=2),
    )

    result = await ai.execute_local_ai(
        "inspect",
        mode="agent",
        execution_context=context,
        tool_event_callback=tool_events.append,
        reply_language=ai.ReplyLanguage("en", "English", "test"),
    )

    assert result.status == "blocked"
    assert "repeated identical tool call" in result.error
    assert [event["status"] for event in tool_events] == ["finished", "blocked"]


async def test_agent_job_blocks_tool_calls_over_budget(monkeypatch, tmp_path):
    (tmp_path / "one.txt").write_text("one", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two", encoding="utf-8")

    async def fake_chat(session, messages, tool_schemas, think=False):
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_workspace_file",
                            "arguments": {"path": "one.txt"},
                        }
                    },
                    {
                        "function": {
                            "name": "read_workspace_file",
                            "arguments": {"path": "two.txt"},
                        }
                    },
                ],
            }
        }

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    context = ExecutionContext(
        "job_tools",
        tmp_path,
        limits=ExecutionLimits(max_tool_calls=1),
    )

    result = await ai.execute_local_ai(
        "inspect",
        mode="agent",
        execution_context=context,
        reply_language=ai.ReplyLanguage("en", "English", "test"),
    )

    assert result.status == "blocked"
    assert result.error == "job tool-call limit exceeded (1 calls)"
