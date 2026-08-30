import pytest

from harness import get_tools
from modes import MODE_POLICIES, get_mode_policy


def test_personal_mode_has_web_tools():
    policy = get_mode_policy("personal")

    assert "search_web" in policy.allowed_tools
    assert "fetch_url" in policy.allowed_tools


def test_private_mode_has_no_web_tools():
    policy = get_mode_policy("private")

    assert "search_web" not in policy.allowed_tools
    assert "fetch_url" not in policy.allowed_tools


def test_registry_only_returns_tools_allowed_by_mode():
    policy = get_mode_policy("private")

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
