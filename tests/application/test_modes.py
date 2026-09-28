import pytest

from application.modes import DEFAULT_MODE, MODE_POLICIES, PUBLIC_MODES, get_mode_policy
from tools import get_tools


def test_agent_is_the_only_mode_and_uses_registered_tools():
    assert DEFAULT_MODE == "agent"
    assert PUBLIC_MODES == ("agent",)
    assert set(MODE_POLICIES) == {"agent"}

    policy = get_mode_policy("agent")
    get_tools(policy.allowed_tools)
    assert {"search_web", "fetch_url", "create_task", "memory.save"} <= policy.allowed_tools
    assert not policy.allowed_tools.intersection({
        "list_workspace_files", "apply_workspace_patch", "run_workspace_command",
    })
    assert not policy.allowed_tools.intersection({
        "terminal.run", "terminal_run", "file.write", "file_write",
        "git.commit", "git_commit", "python.run", "python_run",
    })


@pytest.mark.parametrize("mode", ["chat", "home", "developer", "secret"])
def test_removed_modes_are_rejected(mode):
    with pytest.raises(ValueError, match="unknown mode"):
        get_mode_policy(mode)
