from interfaces.tui.output import set_activity, write_styled


MAX_PROGRESS_DETAIL_CHARS = 60

TOOL_ACTIVITY_NAMES = {
    "search_web": "searching the web",
    "fetch_url": "reading a web page",
    "read_attached_file": "reading an attachment",
    "search_attachment": "searching an attachment",
    "summarize_attachment": "summarizing an attachment",
    "create_task": "saving a task",
    "list_tasks": "checking tasks",
    "complete_task": "updating a task",
    "create_reminder_in": "setting a reminder",
    "create_reminder_at": "setting a reminder",
    "list_reminders": "checking reminders",
    "reschedule_reminder": "rescheduling a reminder",
    "cancel_reminder": "cancelling a reminder",
    "get_daily_briefing": "preparing the daily briefing",
    "set_daily_briefing": "scheduling the daily briefing",
    "disable_daily_briefing": "turning off the daily briefing",
}


def format_elapsed(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def format_progress(update: dict) -> str:
    activity = update["activity"]
    detail = " ".join(str(update["detail"]).split())
    if len(detail) > MAX_PROGRESS_DETAIL_CHARS:
        detail = detail[: MAX_PROGRESS_DETAIL_CHARS - 3].rstrip() + "..."

    if activity == "finished":
        return f"{detail} · {update['total_tokens']:,} tokens"

    return detail


def progress_style(update: dict) -> str:
    activity = update["activity"]
    detail = str(update["detail"]).casefold()
    if activity == "tool":
        return "#60a5fa"
    if any(status in detail for status in (": failed", ": timed out", ": blocked")):
        return "#f87171"
    if activity == "finished" and detail != "completed":
        return "#fbbf24"
    return "#4ade80"


def activity_text(update: dict) -> str | None:
    activity = update["activity"]
    if activity == "finished":
        return None
    if activity == "tool":
        tool_name = str(update["detail"]).split(" ", 1)[0]
        detail = TOOL_ACTIVITY_NAMES.get(tool_name, "processing")
        return f"suto working: {detail}…"
    if activity == "tool_done":
        return "suto thinking…"
    return "suto thinking…"


def print_progress(update: dict, prefix: str = "") -> None:
    set_activity(activity_text(update))
    # Keep the terminal focused on tool usage and the final result. Heartbeats
    # and internal model stages remain available to other interfaces.
    if update.get("heartbeat") or update["activity"] not in {
        "tool",
        "tool_done",
        "finished",
    }:
        return
    label = f"[{prefix}] " if prefix else ""
    write_styled(
        f"{label}{format_progress(update)}",
        progress_style(update),
    )
