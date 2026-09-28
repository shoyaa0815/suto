import asyncio

import pytest

import ai
from llm.types import ModelResponse
from planning import Plan, PlanStep, Planner, Replanner
from tests.support.ai_helpers import FakeClientSession, patch_model_chat


class FakeModel:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        return self.response


@pytest.mark.parametrize("prompt, expected", [
    ("hello", False),
    ("What did you inspect and fix yesterday?", False),
    ("inspect this project, fix the bug, run tests", True),
    ("ตรวจโค้ด แล้วแก้บั๊กและรันทดสอบ", True),
    ("1. Inspect code\n2. Run tests", True),
])
def test_planner_selects_explicit_multistep_requests(prompt, expected):
    assert Planner.needs_plan(prompt) is expected


async def test_planner_uses_tool_free_model_request_and_validates_output():
    model = FakeModel(ModelResponse('{"steps": ["Inspect the code", "Run tests"]}'))
    plan = await Planner(model).create("inspect code and run tests")

    assert plan.steps == (PlanStep("Inspect the code"), PlanStep("Run tests"))
    assert plan.status == "proposed"
    assert model.requests[0].available_tools == []
    assert model.requests[0].messages[-1]["content"] == plan.goal
    with pytest.raises(ValueError, match="2 to 8"):
        Planner.parse(plan.goal, ModelResponse('{"steps": ["only one"]}'))
    with pytest.raises(ValueError, match="strings"):
        Planner.parse(plan.goal, ModelResponse('{"steps": ["valid", 2]}'))


def test_replanner_preserves_completed_work_and_blocks_failed_run():
    plan = Plan("fix bug", (
        PlanStep("Inspect", "completed"),
        PlanStep("Fix", "running"),
        PlanStep("Test"),
    ), status="active")
    replanner = Replanner()

    blocked = replanner.block(plan, "tool unavailable")
    revised = replanner.revise(blocked, ["Use alternate tool"], "new information")

    assert blocked.status == "blocked"
    assert revised.steps == (PlanStep("Inspect", "completed"), PlanStep("Use alternate tool"))
    assert revised.revision == 1
    assert revised.reason == "new information"
    assert plan.status == "active"
    with pytest.raises(ValueError, match="reason"):
        replanner.revise(plan, ["One remaining step"], " ")
    full = Plan("full", tuple(PlanStep(str(index), "completed") for index in range(7)))
    with pytest.raises(ValueError, match="1 to 1"):
        replanner.revise(full, ["Extra one", "Extra two"], "new information")


async def test_complex_request_injects_plan_and_returns_it_without_claiming_work(monkeypatch):
    requests = []

    async def fake_chat(_session, messages, schemas, think=False):
        requests.append((messages, schemas))
        if len(requests) == 1:
            return {
                "message": {"content": '{"steps": ["Inspect files", "Run tests"]}'},
                "prompt_eval_count": 11,
                "eval_count": 5,
            }
        return {
            "message": {"content": "I need workspace access to inspect files."},
            "prompt_eval_count": 17,
            "eval_count": 8,
        }

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    monkeypatch.setattr(ai.response, "detect_language_code", lambda _text: "en")
    patch_model_chat(monkeypatch)

    result = await ai.execute_local_ai(
        "inspect files and run tests",
        reply_language=ai.ReplyLanguage("en", "English", "test"),
    )

    assert result.status == "completed"
    assert result.plan is not None
    assert result.plan.status == "proposed"
    assert all(step.status == "pending" for step in result.plan.steps)
    assert (result.prompt_tokens, result.output_tokens) == (28, 13)
    assert requests[0][1] == []
    assert "Proposed plan" in requests[1][0][-1]["content"]


async def test_invalid_plan_falls_back_to_regular_agent_request(monkeypatch):
    requests = []

    async def fake_chat(_session, messages, schemas, think=False):
        requests.append((messages, schemas))
        if len(requests) == 1:
            return {"message": {"content": "not JSON"}, "eval_count": 3}
        return {"message": {"content": "Please provide the project."}}

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    monkeypatch.setattr(ai.response, "detect_language_code", lambda _text: "en")
    patch_model_chat(monkeypatch)

    result = await ai.execute_local_ai(
        "inspect files and run tests",
        reply_language=ai.ReplyLanguage("en", "English", "test"),
    )

    assert result.status == "completed"
    assert result.plan is None
    assert result.output_tokens == 3
    assert requests[1][0][-1]["content"] == "inspect files and run tests"


@pytest.mark.parametrize("planning_error", [RuntimeError("provider failed"), TimeoutError()])
async def test_failed_planning_call_falls_back_to_agent(monkeypatch, planning_error):
    requests = []

    async def fake_chat(_session, messages, schemas, think=False):
        requests.append((messages, schemas))
        if len(requests) == 1:
            raise planning_error
        return {"message": {"content": "I can help with that."}}

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    monkeypatch.setattr(ai.response, "detect_language_code", lambda _text: "en")
    patch_model_chat(monkeypatch)

    result = await ai.execute_local_ai(
        "inspect files and run tests",
        reply_language=ai.ReplyLanguage("en", "English", "test"),
    )

    assert result.status == "completed"
    assert result.text == "I can help with that."
    assert result.plan is None
    assert len(requests) == 2
    assert requests[0][1] == []
    assert requests[1][0][-1]["content"] == "inspect files and run tests"


async def test_planning_timeout_falls_back_to_agent(monkeypatch):
    requests = []

    async def fake_chat(_session, messages, schemas, think=False):
        requests.append((messages, schemas))
        if len(requests) == 1:
            await asyncio.Event().wait()
        return {"message": {"content": "I can help with that."}}

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    monkeypatch.setattr(ai.config, "AI_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(ai.response, "detect_language_code", lambda _text: "en")
    patch_model_chat(monkeypatch)

    result = await ai.execute_local_ai(
        "inspect files and run tests",
        reply_language=ai.ReplyLanguage("en", "English", "test"),
    )

    assert result.status == "completed"
    assert result.plan is None
    assert len(requests) == 2
    assert requests[1][0][-1]["content"] == "inspect files and run tests"


async def test_cancelling_planning_does_not_start_agent(monkeypatch):
    started = asyncio.Event()
    calls = 0

    async def fake_chat(_session, _messages, _schemas, think=False):
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    patch_model_chat(monkeypatch)

    task = asyncio.create_task(ai.execute_local_ai("inspect files and run tests"))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 1


async def test_simple_request_does_not_call_planner(monkeypatch):
    requests = []

    async def fake_chat(_session, messages, schemas, think=False):
        requests.append((messages, schemas))
        return {"message": {"content": "Hello."}}

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    monkeypatch.setattr(ai.response, "detect_language_code", lambda _text: "en")
    patch_model_chat(monkeypatch)

    result = await ai.execute_local_ai(
        "hello", reply_language=ai.ReplyLanguage("en", "English", "test")
    )

    assert result.plan is None
    assert len(requests) == 1
    assert requests[0][0][-1]["content"] == "hello"


async def test_planned_request_with_failed_tool_is_blocked_without_success_claim(monkeypatch):
    calls = 0

    async def fake_chat(_session, _messages, _schemas, think=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"message": {"content": '{"steps": ["Inspect", "Run tests"]}'}}
        return {"message": {"tool_calls": [{"function": {
            "name": "get_current_datetime", "arguments": {"unexpected": True},
        }}]}}

    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    patch_model_chat(monkeypatch)

    result = await ai.execute_local_ai("inspect files and run tests")

    assert result.status == "failed"
    assert result.plan is not None and result.plan.status == "blocked"
    assert all(step.status == "pending" for step in result.plan.steps)
    assert calls == 2
