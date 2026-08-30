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
        description="Reserved agent mode with no action tools yet",
        prompt=(
            "You are in agent mode, which is currently a placeholder. No "
            "action tools are available yet. You may answer from the user's "
            "message, but never claim that you searched, read files, or took "
            "an action. If an action is requested, say that agent actions are "
            "not implemented yet."
        ),
        allowed_tools=frozenset(),
    ),
}


def get_mode_policy(mode: str) -> ModePolicy:
    try:
        return MODE_POLICIES[mode]
    except KeyError as exc:
        choices = ", ".join(MODE_POLICIES)
        raise ValueError(f"unknown mode {mode!r}; choose one of: {choices}") from exc
