import pytest

from modes import MODE_POLICIES, get_mode_policy
from tools import get_tools


def test_chat_mode_has_current_tools():
    policy = get_mode_policy("chat")

    assert "search_web" in policy.allowed_tools
    assert "fetch_url" in policy.allowed_tools
    assert "get_current_datetime" in policy.allowed_tools
    assert "read_attached_file" in policy.allowed_tools


def test_agent_mode_has_no_tools():
    policy = get_mode_policy("agent")

    assert policy.allowed_tools == frozenset()

    tools, schemas, guidance = get_tools(policy.allowed_tools)
    assert tools == {}
    assert schemas == []
    assert guidance == ""


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown mode"):
        get_mode_policy("secret")


def test_all_modes_reference_registered_tools():
    for policy in MODE_POLICIES.values():
        get_tools(policy.allowed_tools)
