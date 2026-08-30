import pytest

from modes import MODE_POLICIES, get_mode_policy
from tools import get_tools


def test_personal_mode_has_web_tools():
    policy = get_mode_policy("personal")

    assert "search_web" in policy.allowed_tools
    assert "fetch_url" in policy.allowed_tools


def test_private_mode_has_no_web_tools():
    policy = get_mode_policy("private")

    assert "search_web" not in policy.allowed_tools
    assert "fetch_url" not in policy.allowed_tools


def test_private_mode_registry_only_returns_attachment_tools():
    policy = get_mode_policy("private")

    tools, schemas, guidance = get_tools(policy.allowed_tools)

    expected = {
        "read_attached_file",
        "search_attachment",
        "summarize_attachment",
    }
    assert set(tools) == expected
    assert {schema["function"]["name"] for schema in schemas} == expected
    assert "read_attached_file" in guidance


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown mode"):
        get_mode_policy("secret")


def test_all_modes_reference_registered_tools():
    for policy in MODE_POLICIES.values():
        get_tools(policy.allowed_tools)
