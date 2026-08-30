from dataclasses import dataclass


DEFAULT_MODE = "personal"


@dataclass(frozen=True)
class ModePolicy:
    name: str
    description: str
    prompt: str
    allowed_tools: frozenset[str]


MODE_POLICIES = {
    "personal": ModePolicy(
        name="personal",
        description="General assistant with internet access",
        prompt=(
            "You are in the personal workspace. You may use the tools made "
            "available to you, including web tools, when their guidance says "
            "they are needed."
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
    "private": ModePolicy(
        name="private",
        description="Private workspace with no internet access",
        prompt=(
            "You are in the private workspace. Internet access is "
            "disabled. Never claim that you searched the web or accessed a "
            "private knowledge base. Answer only from the user's message and "
            "attached-file content. If the supplied information is not enough, "
            "say what information is missing."
        ),
        # Attached files stay request-scoped and never grant filesystem access.
        # Private document search can be added separately when a store exists.
        allowed_tools=frozenset(
            {
                "read_attached_file",
                "search_attachment",
                "summarize_attachment",
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
