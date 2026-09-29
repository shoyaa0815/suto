import asyncio
import json

import pytest
import ai
from ai.execution import loop

from agent import AgentRequest, AgentRuntime, RunState, RuntimeHooks
from agent.limits import ExecutionLimits
from agent.subagents import SubAgentManager
from ai.execution.request import prepare_request
from application.language import ReplyLanguage
from llm.types import ModelResponse, ToolCall
from permissions import PermissionEngine, PermissionPolicy
from skills import Skill, SkillRegistry
from tools.registry import FunctionTool, ToolRegistry
from tools.delegation import DELEGATE_NAME, DELEGATE_SCHEMA
from workflows.storage.store import JobStore
from workflows.runtime.context import ExecutionContext
from tests.support.ai_helpers import FakeClientSession


class RoutedModel:
    def __init__(self, parent, child):
        self.parent = iter(parent)
        self.child = iter(child)
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        response = next(self.child if "sub-agent" in request.messages[0]["content"] else self.parent)
        if isinstance(response, Exception):
            raise response
        return response


class Trace(RuntimeHooks):
    def __init__(self, store=None, deny=()):
        self.store = store
        self.events = []
        self.deny = deny

    async def on_event(self, event):
        self.events.append(event)
        if self.store:
            self.store.add_run_event(event)

    async def authorize(self, name, args):
        return name not in self.deny


def setup(model, *, extra=(), active=(), skills=None, limits=None, store=None,
          rules=None, deny=(), max_children=1, depth=0):
    registry = ToolRegistry()
    for tool in extra:
        registry.register(tool)
    holder = {}
    registry.register(FunctionTool(DELEGATE_NAME, "Delegate", DELEGATE_SCHEMA,
                                   lambda **args: holder["manager"].delegate(**args)))
    policy = {name: "allow" for name in [DELEGATE_NAME, *(tool.name for tool in extra)]}
    policy.update(rules or {})
    hooks = Trace(store, deny)
    runtime = AgentRuntime(model, registry, hooks=hooks,
                           permissions=PermissionEngine(PermissionPolicy(policy)),
                           limits=limits or ExecutionLimits(max_iterations=4))
    holder["manager"] = SubAgentManager(runtime, skills or SkillRegistry(),
                                        max_children=max_children, depth=depth)
    return runtime, holder["manager"], hooks


def echo(name):
    return FunctionTool(name, "Echo", {"type": "object", "properties": {},
                                      "additionalProperties": False}, lambda: name)


async def test_delegation_reuses_runtime_and_persists_correlated_trace(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    user = store.resolve_channel_identity("cli", "local")
    session = store.get_or_create_conversation(user.id, "cli", "local")
    model = RoutedModel(
        [ModelResponse("", [ToolCall(DELEGATE_NAME, {"role": "research", "task": "Find facts", "context": "selected"}, "p1")]),
         ModelResponse("parent answer")],
        [ModelResponse("", [ToolCall("search_web", {}, "c1")]), ModelResponse("child answer")],
    )
    runtime, _, hooks = setup(model, extra=(echo("search_web"),), store=store)
    messages = [{"role": "system", "content": "private parent system"},
                {"role": "user", "content": "private parent conversation"}]
    result = await runtime.run(AgentRequest("delegate", session_id=session.id), messages)

    assert result.status == "completed" and result.final_text == "parent answer"
    child_request = model.requests[1]
    assert "private parent" not in str(child_request.messages)
    assert "selected" in str(child_request.messages)
    assert {item["function"]["name"] for item in child_request.available_tools} == {"search_web"}
    observation = json.loads(model.requests[-1].messages[-1]["content"])
    assert observation["status"] == "completed" and observation["final_text"] == "child answer"
    child_id = observation["child_run_id"]
    assert child_id != runtime.run_id
    parent_events = store.list_run_events(runtime.run_id)
    child_events = store.list_run_events(child_id)
    assert any(item["event_type"] == "delegation.completed" and
               json.loads(item["data"])["child_run_id"] == child_id for item in parent_events)
    assert child_events and all(item["parent_run_id"] == runtime.run_id for item in child_events)
    assert all(item["session_id"] == session.id for item in parent_events + child_events)
    assert {item["event_type"] for item in child_events} >= {
        "model.requested", "tool.completed", "permission.allowed", "agent.completed"}
    assert not any("private parent" in item["data"] for item in parent_events + child_events)
    assert hooks.events[-1].type == "agent.completed"
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


@pytest.mark.parametrize("name,role,selected,visible", [
    ("apply_workspace_patch", "review", None, False),
    ("mcp.docs.read", "review", ["mcp.docs.read"], False),
    ("mcp.docs.read", "coding", ["mcp.docs.read"], True),
    ("mcp.docs.read", "coding", None, False),
])
async def test_native_and_mcp_selection_uses_parent_and_role_policy(name, role, selected, visible):
    model = RoutedModel([], [ModelResponse("done")])
    runtime, manager, _ = setup(model, extra=(echo(name),))
    runtime.run_id = "parent"
    runtime.request = AgentRequest("x")
    result = await manager.delegate(role, "inspect", allowed_tools=selected)
    if selected and not visible:
        assert not result.ok
        assert not model.requests
    else:
        assert result.ok
        tools = {item["function"]["name"] for item in model.requests[0].available_tools}
        assert (name in tools) is visible


async def test_skill_model_and_permission_inheritance_fail_closed():
    skills = SkillRegistry()
    skills.register(Skill("coding", "Coding", "Skill instruction", allowed_tools=("read_workspace_file",)))
    model = RoutedModel([], [ModelResponse("done")])
    runtime, manager, _ = setup(model, extra=(echo("read_workspace_file"), echo("apply_workspace_patch")),
                                skills=skills, rules={"read_workspace_file": "deny"})
    runtime.run_id = "parent"
    runtime.request = AgentRequest("x", active_skills=("coding",))
    result = await manager.delegate("coding", "task")
    assert result.ok
    request = model.requests[0]
    assert request.available_tools == []
    assert "Skill instruction" in request.messages[0]["content"]
    assert request.generation_options == {}
    assert (await manager.delegate("coding", "again")).error == "budget_exhausted"


async def test_parent_permission_is_rechecked_when_child_calls_tool():
    class ChangePolicy:
        runtime = None

        async def generate(self, request):
            self.runtime.permissions.policy.rules["read_workspace_file"] = "deny"
            return ModelResponse("", [ToolCall("read_workspace_file", {})])

    model = ChangePolicy()
    runtime, manager, _ = setup(model, extra=(echo("read_workspace_file"),))
    model.runtime = runtime
    runtime.run_id = "parent"
    runtime.request = AgentRequest("x")
    result = json.loads((await manager.delegate("coding", "task")).content)
    assert result["status"] == "blocked"


async def test_child_tool_and_iteration_caps():
    called = []
    read = FunctionTool("read_workspace_file", "Read", {"type": "object", "properties": {},
                                                        "additionalProperties": False},
                        lambda: called.append(1) or "ok")
    model = RoutedModel([], [ModelResponse("", [ToolCall("read_workspace_file", {}) for _ in range(7)])])
    runtime, manager, _ = setup(model, extra=(read,))
    runtime.run_id = "parent"
    runtime.request = AgentRequest("x")
    result = json.loads((await manager.delegate("coding", "task")).content)
    assert result["status"] == "blocked" and len(called) == 6

    model = RoutedModel([], [ModelResponse("", [ToolCall("read_workspace_file", {})]) for _ in range(3)])
    runtime, manager, _ = setup(model, extra=(read,))
    runtime.run_id = "parent"
    runtime.request = AgentRequest("x")
    result = json.loads((await manager.delegate("coding", "task")).content)
    assert result["status"] == "failed" and len(model.requests) == 3


async def test_child_cannot_delegate_or_expand_tool_selection():
    model = RoutedModel([], [ModelResponse("", [ToolCall(DELEGATE_NAME, {"role": "coding", "task": "again"})])])
    runtime, manager, hooks = setup(model, extra=(echo("read_workspace_file"),))
    runtime.run_id = "parent"
    runtime.request = AgentRequest("x")
    denied = await manager.delegate("coding", "task", allowed_tools=["unknown"])
    assert not denied.ok and not model.requests
    result = await manager.delegate("coding", "task")
    assert json.loads(result.content)["status"] == "blocked"
    assert any(event.type == "tool.failed" for event in hooks.events)
    assert manager.spawned == 1
    another, deep, _ = setup(model, depth=1)
    another.run_id = "another"
    another.request = AgentRequest("x")
    assert (await deep.delegate("research", "task")).error == "budget_exhausted"


async def test_child_limits_failure_timeout_and_parent_cancellation():
    model = RoutedModel([], [RuntimeError("private model error")])
    runtime, manager, hooks = setup(model)
    runtime.run_id = "parent"
    runtime.request = AgentRequest("x")
    failure = json.loads((await manager.delegate("research", "task")).content)
    assert failure["status"] == "failed" and "private" not in failure["final_text"]
    assert any(event.type == "agent.failed" for event in hooks.events)

    class HangingModel:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def generate(self, request):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

    hanging = HangingModel()
    runtime, manager, hooks = setup(hanging, limits=ExecutionLimits(model_timeout_seconds=.01))
    runtime.run_id = "parent"
    runtime.request = AgentRequest("x")
    timed = json.loads((await manager.delegate("research", "task")).content)
    assert timed["status"] == "timed_out" and hanging.cancelled.is_set()

    tool_cancelled = asyncio.Event()

    async def wait_tool():
        try:
            await asyncio.Event().wait()
        finally:
            tool_cancelled.set()

    read = FunctionTool("read_workspace_file", "Read", {"type": "object", "properties": {},
                                                        "additionalProperties": False}, wait_tool)
    model = RoutedModel([], [ModelResponse("", [ToolCall("read_workspace_file", {})])])
    runtime, manager, _ = setup(model, extra=(read,),
                                limits=ExecutionLimits(tool_timeout_seconds=.01))
    runtime.run_id = "parent"
    runtime.request = AgentRequest("x")
    timed = json.loads((await manager.delegate("coding", "task")).content)
    assert timed["status"] == "timed_out" and tool_cancelled.is_set()

    hanging = HangingModel()
    parent_model = RoutedModel(
        [ModelResponse("", [ToolCall(DELEGATE_NAME, {"role": "research", "task": "task"})])], [])
    async def generate(request):
        if "sub-agent" in request.messages[0]["content"]:
            return await hanging.generate(request)
        return await RoutedModel.generate(parent_model, request)
    parent_model.generate = generate
    runtime, _, hooks = setup(parent_model)
    task = asyncio.create_task(runtime.run(AgentRequest("x"), [{"role": "system", "content": "parent"}]))
    await asyncio.wait_for(hanging.started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hanging.cancelled.is_set() and runtime.state.status == RunState.CANCELLED
    assert any(event.type == "delegation.cancelled" for event in hooks.events)


def test_request_policy_exposes_delegation_only_where_authorized(tmp_path):
    plain = prepare_request("x", "agent", None, ReplyLanguage("en", "English", "test"),
                            "", [], None, None)
    assert DELEGATE_NAME in plain.allowed_tools
    skills = SkillRegistry()
    skills.register(Skill("limited", "Limited", "Only search", allowed_tools=("search_web",)))
    limited = prepare_request("x", "agent", None, ReplyLanguage("en", "English", "test"),
                              "", [], None, None, active_skills=("limited",), skill_registry=skills)
    assert DELEGATE_NAME not in limited.allowed_tools
    job = ExecutionContext("job", tmp_path, allowed_tools=frozenset({"read_workspace_file"}))
    job_prepared = prepare_request("x", "agent", None, ReplyLanguage("en", "English", "test"),
                                   "", [], None, job)
    assert DELEGATE_NAME not in job_prepared.allowed_tools
    job = ExecutionContext("job", tmp_path,
                           allowed_tools=frozenset({"read_workspace_file", DELEGATE_NAME}))
    job_prepared = prepare_request("x", "agent", None, ReplyLanguage("en", "English", "test"),
                                   "", [], None, job)
    assert DELEGATE_NAME in job_prepared.allowed_tools


async def test_public_ai_lifecycle_executes_delegation_through_registered_tool(monkeypatch):
    model = RoutedModel(
        [ModelResponse("", [ToolCall(DELEGATE_NAME, {"role": "research", "task": "summarize"})]),
         ModelResponse("Parent completed")],
        [ModelResponse("Child completed")],
    )
    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(loop, "build_model_router", lambda session: model)
    monkeypatch.setattr(ai.response, "detect_language_code", lambda text: "en")
    result = await ai.execute_local_ai("Delegate this task", mode="agent",
                                       reply_language=ReplyLanguage("en", "English", "test"))
    assert result.status == "completed" and result.text == "Parent completed"
    assert json.loads(model.requests[-1].messages[-1]["content"])["final_text"] == "Child completed"
