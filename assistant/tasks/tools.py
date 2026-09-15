import json
import re
from dataclasses import asdict

from assistant.context import AssistantContext


PROMPT = """- Personal task tools are available only in agent mode.
- Use create_task for work the user wants tracked.
- For reminders like "in 10 minutes", call create_reminder_in and pass the
  number of minutes without calculating a date or time yourself.
- For reminders at a clock time like "21:30", call create_reminder_at and pass
  only that 24-hour HH:MM value. Python selects the next occurrence in the
  user's timezone. Never calculate or invent a calendar date for these tools.
- On Discord, omit channel_id for a private DM reminder. If the user says
  "this channel" or equivalent, pass the current_channel_id from the delivery
  context. If the user mentions a channel, pass that mentioned channel ID.
  Use list_delivery_channels only when a channel was specified by name without
  an ID. Channel delivery requires both the user and bot to have view and send
  permissions. Never invent a channel ID.
- due_at must be an ISO-8601 value with a timezone offset.
- Never invent task or reminder IDs. Use list_tasks or list_reminders before
  completing, rescheduling, or cancelling an item when its ID is unknown.
- When the user asks for a summary, use list_tasks and list_reminders to answer
  from the current user's saved data."""

REMINDER_CREATION_TOOL_NAMES = frozenset(
    {"create_reminder_in", "create_reminder_at"}
)


def reminder_creation_requested(prompt: str) -> bool:
    """Conservatively identify direct requests to create a reminder."""
    text = " ".join(prompt.casefold().split())
    if "เตือน" in text:
        if any(word in text for word in ("ยกเลิก", "ลบ", "รายการ", "อะไร", "ดู")):
            return False
        return bool(
            re.search(r"\d{1,2}\s*[:.]\s*\d{2}", text)
            or any(word in text for word in ("อีก", "ตอน", "โมง", "นาที", "ชั่วโมง"))
        )
    return bool(
        re.search(r"\bremind\s+me\b", text)
        or re.search(r"\b(?:create|set|add)\s+(?:a\s+)?reminder\b", text)
    )


def _schema(name, description, properties=None, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


SCHEMAS = [
    _schema(
        "create_task",
        "Create a personal task for the current user.",
        {
            "title": {"type": "string"},
            "notes": {"type": "string"},
            "due_at": {
                "type": "string",
                "description": "Optional ISO-8601 date-time with timezone offset.",
            },
        },
        ["title"],
    ),
    _schema(
        "list_tasks",
        "List the current user's tasks.",
        {"include_completed": {"type": "boolean"}},
    ),
    _schema(
        "complete_task",
        "Mark one personal task completed.",
        {"task_id": {"type": "string"}},
        ["task_id"],
    ),
    _schema(
        "create_reminder_in",
        "Create a reminder a number of minutes from now. Python calculates the timestamp.",
        {
            "title": {"type": "string"},
            "minutes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 525600,
            },
            "channel_id": {
                "type": "string",
                "description": "Optional accessible Discord channel ID. Omit for the default private destination.",
            },
        },
        ["title", "minutes"],
    ),
    _schema(
        "create_reminder_at",
        "Create a reminder at the next occurrence of a local clock time.",
        {
            "title": {"type": "string"},
            "time": {
                "type": "string",
                "description": "24-hour local time in exact HH:MM format.",
                "pattern": "^(?:[01][0-9]|2[0-3]):[0-5][0-9]$",
            },
            "channel_id": {
                "type": "string",
                "description": "Optional accessible Discord channel ID. Omit for the default private destination.",
            },
        },
        ["title", "time"],
    ),
    _schema("list_reminders", "List the current user's scheduled reminders."),
    _schema(
        "list_delivery_channels",
        "List Discord text channels accessible to both the current user and bot. Optionally filter by name.",
        {"query": {"type": "string"}},
    ),
    _schema(
        "reschedule_reminder",
        "Move a reminder to a new time.",
        {
            "reminder_id": {"type": "string"},
            "remind_at": {"type": "string"},
        },
        ["reminder_id", "remind_at"],
    ),
    _schema(
        "cancel_reminder",
        "Cancel a scheduled reminder.",
        {"reminder_id": {"type": "string"}},
        ["reminder_id"],
    ),
]

TOOL_NAMES = frozenset(schema["function"]["name"] for schema in SCHEMAS)


def _json(value) -> str:
    if isinstance(value, list):
        value = [asdict(item) for item in value]
    elif value is not None:
        value = asdict(value)
    return json.dumps(value, ensure_ascii=False)


def build_task_tools(context: AssistantContext) -> dict:
    store = context.store
    user_id = context.user_id

    def delivery_target_id(channel_id: str | None = None) -> str | None:
        target = context.default_delivery_target
        if channel_id is not None:
            target = next(
                (
                    item
                    for item in context.available_delivery_targets
                    if item.destination_id == str(channel_id)
                    and item.destination_type == "guild_channel"
                ),
                None,
            )
            if target is None:
                raise ValueError("Discord channel is unavailable or not permitted")
        if target is None:
            return None
        return store.get_or_create_delivery_target(
            user_id,
            target.platform,
            target.destination_id,
            target.destination_type,
            target.display_name,
            guild_id=target.guild_id,
            requester_id=target.requester_id,
        ).id

    def create_task(title: str, notes: str = "", due_at: str | None = None):
        return _json(store.create_task(user_id, title, notes=notes, due_at=due_at))

    def list_tasks(include_completed: bool = False):
        return _json(store.list_tasks(user_id, include_completed=include_completed))

    def complete_task(task_id: str):
        task = store.complete_task(user_id, task_id)
        return _json(task) if task else f"task not found or already completed: {task_id}"

    def create_reminder_in(
        title: str,
        minutes: int,
        channel_id: str | None = None,
    ):
        user = store.get_user(user_id)
        return _json(
            store.create_relative_reminder(
                user_id,
                title,
                minutes,
                timezone=user.timezone,
                delivery_target_id=delivery_target_id(channel_id),
            )
        )

    def create_reminder_at(
        title: str,
        time: str,
        channel_id: str | None = None,
    ):
        user = store.get_user(user_id)
        return _json(
            store.create_clock_reminder(
                user_id,
                title,
                time,
                timezone=user.timezone,
                delivery_target_id=delivery_target_id(channel_id),
            )
        )

    def list_reminders():
        return _json(store.list_reminders(user_id))

    def list_delivery_channels(query: str = ""):
        needle = query.strip().casefold()
        channels = [
            {
                "id": item.destination_id,
                "name": item.display_name,
                "guild_id": item.guild_id,
            }
            for item in context.available_delivery_targets
            if item.destination_type == "guild_channel"
            and (not needle or needle in item.display_name.casefold())
        ]
        return json.dumps(channels, ensure_ascii=False)

    def reschedule_reminder(reminder_id: str, remind_at: str):
        reminder = store.reschedule_reminder(user_id, reminder_id, remind_at)
        return _json(reminder) if reminder else f"reminder not found: {reminder_id}"

    def cancel_reminder(reminder_id: str):
        reminder = store.cancel_reminder(user_id, reminder_id)
        return _json(reminder) if reminder else f"reminder not found or inactive: {reminder_id}"

    return {
        name: handler
        for name, handler in locals().copy().items()
        if name in TOOL_NAMES
    }
