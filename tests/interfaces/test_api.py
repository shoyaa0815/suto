"""Local API behavior through the existing executor and persistent trace."""

import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from agent import AgentRequest, AgentRuntime
from ai.execution import loop
from interfaces.api.server import HOST, STATE, create_app
from llm.types import ModelResponse, ToolCall
from mcp_integration.config import MCPConfig
from workflows.storage.store import JobStore


HEADERS = {"X-Suto-Request": "1"}


class Model:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def setup(tmp_path, monkeypatch, model_factory):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(loop, "build_model_router", lambda session: model_factory())
    monkeypatch.setattr("interfaces.api.server.load_settings", lambda: type("Settings", (), {
        "profile": type("Profile", (), {"display_name": "Local", "timezone": "UTC", "locale": "en"})()
    })())
    monkeypatch.setattr("ai.executor.load_mcp_config", lambda: MCPConfig(()))
    monkeypatch.setattr("ai.response.detect_language_code", lambda text: "en")
    store = JobStore(tmp_path / "suto.db")
    return create_app(store=store), store


async def start(client, message="Please answer in English: hello", **fields):
    response = await client.post("/runs", json={"message": message, **fields}, headers=HEADERS)
    return response, await response.json()


async def finished(app, run_id):
    run = app[STATE]["runs"][run_id]
    await run.task
    return run


async def test_health_direct_response_session_continuation_and_trace(tmp_path, monkeypatch):
    models = []

    def factory():
        model = Model(ModelResponse("Answer one" if not models else "Answer two"))
        models.append(model)
        return model

    app, store = setup(tmp_path, monkeypatch, factory)
    async with TestClient(TestServer(app)) as client:
        health = await client.get("/health")
        assert await health.json() == {"status": "ok"}
        response, first = await start(client)
        assert response.status == 202
        await finished(app, first["run_id"])
        result = await (await client.get(f"/runs/{first['run_id']}")).json()
        assert result["status"] == "completed"
        assert result["result"]["final_text"] == "Answer one"
        assert result["result"]["session_id"] == first["session_id"]
        assert result["runtime_run_id"] != first["run_id"]

        stream = await client.get(f"/runs/{first['run_id']}/events")
        assert stream.status == 200
        assert stream.headers["Content-Type"] == "text/event-stream"
        events = [json.loads(line[6:]) for line in (await stream.text()).splitlines() if line.startswith("data: ")]
        assert events[-1]["type"] == "agent.completed"
        assert {event["run_id"] for event in events} == {result["runtime_run_id"]}
        assert {event["session_id"] for event in events} == {first["session_id"]}
        assert len(store.list_run_events(result["runtime_run_id"])) == len(events)
        resume = await client.get(
            f"/runs/{first['run_id']}/events", headers={"Last-Event-ID": events[-1]["event_id"]}
        )
        assert await resume.text() == ""
        assert (await client.get(
            f"/runs/{first['run_id']}/events", headers={"Last-Event-ID": "unknown"}
        )).status == 400

        response, second = await start(client, "Please answer in English: continue", session_id=first["session_id"])
        assert response.status == 202
        await finished(app, second["run_id"])
        assert second["session_id"] == first["session_id"]
        assert any("Answer one" == item.get("content") for item in models[1].requests[0].messages)
        messages = store.list_messages(first["session_id"])
        assert [item.role for item in messages] == ["user", "assistant", "user", "assistant"]


async def test_tool_event_and_denied_tool_and_mcp_are_scoped(tmp_path, monkeypatch):
    models = iter([
        Model(ModelResponse("", [ToolCall("get_current_datetime", {})]), ModelResponse("Time checked")),
        Model(ModelResponse("", [ToolCall("run_workspace_command", {"command": "pwd"})])),
        Model(ModelResponse("", [ToolCall("mcp.fake.exec", {})])),
    ])
    app, _ = setup(tmp_path, monkeypatch, lambda: next(models))
    async with TestClient(TestServer(app)) as client:
        _, direct = await start(client)
        await finished(app, direct["run_id"])
        result = await (await client.get(f"/runs/{direct['run_id']}")).json()
        assert result["status"] == "completed"
        events = await (await client.get(f"/runs/{direct['run_id']}/events")).text()
        assert "tool.completed" in events
        assert "tool_call_id" in events

        _, denied = await start(client)
        await finished(app, denied["run_id"])
        assert (await (await client.get(f"/runs/{denied['run_id']}")).json())["status"] == "blocked"
        denied_events = await (await client.get(f"/runs/{denied['run_id']}/events")).text()
        assert "tool.started" not in denied_events

        _, mcp = await start(client)
        await finished(app, mcp["run_id"])
        assert (await (await client.get(f"/runs/{mcp['run_id']}")).json())["status"] == "blocked"
        assert "mcp.fake.exec" not in await (await client.get(f"/runs/{mcp['run_id']}/events")).text()


async def test_cancellation_streaming_and_independent_runs(tmp_path, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    class WaitingModel:
        async def generate(self, request):
            entered.set()
            await release.wait()
            return ModelResponse("done")

    app, _ = setup(tmp_path, monkeypatch, lambda: WaitingModel())
    async with TestClient(TestServer(app)) as client:
        _, first = await start(client)
        await entered.wait()
        _, second = await start(client)
        assert first["run_id"] != second["run_id"]
        assert first["session_id"] != second["session_id"]
        assert (await (await client.get(f"/runs/{first['run_id']}")).json())["status"] == "running"

        stream = await client.get(f"/runs/{first['run_id']}/events")
        data = ""
        while "data: " not in data:
            data += (await stream.content.readline()).decode()
        assert first["session_id"] in data
        response = await client.post(f"/runs/{first['run_id']}/cancel", json={}, headers=HEADERS)
        assert response.status == 202
        try:
            await finished(app, first["run_id"])
        except asyncio.CancelledError:
            pass
        assert (await (await client.get(f"/runs/{first['run_id']}")).json())["status"] == "cancelled"
        release.set()
        await finished(app, second["run_id"])
        assert (await (await client.get(f"/runs/{second['run_id']}")).json())["status"] == "completed"
        assert "agent.cancelled" in await stream.text()


async def test_same_session_conflict_and_immediate_cancel_releases_slot(tmp_path, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    class WaitingModel:
        async def generate(self, request):
            entered.set()
            await release.wait()
            return ModelResponse("done")

    app, _ = setup(tmp_path, monkeypatch, lambda: WaitingModel())
    async with TestClient(TestServer(app)) as client:
        _, first = await start(client)
        await entered.wait()
        conflict, _ = await start(client, session_id=first["session_id"])
        assert conflict.status == 409
        await client.post(f"/runs/{first['run_id']}/cancel", json={}, headers=HEADERS)
        try:
            await finished(app, first["run_id"])
        except asyncio.CancelledError:
            pass
        _, immediate = await start(client, session_id=first["session_id"])
        await client.post(f"/runs/{immediate['run_id']}/cancel", json={}, headers=HEADERS)
        try:
            await finished(app, immediate["run_id"])
        except asyncio.CancelledError:
            pass
        assert (await (await client.get(f"/runs/{immediate['run_id']}")).json())["status"] == "cancelled"
        assert app[STATE]["active_sessions"] == {}


async def test_two_sse_readers_receive_same_terminal_trace(tmp_path, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    class WaitingModel:
        async def generate(self, request):
            entered.set()
            await release.wait()
            return ModelResponse("done")

    app, _ = setup(tmp_path, monkeypatch, lambda: WaitingModel())
    async with TestClient(TestServer(app)) as client:
        _, run = await start(client)
        await entered.wait()
        first = await client.get(f"/runs/{run['run_id']}/events")
        second = await client.get(f"/runs/{run['run_id']}/events")
        release.set()
        await finished(app, run["run_id"])
        assert "agent.completed" in await first.text()
        assert "agent.completed" in await second.text()


async def test_known_skill_is_selected_only_through_registry(tmp_path, monkeypatch):
    model = Model(ModelResponse("Research summary"))
    app, _ = setup(tmp_path, monkeypatch, lambda: model)
    async with TestClient(TestServer(app)) as client:
        _, run = await start(client, skills=["research"])
        await finished(app, run["run_id"])
        assert (await (await client.get(f"/runs/{run['run_id']}")).json())["status"] == "completed"
        assert "Gather relevant information" in model.requests[0].messages[0]["content"]


async def test_bad_requests_skills_identity_and_local_guard(tmp_path, monkeypatch):
    app, store = setup(tmp_path, monkeypatch, lambda: Model(ModelResponse("done")))
    foreign = store.resolve_channel_identity("tui", "local")
    foreign_session = store.get_or_create_conversation(foreign.id, "tui", "local")
    async with TestClient(TestServer(app)) as client:
        for payload in ({"message": ""}, {"message": "x", "mcp_command": "/bin/sh"},
                        {"message": "x", "skills": ["/tmp/SKILL.md"]},
                        {"message": "x", "delegation_depth": 10}):
            response = await client.post("/runs", json=payload, headers=HEADERS)
            assert response.status == 400
        assert (await client.post("/runs", data="{", headers={**HEADERS, "Content-Type": "application/json"})).status == 400
        assert (await client.post("/runs", json={"message": "x", "session_id": foreign_session.id}, headers=HEADERS)).status == 404
        assert (await client.post("/runs", json={"message": "x"})).status == 403
        assert (await client.get("/health", headers={"Host": "evil.example"})).status == 403
        assert (await client.post("/runs", json={"message": "x"}, headers={**HEADERS, "Origin": "http://evil.example"})).status == 403
        assert app[STATE]["runs"] == {}
    assert HOST == "127.0.0.1"


async def test_model_failure_is_sanitized_and_no_http_type_enters_runtime(tmp_path, monkeypatch):
    secret = "private-provider-token"
    observed = []
    original = AgentRuntime.run

    async def inspect(self, request, messages, **kwargs):
        observed.append(request)
        return await original(self, request, messages, **kwargs)

    monkeypatch.setattr(AgentRuntime, "run", inspect)
    app, _ = setup(tmp_path, monkeypatch, lambda: Model(RuntimeError(secret)))
    async with TestClient(TestServer(app)) as client:
        _, run = await start(client)
        await finished(app, run["run_id"])
        result = await (await client.get(f"/runs/{run['run_id']}")).json()
        events = await (await client.get(f"/runs/{run['run_id']}/events")).text()
        assert result["status"] == "failed"
        assert secret not in json.dumps(result) + events
        assert isinstance(observed[0], AgentRequest)
        assert observed[0].metadata == {}


async def test_tool_exception_is_sanitized(tmp_path, monkeypatch):
    secret = "private-tool-token"
    model = Model(ModelResponse("", [ToolCall("get_current_datetime", {})]))
    app, _ = setup(tmp_path, monkeypatch, lambda: model)

    async def broken_tool():
        raise RuntimeError(secret)

    monkeypatch.setattr("ai.executor.build_runtime_tools", lambda **kwargs: {
        "get_current_datetime": broken_tool
    })
    async with TestClient(TestServer(app)) as client:
        _, run = await start(client)
        await finished(app, run["run_id"])
        result = await (await client.get(f"/runs/{run['run_id']}")).json()
        events = await (await client.get(f"/runs/{run['run_id']}/events")).text()
        assert result["status"] == "failed"
        assert secret not in json.dumps(result) + events


async def test_subagent_cannot_gain_tools_or_budget_from_http(tmp_path, monkeypatch):
    model = Model(ModelResponse("", [ToolCall("agent.delegate", {
        "role": "coding", "task": "run a command", "allowed_tools": ["run_workspace_command"]
    })]))
    app, _ = setup(tmp_path, monkeypatch, lambda: model)
    async with TestClient(TestServer(app)) as client:
        _, run = await start(client)
        await finished(app, run["run_id"])
        result = await (await client.get(f"/runs/{run['run_id']}")).json()
        assert result["status"] == "failed"
        events = await (await client.get(f"/runs/{run['run_id']}/events")).text()
        assert "delegation.completed" not in events
        assert "tool.failed" in events
        assert "run_workspace_command" not in events


async def test_delegated_child_events_keep_parent_correlation(tmp_path, monkeypatch):
    model = Model(
        ModelResponse("", [ToolCall("agent.delegate", {"role": "research", "task": "summarize"})]),
        ModelResponse("Child summary"),
        ModelResponse("Parent answer"),
    )
    app, store = setup(tmp_path, monkeypatch, lambda: model)
    async with TestClient(TestServer(app)) as client:
        _, run = await start(client)
        await finished(app, run["run_id"])
        status = await (await client.get(f"/runs/{run['run_id']}")).json()
        assert status["status"] == "completed"
        stream = await (await client.get(f"/runs/{run['run_id']}/events")).text()
        events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")]
        children = [event for event in events if event["parent_run_id"] == status["runtime_run_id"]]
        assert children
        assert {event["run_id"] for event in children} != {status["runtime_run_id"]}
        assert all(event["session_id"] == run["session_id"] for event in children)
        assert len(store.list_run_tree_events(status["runtime_run_id"])) == len(events)


async def test_api_cancel_reaches_delegated_child(tmp_path, monkeypatch):
    child_entered = asyncio.Event()

    class DelegatingModel:
        def __init__(self):
            self.calls = 0

        async def generate(self, request):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse("", [ToolCall("agent.delegate", {
                    "role": "research", "task": "wait for evidence"
                })])
            child_entered.set()
            await asyncio.Event().wait()

    app, _ = setup(tmp_path, monkeypatch, lambda: DelegatingModel())
    async with TestClient(TestServer(app)) as client:
        _, run = await start(client)
        await child_entered.wait()
        await client.post(f"/runs/{run['run_id']}/cancel", json={}, headers=HEADERS)
        try:
            await finished(app, run["run_id"])
        except asyncio.CancelledError:
            pass
        status = await (await client.get(f"/runs/{run['run_id']}")).json()
        assert status["status"] == "cancelled"
        events = await (await client.get(f"/runs/{run['run_id']}/events")).text()
        assert "delegation.cancelled" in events
        assert events.count("agent.cancelled") >= 2
