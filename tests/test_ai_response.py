import ai
from tests.ai_helpers import FakeClientSession as _FakeClientSession


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

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

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

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

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

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

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

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

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

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

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

