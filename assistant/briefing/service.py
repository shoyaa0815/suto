import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _reference_time(now: datetime | None) -> datetime:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must include a timezone")
    return current


def _is_thai(locale: str) -> bool:
    return locale.casefold().startswith("th")


def _clock(value: datetime) -> str:
    return value.strftime("%H:%M")


def _task_line(task, zone: ZoneInfo) -> str:
    if task.due_at is None:
        return task.title
    due = _instant(task.due_at).astimezone(zone)
    return f"{task.title} — {_clock(due)}"


def build_daily_briefing(store, user_id: str, *, now: datetime | None = None) -> str:
    """Build a deterministic, token-free summary from personal tasks and reminders."""
    user = store.get_user(user_id)
    if user is None:
        raise ValueError(f"user not found: {user_id}")
    zone = ZoneInfo(user.timezone)
    current = _reference_time(now).astimezone(zone)
    today = current.date()
    tasks = store.list_tasks(user_id)
    reminders = store.list_reminders(user_id, limit=None)

    overdue = []
    due_today = []
    upcoming = []
    unscheduled = []
    for task in tasks:
        if task.due_at is None:
            unscheduled.append(task)
            continue
        due = _instant(task.due_at).astimezone(zone)
        if due < current:
            overdue.append(task)
        elif due.date() == today:
            due_today.append(task)
        else:
            upcoming.append(task)

    today_reminders = [
        item
        for item in reminders
        if _instant(item.remind_at).astimezone(zone).date() == today
    ]
    next_reminder = next(
        (
            item
            for item in reminders
            if _instant(item.remind_at).astimezone(zone) >= current
        ),
        None,
    )

    thai = _is_thai(user.locale)
    if thai:
        lines = [f"สรุปประจำวัน — {today.isoformat()}"]
        if not tasks and not reminders:
            return "\n".join(lines + ["วันนี้ยังไม่มีงานหรือการแจ้งเตือน"])
        if overdue:
            lines += ["", f"งานเลยกำหนด ({len(overdue)})"]
            lines += [f"- {_task_line(item, zone)}" for item in overdue[:5]]
        if due_today:
            lines += ["", f"งานวันนี้ ({len(due_today)})"]
            lines += [f"- {_task_line(item, zone)}" for item in due_today[:5]]
        if today_reminders:
            lines += ["", f"การแจ้งเตือนวันนี้ ({len(today_reminders)})"]
            lines += [
                f"- {_clock(_instant(item.remind_at).astimezone(zone))} {item.title}"
                for item in today_reminders[:5]
            ]
        if not overdue and not due_today and not today_reminders:
            lines += ["", "วันนี้ไม่มีรายการที่มีกำหนดเวลา"]
        if unscheduled:
            lines += ["", f"งานที่ยังไม่กำหนดเวลา: {len(unscheduled)} งาน"]
        if upcoming:
            first = upcoming[0]
            due = _instant(first.due_at).astimezone(zone)
            lines += ["", f"งานถัดไป: {first.title} — {due:%Y-%m-%d %H:%M}"]
        elif next_reminder and next_reminder not in today_reminders:
            instant = _instant(next_reminder.remind_at).astimezone(zone)
            lines += ["", f"การแจ้งเตือนถัดไป: {next_reminder.title} — {instant:%Y-%m-%d %H:%M}"]
        priority = overdue[0] if overdue else (due_today[0] if due_today else None)
        if priority:
            lines += ["", f"แนะนำให้เริ่มจาก: {priority.title}"]
        return "\n".join(lines)

    lines = [f"Daily briefing — {today.isoformat()}"]
    if not tasks and not reminders:
        return "\n".join(lines + ["No tasks or reminders yet today."])
    if overdue:
        lines += ["", f"Overdue ({len(overdue)})"]
        lines += [f"- {_task_line(item, zone)}" for item in overdue[:5]]
    if due_today:
        lines += ["", f"Due today ({len(due_today)})"]
        lines += [f"- {_task_line(item, zone)}" for item in due_today[:5]]
    if today_reminders:
        lines += ["", f"Today's reminders ({len(today_reminders)})"]
        lines += [
            f"- {_clock(_instant(item.remind_at).astimezone(zone))} {item.title}"
            for item in today_reminders[:5]
        ]
    if not overdue and not due_today and not today_reminders:
        lines += ["", "Nothing is scheduled for today."]
    if unscheduled:
        lines += ["", f"Unscheduled tasks: {len(unscheduled)}"]
    if upcoming:
        first = upcoming[0]
        due = _instant(first.due_at).astimezone(zone)
        lines += ["", f"Next task: {first.title} — {due:%Y-%m-%d %H:%M}"]
    elif next_reminder and next_reminder not in today_reminders:
        instant = _instant(next_reminder.remind_at).astimezone(zone)
        lines += ["", f"Next reminder: {next_reminder.title} — {instant:%Y-%m-%d %H:%M}"]
    priority = overdue[0] if overdue else (due_today[0] if due_today else None)
    if priority:
        lines += ["", f"Start with: {priority.title}"]
    return "\n".join(lines)


def set_daily_briefing_time(store, user_id: str, clock_time: str) -> str:
    if not isinstance(clock_time, str) or not TIME_PATTERN.fullmatch(clock_time):
        raise ValueError("briefing time must use 24-hour HH:MM format")
    store.set_user_preference(user_id, "briefing_time", clock_time)
    return clock_time


def disable_daily_briefing(store, user_id: str) -> bool:
    return store.delete_user_preference(user_id, "briefing_time")


def briefing_schedule_status(store, user_id: str) -> str | None:
    return store.user_preferences(user_id).get("briefing_time")


def briefing_is_due(store, user_id: str, *, now: datetime | None = None) -> bool:
    user = store.get_user(user_id)
    if user is None:
        return False
    clock_time = briefing_schedule_status(store, user_id)
    if not clock_time or not TIME_PATTERN.fullmatch(clock_time):
        return False
    current = _reference_time(now).astimezone(ZoneInfo(user.timezone))
    hour, minute = (int(part) for part in clock_time.split(":"))
    scheduled = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return current >= scheduled
