from dataclasses import dataclass


DEFAULT_MODE = "chat"


@dataclass(frozen=True)
class ModePolicy:
    name: str
    description: str
    prompt: str
    allowed_tools: frozenset[str]


MODE_POLICIES = {
    "chat": ModePolicy(
        name="chat",
        description="Chat assistant with the currently available tools",
        prompt=(
            "You are in chat mode. You may use the tools made available to "
            "you when their guidance says they are needed."
        ),
        allowed_tools=frozenset(
            {
                "get_current_datetime",
                "search_web",
                "fetch_url",
                "read_attached_file",
                "search_attachment",
                "summarize_attachment",
            }
        ),
    ),
    "agent": ModePolicy(
        name="agent",
        description="Automation agent with restricted workspace access",
        prompt=(
            "You are in automation agent mode. Restricted workspace tools may "
            "be available for a job. You may modify files only when that job "
            "explicitly grants write permission. You cannot run commands. Never "
            "claim to have taken an unavailable action."
        ),
        allowed_tools=frozenset(
            {
                "list_workspace_files",
                "read_workspace_file",
                "search_workspace",
                "apply_workspace_patch",
            }
        ),
    ),
}


def get_mode_policy(mode: str) -> ModePolicy:
    try:
        return MODE_POLICIES[mode]
    except KeyError as exc:
        choices = ", ".join(MODE_POLICIES)
        raise ValueError(f"unknown mode {mode!r}; choose one of: {choices}") from exc
