import asyncio

import pytest

import ai
from automation.context import ExecutionContext


class _FakeClientSession:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def test_write_tool_audit_redacts_file_content():
    arguments = ai._audit_tool_arguments(
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

    monkeypatch.setattr(ai.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai, "_chat", fake_chat)

    answer = await ai.ask_local_ai("What is the answer?", mode="agent")

    assert answer == "agent answer"
    assert observed["schemas"] == []
    assert "No tools are available" in observed["system_prompt"]


async def test_chat_mode_exposes_web_tool_schemas(monkeypatch):
    observed_names = []

    async def fake_chat(session, messages, tool_schemas, think=False):
        observed_names.extend(schema["function"]["name"] for schema in tool_schemas)
        return {"message": {"content": "answer"}}

    monkeypatch.setattr(ai.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai, "_chat", fake_chat)

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

    monkeypatch.setattr(ai.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai, "_chat", fake_chat)
    monkeypatch.setattr(ai, "detect_language_code", lambda text: "en")

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

    monkeypatch.setattr(ai.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai, "_chat", fake_chat)
    monkeypatch.setattr(ai, "detect_language_code", lambda text: "en")

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

    monkeypatch.setattr(ai.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai, "_chat", fake_chat)

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

    monkeypatch.setattr(ai.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai, "_chat", fake_chat)

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


async def test_agent_job_exposes_workspace_tools_and_audits_calls(
    monkeypatch,
    tmp_path,
):
    calls = 0
    observed = {}
    tool_events = []
    (tmp_path / "notes.txt").write_text("workspace facts", encoding="utf-8")

    async def fake_chat(session, messages, tool_schemas, think=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            observed["schemas"] = {
                schema["function"]["name"] for schema in tool_schemas
            }
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
        observed["tool_results"] = [
            message["content"]
            for message in messages
            if message["role"] == "tool"
        ]
        return {"message": {"content": "These are the workspace facts."}}

    monkeypatch.setattr(ai.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai, "_chat", fake_chat)

    result = await ai.execute_local_ai(
        "inspect workspace",
        mode="agent",
        reply_language=ai.ReplyLanguage("en", "English", "test"),
        execution_context=ExecutionContext("job_test", tmp_path),
        tool_event_callback=tool_events.append,
    )

    assert result.status == "completed"
    assert observed["schemas"] == {
        "list_workspace_files",
        "read_workspace_file",
        "search_workspace",
    }
    assert observed["tool_results"][0].startswith(
        "[workspace file: notes.txt; sha256: "
    )
    assert observed["tool_results"][0].endswith("\nworkspace facts")
    assert tool_events[0]["tool_name"] == "read_workspace_file"
    assert tool_events[0]["status"] == "finished"
    assert tool_events[0]["result_size"] > 0


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
                                "arguments": {"query": "agent request"},
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

    answer = await ai.ask_local_ai("question", mode="agent")

    assert answer == "blocked"
    assert tool_results == ["tool is not allowed in agent mode: search_web"]


async def test_attached_file_tool_is_scoped_to_current_request(monkeypatch):
    calls = 0
    observed = {}

    async def fake_chat(session, messages, tool_schemas, think=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            observed["schema_names"] = [
                schema["function"]["name"] for schema in tool_schemas
            ]
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read_attached_file",
                                "arguments": {"attachment_id": "1"},
                            }
                        }
                    ],
                }
            }

        observed["tool_results"] = [
            message["content"]
            for message in messages
            if message["role"] == "tool"
        ]
        return {"message": {"content": "file answer"}}

    monkeypatch.setattr(ai.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai, "_chat", fake_chat)

    answer = await ai.ask_local_ai(
        "read attachment 1",
        mode="chat",
        attachments={"1": ("notes.txt", b"chat contents")},
    )

    assert answer == "file answer"
    assert "read_attached_file" in observed["schema_names"]
    assert observed["tool_results"] == [
        "[attached file: notes.txt]\nchat contents"
    ]


async def test_attachment_language_does_not_change_reply_language(monkeypatch):
    observed = {}

    async def fake_chat(session, messages, tool_schemas, think=False):
        observed["system_prompt"] = messages[0]["content"]
        return {"message": {"content": "สรุปภาษาไทย"}}

    monkeypatch.setattr(ai.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai, "_chat", fake_chat)

    await ai.ask_local_ai(
        "สรุปไฟล์นี้",
        mode="chat",
        attachments={"1": ("english.txt", b"English document contents")},
    )

    assert "Reply only in Thai (language code: th)" in observed["system_prompt"]


async def test_wrong_language_answer_is_rewritten_without_tools(monkeypatch):
    calls = 0
    observed = {}

    async def fake_chat(session, messages, tool_schemas, think=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "message": {
                    "content": "This is an English summary of the document."
                }
            }

        observed["correction_system_prompt"] = messages[0]["content"]
        observed["correction_tool_schemas"] = tool_schemas
        return {"message": {"content": "นี่คือสรุปภาษาไทยของเอกสาร"}}

    monkeypatch.setattr(ai.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai, "_chat", fake_chat)

    answer = await ai.ask_local_ai("สรุปเอกสารนี้", mode="agent")

    assert answer == "นี่คือสรุปภาษาไทยของเอกสาร"
    assert "entirely in Thai" in observed["correction_system_prompt"]
    assert observed["correction_tool_schemas"] == []
    assert calls == 2


async def test_correct_language_answer_is_not_rewritten(monkeypatch):
    calls = 0

    async def fake_chat(session, messages, tool_schemas, think=False):
        nonlocal calls
        calls += 1
        return {"message": {"content": "คำตอบเป็นภาษาไทยอยู่แล้ว"}}

    monkeypatch.setattr(ai.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai, "_chat", fake_chat)

    answer = await ai.ask_local_ai("ตอบคำถามนี้", mode="agent")

    assert answer == "คำตอบเป็นภาษาไทยอยู่แล้ว"
    assert calls == 1


async def test_summary_tool_uses_chunk_completion_without_tools(monkeypatch):
    main_calls = 0
    observed = {}

    async def fake_chat(
        session,
        messages,
        tool_schemas,
        think=False,
        max_output_tokens=None,
    ):
        nonlocal main_calls
        if max_output_tokens is not None:
            observed["chunk_tools"] = tool_schemas
            observed["chunk_output_limit"] = max_output_tokens
            return {"message": {"content": "document facts [document]"}}

        main_calls += 1
        if main_calls == 1:
            observed["schema_names"] = {
                schema["function"]["name"] for schema in tool_schemas
            }
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "summarize_attachment",
                                "arguments": {
                                    "attachment_id": "1",
                                    "detail": "brief",
                                },
                            }
                        }
                    ],
                }
            }
        return {"message": {"content": "สรุปข้อเท็จจริงจากเอกสาร"}}

    monkeypatch.setattr(ai.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai, "_chat", fake_chat)

    answer = await ai.ask_local_ai(
        "สรุปไฟล์นี้",
        mode="chat",
        attachments={"1": ("notes.txt", b"document facts")},
    )

    assert answer == "สรุปข้อเท็จจริงจากเอกสาร"
    assert "summarize_attachment" in observed["schema_names"]
    assert "search_attachment" in observed["schema_names"]
    assert observed["chunk_tools"] == []
    assert observed["chunk_output_limit"] == 140
