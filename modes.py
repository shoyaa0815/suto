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
            {"get_current_datetime", "search_web", "fetch_url"}
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
        # Document tools will be added here when the private document store is
        # implemented. Keeping this empty is safer than exposing web tools as a
        # temporary substitute.
        allowed_tools=frozenset(),
    ),
}


def get_mode_policy(mode: str) -> ModePolicy:
    try:
        return MODE_POLICIES[mode]
    except KeyError as exc:
        choices = ", ".join(MODE_POLICIES)
        raise ValueError(f"unknown mode {mode!r}; choose one of: {choices}") from exc
