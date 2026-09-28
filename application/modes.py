from dataclasses import dataclass

DEFAULT_MODE = "agent"
PUBLIC_MODES = ("agent",)

# Parked until clarification state can be persisted and resumed across
# interfaces. The implementation remains available internally for that work.
CLARIFICATIONS_ENABLED = False

ASSISTANT_TOOLS = frozenset(
    {
        "get_current_datetime",
        "search_web",
        "fetch_url",
        "read_attached_file",
        "search_attachment",
        "summarize_attachment",
    }
) | (frozenset({"ask_user"}) if CLARIFICATIONS_ENABLED else frozenset())

PERSONAL_TASK_TOOLS = frozenset(
    {
        "create_task",
        "list_tasks",
        "complete_task",
        "create_reminder_in",
        "create_reminder_at",
        "create_reminders_at",
        "list_reminders",
        "reschedule_reminder",
        "cancel_reminder",
        "list_delivery_channels",
    }
)

ASSISTANT_MEMORY_TOOLS = frozenset(
    {
        "save_memory",
        "search_memory",
        "list_memories",
        "delete_memory",
        "memory.save",
        "memory.search",
        "memory.list",
        "memory.delete",
    }
)

@dataclass(frozen=True)
class ModePolicy:
    name: str
    description: str
    prompt: str
    allowed_tools: frozenset[str]


MODE_POLICIES = {
    "agent": ModePolicy(
        name="agent",
        description="Personal assistant with request-scoped tools",
        prompt=(
            "You are Suto, a personal assistant. Help the user complete tasks "
            "with the tools currently available. Ask for confirmation before "
            "consequential actions. Never claim to have taken an action when "
            "the required tool is unavailable or failed. Workspace tools are "
            "available only within an authorized job."
        ),
        allowed_tools=(
            ASSISTANT_TOOLS
            | PERSONAL_TASK_TOOLS
            | ASSISTANT_MEMORY_TOOLS
            | frozenset({"research"})
        ),
    ),
}


def get_mode_policy(mode: str) -> ModePolicy:
    try:
        return MODE_POLICIES[mode]
    except KeyError as exc:
        choices = ", ".join(MODE_POLICIES)
        raise ValueError(f"unknown mode {mode!r}; choose one of: {choices}") from exc
