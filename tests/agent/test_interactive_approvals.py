"""Generic permission-to-interface approval handoff remains fail closed."""

import asyncio

import pytest

from agent import AgentRequest, AgentRuntime, RuntimeHooks
from llm.types import ModelResponse, ToolCall
from permissions import ApprovalBroker, PermissionEngine, PermissionPolicy
from tools.registry import FunctionTool, ToolRegistry


class Model:
    def __init__(self):
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        return (ModelResponse("", [ToolCall("test.action", {})]) if self.calls == 1
                else ModelResponse("done"))


class Hooks(RuntimeHooks):
    def __init__(self, broker):
        self.broker = broker

    async def request_approval(self, run_id, tool_call_id, tool_name):
        return await self.broker.request(run_id, tool_call_id, tool_name)


def runtime(broker, called):
    tools = ToolRegistry()
    tools.register(FunctionTool("test.action", "test", {
        "type": "object", "properties": {}, "additionalProperties": False,
    }, lambda: called.append(True) or "ok"))
    return AgentRuntime(Model(), tools, hooks=Hooks(broker),
                        permissions=PermissionEngine(PermissionPolicy({
                            "test.action": "require_approval",
                        })))


@pytest.mark.parametrize("choice,expected", [
    ("allow_once", "completed"), ("deny", "blocked"),
])
async def test_approval_is_exactly_one_tool_call(choice, expected):
    seen = []
    broker = ApprovalBroker(lambda request: broker.submit(request.id, choice))
    agent = runtime(broker, seen)
    result = await agent.run(AgentRequest("act", run_id="canonical"), [])
    assert result.status == expected
    assert seen == ([True] if choice == "allow_once" else [])
    assert agent.run_id == "canonical"
    assert not broker.pending


async def test_pending_approval_cancels_and_restart_broker_cannot_replay():
    entered = asyncio.Event()
    pending = []

    def on_request(request):
        pending.append(request)
        entered.set()

    broker = ApprovalBroker(on_request)
    seen = []
    agent = runtime(broker, seen)
    task = asyncio.create_task(agent.run(AgentRequest("act", run_id="run-1"), []))
    await entered.wait()
    request = pending[0]
    assert request.run_id == "run-1"
    assert request.tool_call_id.startswith("run-1:")
    assert not ApprovalBroker().submit(request.id, "allow_once")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not broker.submit(request.id, "allow_once")
    assert seen == []


async def test_expired_approval_fails_closed():
    seen = []
    broker = ApprovalBroker(timeout_seconds=0)
    agent = runtime(broker, seen)
    assert (await agent.run(AgentRequest("act"), [])).status == "blocked"
    assert seen == []
    assert not broker.pending


async def test_expiration_cancels_interface_prompt():
    prompt_cancelled = asyncio.Event()

    async def waiting_prompt(request):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            prompt_cancelled.set()
            raise

    broker = ApprovalBroker(waiting_prompt, timeout_seconds=0.01)
    request, answer = await broker.request("run", "run:1:1", "test.action")
    assert answer.choice == "deny"
    assert prompt_cancelled.is_set()
    assert not broker.submit(request.id, "allow_once")
