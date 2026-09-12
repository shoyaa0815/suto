import pytest

from core.modes import MODE_POLICIES, get_mode_policy
from tools import get_tools


def test_chat_mode_has_current_tools():
    policy = get_mode_policy("chat")

    assert "search_web" in policy.allowed_tools
    assert "fetch_url" in policy.allowed_tools
    assert "get_current_datetime" in policy.allowed_tools
    assert "read_attached_file" in policy.allowed_tools


def test_agent_mode_has_personal_assistant_tools():
    policy = get_mode_policy("agent")

    assert policy.allowed_tools == get_mode_policy("chat").allowed_tools
    assert "search_web" in policy.allowed_tools
    assert "apply_workspace_patch" not in policy.allowed_tools


def test_developer_mode_has_restricted_workspace_tools():
    policy = get_mode_policy("developer")

    expected = {
        "list_workspace_files",
        "read_workspace_file",
        "search_workspace",
        "apply_workspace_patch",
        "create_plan",
        "update_step",
        "revise_plan",
        "run_workspace_command",
    }
    assert policy.allowed_tools == expected

    tools, schemas, guidance = get_tools(policy.allowed_tools)
    assert set(tools) == expected
    assert {schema["function"]["name"] for schema in schemas} == expected
    assert "write permission" in guidance


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown mode"):
        get_mode_policy("secret")


def test_all_modes_reference_registered_tools():
    for policy in MODE_POLICIES.values():
        get_tools(policy.allowed_tools)
