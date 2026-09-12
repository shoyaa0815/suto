import ai
from automation.runtime.context import COMMAND_TOOLS, PLANNING_TOOLS, ExecutionContext
from automation.storage.store import JobStore
from tests.ai_helpers import FakeClientSession as _FakeClientSession


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

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

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


async def test_agent_job_can_create_a_persistent_plan(monkeypatch, tmp_path):
    calls = 0
    observed_schemas = set()
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("inspect and report")
    store.claim_next_job()

    async def fake_chat(session, messages, tool_schemas, think=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            observed_schemas.update(
                schema["function"]["name"] for schema in tool_schemas
            )
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "create_plan",
                                "arguments": {
                                    "steps": ["Inspect files", "Write report"]
                                },
                            }
                        }
                    ],
                }
            }
        return {"message": {"content": "Plan created."}}

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    context = ExecutionContext(
        job.id,
        tmp_path,
        allowed_tools=PLANNING_TOOLS,
        plan_store=store,
    )

    result = await ai.execute_local_ai(
        "inspect and report",
        mode="agent",
        execution_context=context,
        reply_language=ai.ReplyLanguage("en", "English", "test"),
    )

    assert result.status == "completed"
    assert observed_schemas == PLANNING_TOOLS
    assert [step.description for step in store.list_steps(job.id)] == [
        "Inspect files",
        "Write report",
    ]


async def test_agent_job_runs_allowlisted_command_and_audits_it(
    monkeypatch,
    tmp_path,
):
    calls = 0
    observed = {}
    command_events = []
    (tmp_path / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )

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
                                "name": "run_workspace_command",
                                "arguments": {
                                    "command": ["pytest", "-q", "test_sample.py"]
                                },
                            }
                        }
                    ],
                }
            }
        observed["tool_result"] = next(
            message["content"]
            for message in messages
            if message["role"] == "tool"
        )
        return {"message": {"content": "Checks passed."}}

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    context = ExecutionContext(
        "job_test",
        tmp_path,
        allowed_tools=COMMAND_TOOLS,
        command_event_callback=command_events.append,
        approval_callback=lambda *args: None,
    )

    result = await ai.execute_local_ai(
        "run checks",
        mode="agent",
        execution_context=context,
        reply_language=ai.ReplyLanguage("en", "English", "test"),
    )

    assert result.status == "completed"
    assert observed["schemas"] == COMMAND_TOOLS
    assert "status: completed" in observed["tool_result"]
    assert command_events[0]["exit_code"] == 0


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

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)

    answer = await ai.ask_local_ai("question", mode="agent")

    assert answer == "blocked"
    assert tool_results == ["tool is not allowed in agent mode: search_web"]
