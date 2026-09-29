import pytest

from agent import AgentRequest, AgentRuntime
from ai.execution.request import prepare_request
from application.language import ReplyLanguage
from context import ContextManager
from llm.types import ModelResponse, ToolCall
from permissions import PermissionEngine, PermissionPolicy
from skills import Skill, SkillLoadError, SkillRegistry, SkillSelection, builtin_registry, load_skill
from tools.registry import FunctionTool, ToolRegistry


def _file(tmp_path, frontmatter, body="Use evidence."):
    directory = tmp_path / "example"
    directory.mkdir(exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")
    return path


def _skill(name="example", **kwargs):
    return Skill(name, "Example skill", "Use evidence.", **kwargs)


def test_load_valid_skill_with_metadata_and_configuration(tmp_path):
    path = _file(tmp_path, """name: example
description: Explain findings.
recommended_tools: [search_web]
allowed_tools: [search_web]
configuration:
  depth: 2
metadata:
  owner: local""")
    skill = load_skill(path)
    assert skill.name == "example"
    assert skill.instructions == "Use evidence."
    assert skill.recommended_tools == ("search_web",)
    assert skill.allowed_tools == ("search_web",)
    assert skill.configuration == {"depth": 2}
    assert skill.metadata == {"owner": "local"}


@pytest.mark.parametrize("metadata", [
    "name: Bad Name\ndescription: x",
    "name: example\ndescription: x\nallowed_tools: search_web",
    "name: example\ndescription: x\nrecommended_tools: [search_web, search_web]",
    "name: example\ndescription: x\nunknown: value",
    "name: example\ndescription: x\nname: duplicate",
    "name: example\ndescription: x\nconfiguration: [invalid]",
])
def test_invalid_skill_metadata(tmp_path, metadata):
    with pytest.raises(SkillLoadError):
        load_skill(_file(tmp_path, metadata))


@pytest.mark.parametrize("content", [
    "name: example\nno frontmatter",
    "---\nname: [\n---\nbody",
    "---\nname: example\ndescription: x\n---\n",
])
def test_malformed_skill_file(tmp_path, content):
    path = tmp_path / "SKILL.md"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SkillLoadError):
        load_skill(path)


def test_discovery_is_sorted_and_duplicate_registration_fails(tmp_path):
    for name in ("beta", "alpha"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}\n---\nInstructions.\n",
            encoding="utf-8",
        )
    registry = SkillRegistry()
    assert [skill.name for skill in registry.discover(tmp_path)] == ["alpha", "beta"]
    assert [skill.name for skill in registry.list_skills()] == ["alpha", "beta"]
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_skill("alpha"))
    with pytest.raises(ValueError, match="already registered"):
        registry.discover(tmp_path)
    assert len(registry.list_skills()) == 2


def test_activation_deactivation_multiple_and_unknown():
    registry = SkillRegistry()
    registry.register(_skill("first"))
    registry.register(_skill("second"))
    session = SkillSelection(registry)
    other = SkillSelection(registry)
    session.activate("first")
    session.activate("second")
    session.activate("first")
    assert session.names == ("first", "second")
    assert [item.name for item in registry.active(session.names)] == ["first", "second"]
    assert other.names == ()
    session.deactivate("first")
    assert session.names == ("second",)
    with pytest.raises(ValueError, match="unknown skill"):
        session.activate("missing")
    with pytest.raises(ValueError, match="not active"):
        session.deactivate("first")


def test_context_receives_multiple_skills_recommendations_and_configuration():
    skills = (
        _skill("first", recommended_tools=("search_web", "missing"), configuration={"depth": 2}),
        _skill("second"),
    )
    messages = ContextManager().build(
        "System.", "Question", [], active_skills=skills,
        available_tools=frozenset({"search_web"}),
    )
    system = messages[0]["content"]
    assert "Skill first:\nUse evidence." in system
    assert "Skill second:\nUse evidence." in system
    assert "Recommended available tools: search_web" in system
    assert "missing" not in system
    assert '"depth": 2' in system


def test_request_preparation_intersects_allowed_tools_and_does_not_grant_new_tools():
    registry = SkillRegistry()
    registry.register(_skill("limited", allowed_tools=("search_web", "file_write"), recommended_tools=("file_write", "search_web")))
    prepared = prepare_request(
        "Find evidence", "agent", None, ReplyLanguage("en", "English", "test"),
        "", [], None, None, active_skills=("limited",), skill_registry=registry,
    )
    assert prepared.allowed_tools == frozenset({"search_web"})
    assert [item["function"]["name"] for item in prepared.tool_schemas] == ["search_web"]
    assert "Recommended available tools: search_web" in prepared.messages[0]["content"]
    assert "Recommended available tools: file_write" not in prepared.messages[0]["content"]
    with pytest.raises(ValueError, match="unknown skill"):
        prepare_request("x", "agent", None, None, "", [], None, None,
                        active_skills=("missing",), skill_registry=registry)


def test_multiple_skill_allow_lists_intersect():
    registry = SkillRegistry()
    registry.register(_skill("first", allowed_tools=("search_web", "fetch_url")))
    registry.register(_skill("second", allowed_tools=("fetch_url",)))
    prepared = prepare_request("x", "agent", None, None, "", [], None, None,
                               active_skills=("first", "second"), skill_registry=registry)
    assert prepared.allowed_tools == frozenset({"fetch_url"})
    assert [item["function"]["name"] for item in prepared.tool_schemas] == ["fetch_url"]


def test_empty_allow_list_and_legacy_automation_instructions():
    registry = SkillRegistry()
    registry.register(_skill("no_tools", allowed_tools=()))
    prepared = prepare_request(
        "x", "agent", None, None, "Pinned automation instructions.", [], None, None,
        active_skills=("no_tools",), skill_registry=registry,
    )
    assert prepared.allowed_tools == frozenset()
    assert prepared.tool_schemas == []
    assert "Pinned automation instructions." in prepared.messages[0]["content"]


async def test_skill_cannot_execute_or_bypass_registry_and_permission():
    skill = _skill("limited", recommended_tools=("test.echo",), allowed_tools=("test.echo",))
    registry = ToolRegistry()
    calls = []
    registry.register(FunctionTool("test.echo", "Echo", {"type": "object"},
                                   lambda: calls.append("executed")))

    class FakeModel:
        async def generate(self, request):
            assert [item["function"]["name"] for item in request.available_tools] == ["test.echo"]
            return ModelResponse("", [ToolCall("test.echo", {})])

    messages = ContextManager().build("System", "Do it", [], active_skills=(skill,),
                                      available_tools=frozenset({"test.echo"}))
    runtime = AgentRuntime(FakeModel(), registry,
                           permissions=PermissionEngine(PermissionPolicy({"test.echo": "deny"})))
    result = await runtime.run(AgentRequest("Do it", active_skills=("limited",)), messages)
    assert result.status == "blocked"
    assert calls == []
    assert any(event.type == "permission.denied" for event in runtime.events)

    class UnknownToolModel:
        async def generate(self, request):
            assert request.available_tools == registry.export_model_schemas()
            return ModelResponse("", [ToolCall("not_registered", {})])

    unknown = AgentRuntime(
        UnknownToolModel(), registry,
        permissions=PermissionEngine(PermissionPolicy({"not_registered": "allow"})),
    )
    assert (await unknown.run(AgentRequest("Do it", active_skills=("limited",)), messages)).status == "blocked"
    assert calls == []


def test_builtin_skills_are_discoverable():
    assert {skill.name for skill in builtin_registry().list_skills()} == {"coding", "research"}
