import asyncio

import pytest

from agent import AgentRequest, AgentRuntime, RunState, RuntimeHooks
from agent.limits import ExecutionLimits
from llm.types import ModelResponse, ModelUsage, ToolCall
from permissions import PermissionEngine, PermissionPolicy
from tools.registry import FunctionTool, ToolRegistry, ToolValidationError
from tools.types import ToolResult


class FakeModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def registry(handler=None):
    tools = ToolRegistry()
    tools.register(FunctionTool(
        "test.echo", "Echo a value", {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }, handler or (lambda value: value),
    ))
    return tools


async def test_final_result_has_usage_session_and_terminal_event():
    model = FakeModel(ModelResponse("hello", usage=ModelUsage(3, 2)))
    runtime = AgentRuntime(model, registry())

    result = await runtime.run(AgentRequest("hi", session_id="session-1"), [{"role": "user", "content": "hi"}])

    assert result.final_text == "hello"
    assert result.status == "completed"
    assert result.session_id == "session-1"
    assert result.usage == {"prompt_tokens": 3, "output_tokens": 2}
    assert runtime.state.status == RunState.COMPLETED
    assert [event.type for event in runtime.events] == [
        "agent.preparing", "agent.thinking", "agent.completed"
    ]
    assert {event.run_id for event in runtime.events} == {runtime.events[0].run_id}


async def test_tool_call_is_validated_executed_and_observed():
    called = []
    tools = registry(lambda value: called.append(value) or value.upper())
    model = FakeModel(
        ModelResponse("", [ToolCall("test.echo", {"value": "ok"}, "call-1")]),
        ModelResponse("done"),
    )
    runtime = AgentRuntime(model, tools)
    messages = [{"role": "user", "content": "echo"}]

    result = await runtime.run(AgentRequest("echo"), messages)

    assert result.status == "completed"
    assert called == ["ok"]
    assert model.requests[1].messages[-1] == {
        "role": "tool", "content": "OK", "tool_call_id": "call-1"
    }
    assert any(event.type == "agent.authorizing" for event in runtime.events)
    assert any(event.type == "agent.observing" for event in runtime.events)


@pytest.mark.parametrize("arguments", [
    {}, {"value": 3}, {"value": "ok", "extra": True}, ["ok"],
])
async def test_invalid_arguments_never_reach_tool(arguments):
    called = []
    model = FakeModel(ModelResponse("", [ToolCall("test.echo", arguments)]))
    runtime = AgentRuntime(model, registry(lambda **kwargs: called.append(kwargs)))

    result = await runtime.run(AgentRequest("echo"), [])

    assert result.status == "failed"
    assert result.error.startswith("invalid tool arguments")
    assert called == []


async def test_invalid_arguments_are_rejected_before_permission_check():
    class RecordAuthorization(RuntimeHooks):
        def __init__(self):
            self.calls = []

        async def authorize(self, name, args):
            self.calls.append((name, args))
            return True

    hooks = RecordAuthorization()
    runtime = AgentRuntime(
        FakeModel(ModelResponse("", [ToolCall("test.echo", {"value": 42})])),
        registry(), hooks=hooks,
    )

    assert (await runtime.run(AgentRequest("echo"), [])).status == "failed"
    assert hooks.calls == []


async def test_registered_tool_is_available_without_runtime_changes():
    called = []

    class UpperTool:
        name = "test.upper"
        description = "Uppercase text"
        input_schema = {
            "type": "object", "properties": {"text": {"type": "string"}},
            "required": ["text"], "additionalProperties": False,
        }

        async def execute(self, args):
            called.append(args["text"])
            return ToolResult(ok=True, content=args["text"].upper())

    tools = ToolRegistry()
    tools.register(UpperTool())
    model = FakeModel(
        ModelResponse("", [ToolCall("test.upper", {"text": "hello"}, "call-2")]),
        ModelResponse("done"),
    )
    runtime = AgentRuntime(model, tools)

    result = await runtime.run(AgentRequest("upper"), [])

    assert result.status == "completed"
    assert called == ["hello"]
    assert model.requests[0].available_tools[0]["function"]["name"] == "test.upper"
    assert model.requests[1].messages[-1] == {
        "role": "tool", "content": "HELLO", "tool_call_id": "call-2"
    }


async def test_permission_hook_cannot_change_validated_execution_arguments():
    class MutatingAuthorization(RuntimeHooks):
        async def authorize(self, name, args):
            args["value"] = 7
            return True

    called = []
    runtime = AgentRuntime(
        FakeModel(
            ModelResponse("", [ToolCall("test.echo", {"value": "safe"})]),
            ModelResponse("done"),
        ),
        registry(lambda value: called.append(value) or value),
        hooks=MutatingAuthorization(),
    )

    assert (await runtime.run(AgentRequest("echo"), [])).status == "completed"
    assert called == ["safe"]


async def test_tool_result_failure_cannot_be_wrapped_as_success():
    tools = registry(lambda value: ToolResult(False, "rejected", error="write rejected"))
    model = FakeModel(ModelResponse("", [ToolCall("test.echo", {"value": "x"})]))
    runtime = AgentRuntime(model, tools)
    messages = []

    result = await runtime.run(AgentRequest("echo"), messages)

    assert result.status == "failed"
    assert result.error == "write rejected"
    assert len(model.requests) == 1
    assert not any(message.get("role") == "tool" for message in messages)


async def test_unknown_tool_and_denied_tool_do_not_execute():
    called = []
    unknown = AgentRuntime(FakeModel(ModelResponse("", [ToolCall("missing", {})])), registry())
    assert (await unknown.run(AgentRequest("x"), [])).status == "blocked"

    class Deny(RuntimeHooks):
        async def authorize(self, name, args):
            return False

    denied = AgentRuntime(
        FakeModel(ModelResponse("", [ToolCall("test.echo", {"value": "x"})])),
        registry(lambda **kwargs: called.append(kwargs)), hooks=Deny(),
    )
    assert (await denied.run(AgentRequest("x"), [])).status == "blocked"
    assert called == []

    policy_denied = AgentRuntime(
        FakeModel(ModelResponse("", [ToolCall("test.echo", {"value": "x"})])),
        registry(lambda **kwargs: called.append(kwargs)),
        permissions=PermissionEngine(PermissionPolicy({"test.echo": "deny"})),
    )
    result = await policy_denied.run(AgentRequest("x"), [])
    assert result.status == "blocked"
    assert called == []


async def test_tool_failure_and_iteration_limit_cannot_claim_success():
    def fail(value):
        raise RuntimeError("broken")

    failing = AgentRuntime(
        FakeModel(ModelResponse("", [ToolCall("test.echo", {"value": "x"})])),
        registry(fail),
    )
    failure = await failing.run(AgentRequest("x"), [])
    assert failure.status == "failed"
    assert failure.error == "tool failed: test.echo"
    assert failing.state.status == RunState.FAILED

    looping = AgentRuntime(
        FakeModel(ModelResponse("", [ToolCall("test.echo", {"value": "x"})])),
        registry(), limits=ExecutionLimits(max_iterations=1),
    )
    result = await looping.run(AgentRequest("x"), [])
    assert result.status == "failed"
    assert "stopped after 1" in result.error


async def test_model_error_and_cancellation_record_terminal_state():
    failing = AgentRuntime(FakeModel(RuntimeError("model unavailable")), registry())
    with pytest.raises(RuntimeError, match="model unavailable"):
        await failing.run(AgentRequest("x"), [])
    assert failing.state.status == RunState.FAILED

    started = asyncio.Event()

    class WaitingModel:
        async def generate(self, request):
            started.set()
            await asyncio.Event().wait()

    waiting = AgentRuntime(WaitingModel(), registry())
    task = asyncio.create_task(waiting.run(AgentRequest("x"), []))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert waiting.state.status == RunState.CANCELLED


async def test_tool_timeout_and_approval_keep_correct_terminal_states():
    async def wait_forever(value):
        await asyncio.Event().wait()

    timed_out = AgentRuntime(
        FakeModel(ModelResponse("", [ToolCall("test.echo", {"value": "x"})])),
        registry(wait_forever),
        limits=ExecutionLimits(tool_timeout_seconds=0.01),
    )
    result = await timed_out.run(AgentRequest("x"), [])
    assert result.status == "timed_out"
    assert timed_out.state.status == RunState.FAILED

    class ApprovalNeeded(Exception):
        approval_id = "approval-1"

    def ask_approval(value):
        raise ApprovalNeeded()

    approving = AgentRuntime(
        FakeModel(ModelResponse("", [ToolCall("test.echo", {"value": "x"})])),
        registry(ask_approval),
        passthrough_exceptions=(ApprovalNeeded,),
    )
    with pytest.raises(ApprovalNeeded):
        await approving.run(AgentRequest("x"), [])
    assert approving.state.status == RunState.WAITING_FOR_APPROVAL


def test_registry_rejects_duplicates_and_enforces_nested_schema():
    tools = registry()
    with pytest.raises(ValueError, match="already registered"):
        tools.register(FunctionTool("test.echo", "", {"type": "object"}, lambda: None))
    tool = tools.resolve("test.echo")
    with pytest.raises(ToolValidationError):
        tools.validate(tool, {"value": 2})
    assert tools.export_model_schemas()[0]["function"]["name"] == "test.echo"
