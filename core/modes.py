from dataclasses import dataclass


DEFAULT_MODE = "chat"
PUBLIC_MODES = ("chat", "agent")

ASSISTANT_TOOLS = frozenset(
    {
        "get_current_datetime",
        "search_web",
        "fetch_url",
        "read_attached_file",
        "search_attachment",
        "summarize_attachment",
    }
)


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
        allowed_tools=ASSISTANT_TOOLS,
    ),
    "agent": ModePolicy(
        name="agent",
        description="Personal assistant for carrying out multi-step tasks",
        prompt=(
            "You are in personal assistant mode. Help the user complete tasks "
            "proactively with the tools currently available. Ask for confirmation "
            "before consequential actions and never claim to have taken an "
            "action when the required tool is unavailable."
        ),
        allowed_tools=ASSISTANT_TOOLS,
    ),
    "developer": ModePolicy(
        name="developer",
        description="Optional coding capability with restricted workspace access",
        prompt=(
            "You are in developer capability mode. Restricted workspace tools may "
            "be available for a job. You may modify files only when that job "
            "explicitly grants write permission, and run verification commands "
            "only when command permission is granted. Exact writes and commands "
            "also pause for user approval before execution. Never claim to have "
            "taken an unavailable action."
        ),
        allowed_tools=frozenset(
            {
                "list_workspace_files",
                "read_workspace_file",
                "search_workspace",
                "apply_workspace_patch",
                "create_plan",
                "update_step",
                "revise_plan",
                "run_workspace_command",
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
