import asyncio
import os
import sys
from pathlib import Path

import pytest

import ai
from agent import AgentRequest, AgentRuntime
from ai.execution.request import eligible_mcp_config, prepare_request
from application.language import ReplyLanguage
from llm.types import ModelResponse, ToolCall
from mcp_integration import MCPConfig, MCPManager, MCPServerConfig, load_mcp_config, parse_mcp_config
from mcp_integration.adapter import adapt_tool
from permissions import PermissionEngine, PermissionPolicy
from skills import Skill, SkillRegistry
from tests.support.ai_helpers import FakeClientSession, patch_model_chat
from tools.registry import FunctionTool, ToolRegistry, ToolValidationError
from workflows.runtime.context import ExecutionContext


SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": False,
}


class FakeClient:
    instances = []

    def __init__(self, config, definitions=None, fail=None):
        self.config = config
        self.definitions = definitions if definitions is not None else [
            {"name": "echo", "description": "Echo", "inputSchema": SCHEMA}
        ]
        self.fail = fail
        self.calls = []
        self.closed = False
        self.connected = False
        self.instances.append(self)

    async def connect(self):
        if self.fail:
            raise RuntimeError("secret server failure")
        self.connected = True

    async def list_tools(self):
        return self.definitions

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if self.fail == "call":
            raise RuntimeError("secret transport detail")
        return {"content": [{"type": "text", "text": args["message"].upper()}]}

    async def close(self):
        self.closed = True


def server(name="one", allowed=("echo",)):
    return MCPServerConfig(name, "fake", allow_tools=frozenset(allowed))


def test_mcp_configuration_is_explicit_and_keeps_environment_values_out(tmp_path, monkeypatch):
    path = tmp_path / "mcp.yaml"
    path.write_text(
        f"servers:\n  files:\n    transport: stdio\n    command: {sys.executable}\n"
        "    args: [server.py]\n    env: {API_TOKEN: SUTO_TEST_TOKEN}\n"
        "    allow_tools: [echo]\n", encoding="utf-8",
    )
    path.chmod(0o600)
    monkeypatch.setenv("SUTO_TEST_TOKEN", "private-token")
    parsed = load_mcp_config(path)
    assert parsed.servers[0].resolved_env() == {"API_TOKEN": "private-token"}
    assert "private-token" not in repr(parsed)
    assert "private-token" not in path.read_text()
    with pytest.raises(ValueError, match="cannot read MCP configuration"):
        load_mcp_config(tmp_path / "missing.yaml")
    with pytest.raises(ValueError, match="environment variable is unavailable"):
        monkeypatch.delenv("SUTO_TEST_TOKEN")
        parsed.servers[0].resolved_env()


def test_mcp_commands_require_an_explicit_local_config_path(tmp_path, monkeypatch):
    config = tmp_path / "mcp.yaml"
    config.write_text("servers:\n  local:\n    transport: stdio\n    command: /bin/true\n    allow_tools: [echo]\n", encoding="utf-8")
    config.chmod(0o600)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUTO_MCP_CONFIG", raising=False)
    assert load_mcp_config() == MCPConfig()
    monkeypatch.setenv("SUTO_MCP_CONFIG", str(config))
    assert load_mcp_config().servers[0].name == "local"
    monkeypatch.setenv("SUTO_MCP_CONFIG", "mcp.yaml")
    with pytest.raises(ValueError, match="absolute"):
        load_mcp_config()
    monkeypatch.setenv("SUTO_MCP_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(ValueError, match="cannot read MCP configuration"):
        load_mcp_config()
    link = tmp_path / "linked.yaml"
    link.symlink_to(config)
    with pytest.raises(ValueError, match="absolute, regular"):
        load_mcp_config(link)
    if os.name == "posix":
        config.chmod(0o666)
        with pytest.raises(ValueError, match="cannot read MCP configuration"):
            load_mcp_config(config)


@pytest.mark.parametrize("source", [
    {"servers": {"one": {"transport": "http", "command": "x"}}},
    {"servers": {"one": {"transport": "stdio", "command": "x", "env": {"KEY": "literal-secret!"}}}},
    {"servers": {"one": {"transport": "stdio", "command": "x", "allow_tools": ["echo", "echo"]}}},
    {"servers": {"one": {"transport": "stdio", "command": "python", "allow_tools": ["echo"]}}},
])
def test_invalid_mcp_configuration_is_rejected(source):
    with pytest.raises(ValueError):
        parse_mcp_config(source)


def test_duplicate_yaml_server_keys_fail_closed(tmp_path):
    path = tmp_path / "mcp.yaml"
    path.write_text("servers:\n  one: {}\n  one: {}\n", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="cannot read MCP configuration"):
        load_mcp_config(path)


async def test_multiple_servers_namespacing_discovery_registration_and_cleanup():
    config = MCPConfig((server("one"), server("two")))
    manager = MCPManager(config, FakeClient)
    async with manager:
        assert set(manager.tools) == {"mcp.one.echo", "mcp.two.echo"}
        assert set(manager.allowed_tools()) == set(manager.tools)
        registry = ToolRegistry()
        registry.register(FunctionTool("echo", "native", {"type": "object"}, lambda: None))
        for tool in manager.allowed_tools().values():
            registry.register(tool)
        assert registry.resolve("echo").description == "native"
        assert len(registry.list_tools()) == 3
        with pytest.raises(ValueError, match="already registered"):
            registry.register(manager.tools["mcp.one.echo"])
        native_collision = ToolRegistry()
        native_collision.register(FunctionTool("mcp.one.echo", "native", {"type": "object"}, lambda: None))
        with pytest.raises(ValueError, match="already registered"):
            native_collision.register(manager.tools["mcp.one.echo"])
        result = await manager.tools["mcp.one.echo"].execute({"message": "hi"})
        assert result.ok and result.content == "HI"
        assert result.metadata == {"mcp_server": "one", "mcp_tool": "echo"}
        assert manager._clients[0].calls == [("echo", {"message": "hi"})]
    assert all(client.closed for client in FakeClient.instances[-2:])
    assert manager.tools == {}


async def test_bad_server_or_duplicate_discovery_does_not_poison_other_servers():
    clients = {}

    def factory(config):
        definitions = (
            [{"name": "echo", "inputSchema": SCHEMA}] * 2
            if config.name == "duplicate" else None
        )
        client = FakeClient(config, definitions, fail=config.name == "offline")
        clients[config.name] = client
        return client

    async with MCPManager(MCPConfig((server("offline"), server("duplicate"), server("good"))), factory) as manager:
        assert manager.failures == {"offline": "RuntimeError", "duplicate": "ValueError"}
        assert set(manager.tools) == {"mcp.good.echo"}
        assert clients["offline"].closed and clients["duplicate"].closed
    assert clients["good"].closed


async def test_cancelled_startup_closes_current_and_previous_clients():
    started = asyncio.Event()
    clients = {}

    class PendingClient(FakeClient):
        async def connect(self):
            self.connected = True
            started.set()
            await asyncio.Event().wait()

    def factory(config):
        client = PendingClient(config) if config.name == "pending" else FakeClient(config)
        clients[config.name] = client
        return client

    manager = MCPManager(MCPConfig((server("first"), server("pending"))), factory)
    task = asyncio.create_task(manager.start())
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clients["first"].closed and clients["pending"].closed
    assert manager.tools == {}


async def test_server_does_not_start_without_explicit_tool_allowlist():
    def unexpected_client(_config):
        raise AssertionError("server command must not start")

    async with MCPManager(MCPConfig((server(allowed=()),)), unexpected_client) as manager:
        assert manager.tools == {}
        assert manager.allowed_tools() == {}


@pytest.mark.parametrize("definition", [
    {"name": "../escape", "inputSchema": SCHEMA},
    {"name": "echo", "inputSchema": {"type": "array"}},
    {"name": "echo", "inputSchema": {"type": "object", "properties": {"x": {"type": "integer", "minimum": "bad"}}}},
    {"name": "echo", "inputSchema": {"type": "object", "properties": {"x": {"oneOf": [{"type": "string"}]}}}},
])
def test_malformed_or_unsupported_tool_definitions_are_rejected(definition):
    with pytest.raises(ValueError):
        adapt_tool("one", definition, FakeClient(server()))


async def test_adapter_preserves_schema_and_normalizes_results_and_errors():
    client = FakeClient(server())
    tool = adapt_tool("one", {"name": "echo", "description": "Echo", "inputSchema": SCHEMA}, client)
    registry = ToolRegistry()
    registry.register(tool)
    assert registry.export_model_schemas()[0]["function"]["parameters"] == SCHEMA
    with pytest.raises(ToolValidationError):
        registry.validate(tool, {"message": 1})
    assert (await tool.execute({"message": "ok"})).content == "OK"
    client.call_tool = lambda *args: raise_error("secret call failure")
    failure = await tool.execute({"message": "ok"})
    assert not failure.ok and failure.error == "mcp_server_error"
    assert "secret" not in str(failure)
    async def reported_error(*args):
        return {"isError": True, "content": [{"type": "text", "text": "private"}]}
    client.call_tool = reported_error
    failure = await tool.execute({"message": "ok"})
    assert not failure.ok and failure.error == "mcp_tool_error"
    assert "private" not in str(failure)
    async def structured(*args):
        return {"structuredContent": {"answer": 3}, "content": []}
    client.call_tool = structured
    assert (await tool.execute({"message": "ok"})).content == {"answer": 3}


async def raise_error(message):
    raise RuntimeError(message)


async def test_mcp_runtime_permission_denial_and_argument_validation():
    client = FakeClient(server())
    tool = adapt_tool("one", {"name": "echo", "inputSchema": SCHEMA}, client)
    registry = ToolRegistry()
    registry.register(tool)

    class Model:
        async def generate(self, _request):
            return ModelResponse("", [ToolCall("mcp.one.echo", {"message": "hi"})])

    denied = AgentRuntime(Model(), registry, permissions=PermissionEngine(PermissionPolicy()))
    assert (await denied.run(AgentRequest("echo"), [])).status == "blocked"
    assert client.calls == []
    bad_model = type("BadModel", (), {"generate": lambda self, request: bad_call()})()
    async def bad_call():
        return ModelResponse("", [ToolCall("mcp.one.echo", {"message": 7})])
    invalid = AgentRuntime(bad_model, registry, permissions=PermissionEngine(PermissionPolicy({"mcp.one.echo": "allow"})))
    assert (await invalid.run(AgentRequest("echo"), [])).status == "failed"
    assert client.calls == []


def test_skill_tool_names_restrict_and_recommend_mcp_without_special_handling():
    tool = adapt_tool("one", {"name": "echo", "inputSchema": SCHEMA}, FakeClient(server()))
    skills = SkillRegistry()
    skills.register(Skill("mcp-skill", "MCP", "Use echo", ("mcp.one.echo",), ("mcp.one.echo",)))
    prepared = prepare_request("hello", "agent", None, None, "", None, None, None,
                               active_skills=("mcp-skill",), skill_registry=skills,
                               mcp_tools={tool.name: tool})
    assert prepared.allowed_tools == frozenset({"mcp.one.echo"})
    assert [schema["function"]["name"] for schema in prepared.tool_schemas] == ["mcp.one.echo"]
    assert "Recommended available tools: mcp.one.echo" in prepared.messages[0]["content"]
    skills.register(Skill("no-tools", "none", "Use none", allowed_tools=()))
    blocked = prepare_request("hello", "agent", None, None, "", None, None, None,
                              active_skills=("no-tools",), skill_registry=skills,
                              mcp_tools={tool.name: tool})
    assert blocked.allowed_tools == frozenset()
    assert eligible_mcp_config(MCPConfig((server(),)), None, ("no-tools",), skills).servers == ()


def test_job_mcp_remains_disabled_even_with_a_context_allowlist(tmp_path):
    tool = adapt_tool("one", {"name": "echo", "inputSchema": SCHEMA}, FakeClient(server()))
    denied_job = ExecutionContext("job", tmp_path)
    denied = prepare_request("hello", "agent", None, None, "", None, None, denied_job,
                             mcp_tools={tool.name: tool})
    assert tool.name not in denied.allowed_tools
    assert eligible_mcp_config(MCPConfig((server(),)), denied_job, (), None).servers == ()
    allowed_job = ExecutionContext("job", tmp_path, allowed_tools=frozenset({tool.name}))
    allowed = prepare_request("hello", "agent", None, None, "", None, None, allowed_job,
                              mcp_tools={tool.name: tool})
    assert tool.name not in allowed.allowed_tools
    assert eligible_mcp_config(MCPConfig((server(),)), allowed_job, (), None).servers == ()


def test_active_native_name_cannot_be_silently_replaced_by_mcp(monkeypatch):
    tool = adapt_tool("one", {"name": "echo", "inputSchema": SCHEMA}, FakeClient(server()))
    monkeypatch.setattr("ai.execution.request._allowed_tools", lambda *args: frozenset({tool.name}))
    with pytest.raises(ValueError, match="collides with a native tool"):
        prepare_request("hello", "agent", None, None, "", None, None, None,
                        mcp_tools={tool.name: tool})


async def test_local_stdio_server_connects_calls_and_exits_cleanly(tmp_path, monkeypatch, capsys):
    fixture = Path(__file__).with_name("fixture_server.py")
    pidfile = tmp_path / "server.pid"
    monkeypatch.setenv("SUTO_MCP_TEST_PIDFILE", str(pidfile))
    monkeypatch.setenv("SUTO_MCP_TEST_SECRET", "test-private-token")
    config = MCPConfig((MCPServerConfig(
        "local", sys.executable, (str(fixture),),
        env={
            "SUTO_MCP_TEST_PIDFILE": "SUTO_MCP_TEST_PIDFILE",
            "SUTO_MCP_TEST_SECRET": "SUTO_MCP_TEST_SECRET",
        },
        allow_tools=frozenset({"echo"}),
    ),))
    async with MCPManager(config) as manager:
        assert manager.failures == {}
        assert set(manager.allowed_tools()) == {"mcp.local.echo"}
        result = await manager.tools["mcp.local.echo"].execute({"message": "hello"})
        assert result.ok and result.content == "HELLO"
    assert manager.tools == {}
    captured = capsys.readouterr()
    assert "test-private-token" not in captured.out + captured.err
    if sys.platform == "linux":
        assert not Path(f"/proc/{pidfile.read_text()}").exists()


async def test_failed_discovery_closes_local_stdio_process(tmp_path, monkeypatch):
    fixture = Path(__file__).with_name("fixture_server.py")
    pidfile = tmp_path / "bad-server.pid"
    monkeypatch.setenv("SUTO_MCP_TEST_PIDFILE", str(pidfile))
    monkeypatch.setenv("SUTO_MCP_TEST_BAD_SCHEMA", "1")
    config = MCPConfig((MCPServerConfig(
        "bad", sys.executable, (str(fixture),),
        env={
            "SUTO_MCP_TEST_PIDFILE": "SUTO_MCP_TEST_PIDFILE",
            "SUTO_MCP_TEST_BAD_SCHEMA": "SUTO_MCP_TEST_BAD_SCHEMA",
        },
        allow_tools=frozenset({"echo"}),
    ),))
    async with MCPManager(config) as manager:
        assert manager.failures == {"bad": "ValueError"}
        assert manager.tools == {}
    if sys.platform == "linux":
        assert not Path(f"/proc/{pidfile.read_text()}").exists()


async def test_malformed_server_output_does_not_log_environment_secret(
    tmp_path, monkeypatch, caplog, capsys
):
    fixture = Path(__file__).with_name("fixture_server.py")
    secret = "test-private-output-token"
    monkeypatch.setenv("SUTO_MCP_TEST_BAD_STDOUT", secret)
    config = MCPConfig((MCPServerConfig(
        "bad_output", sys.executable, (str(fixture),),
        env={"SUTO_MCP_TEST_BAD_STDOUT": "SUTO_MCP_TEST_BAD_STDOUT"},
        allow_tools=frozenset({"echo"}),
    ),))
    async with MCPManager(config) as manager:
        assert "mcp.bad_output.echo" in manager.tools
    output = capsys.readouterr()
    assert secret not in caplog.text + output.out + output.err


async def test_execute_local_ai_uses_configured_mcp_tool_through_normal_loop(monkeypatch):
    config = MCPConfig((server(),))
    clients = []
    def factory(item):
        client = FakeClient(item)
        clients.append(client)
        return client
    monkeypatch.setattr("ai.executor.load_mcp_config", lambda: config)
    monkeypatch.setattr("ai.executor.MCPManager", lambda selected: MCPManager(selected, factory))
    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(ai.response, "detect_language_code", lambda text: "en")
    patch_model_chat(monkeypatch)
    rounds = []
    audit_events = []
    progress_updates = []
    private_argument = "test-private-argument"
    async def fake_chat(session, messages, schemas, think=False):
        rounds.append((messages, schemas))
        if len(rounds) == 1:
            return {"message": {"tool_calls": [{"function": {
                "name": "mcp.one.echo", "arguments": {"message": private_argument},
            }}]}}
        return {"message": {"content": "Done."}}
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    result = await ai.execute_local_ai(
        "echo", reply_language=ReplyLanguage("en", "English", "test"),
        tool_event_callback=audit_events.append,
        progress_callback=progress_updates.append,
    )
    assert result.status == "completed"
    assert clients[0].calls == [("echo", {"message": private_argument})]
    assert clients[0].closed
    assert "mcp.one.echo" in {item["function"]["name"] for item in rounds[0][1]}
    assert rounds[1][0][-1]["content"] == private_argument.upper()
    assert private_argument not in str(audit_events) + str(progress_updates)


async def test_skill_restriction_blocks_mcp_call_even_if_model_requests_it(monkeypatch):
    config = MCPConfig((server(),))
    clients = []
    def factory(item):
        client = FakeClient(item)
        clients.append(client)
        return client
    skills = SkillRegistry()
    skills.register(Skill("no-mcp", "No MCP", "Use native tools", allowed_tools=()))
    monkeypatch.setattr("ai.executor.load_mcp_config", lambda: config)
    monkeypatch.setattr("ai.executor.MCPManager", lambda selected: MCPManager(selected, factory))
    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    patch_model_chat(monkeypatch)
    async def fake_chat(session, messages, schemas, think=False):
        assert "mcp.one.echo" not in {item["function"]["name"] for item in schemas}
        return {"message": {"tool_calls": [{"function": {
            "name": "mcp.one.echo", "arguments": {"message": "hello"},
        }}]}}
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    result = await ai.execute_local_ai("echo", active_skills=("no-mcp",), skill_registry=skills,
                                       reply_language=ReplyLanguage("en", "English", "test"))
    assert result.status == "blocked"
    assert clients == []


async def test_cancelled_ai_request_closes_mcp_connection(monkeypatch):
    config = MCPConfig((server(),))
    clients = []
    started = asyncio.Event()

    def factory(item):
        client = FakeClient(item)
        clients.append(client)
        return client

    monkeypatch.setattr("ai.executor.load_mcp_config", lambda: config)
    monkeypatch.setattr("ai.executor.MCPManager", lambda selected: MCPManager(selected, factory))
    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    patch_model_chat(monkeypatch)

    async def pending_chat(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ai.client, "chat", pending_chat)
    task = asyncio.create_task(ai.execute_local_ai(
        "wait", reply_language=ReplyLanguage("en", "English", "test")
    ))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(clients) == 1 and clients[0].closed


async def test_invalid_mcp_config_fails_request_without_calling_model(monkeypatch):
    monkeypatch.setattr("ai.executor.load_mcp_config", lambda: parse_mcp_config({"bad": "value"}))
    async def unexpected_chat(*args, **kwargs):
        raise AssertionError("model must not run")
    monkeypatch.setattr(ai.client, "chat", unexpected_chat)
    result = await ai.execute_local_ai("hello", reply_language=ReplyLanguage("en", "English", "test"))
    assert result.status == "failed"
    assert result.error == "invalid MCP configuration"
