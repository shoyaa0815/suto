import asyncio
import json
import shlex
import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from assistant.briefing import build_daily_briefing
from assistant.briefing.service import briefing_is_due
from clients.tui.output import write as print


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


async def handle_operations(store, command: str, argument: str) -> bool:
    commands = {'/health', '/diagnostics', '/metrics', '/backup', '/cleanup', '/limits',
                '/logs', '/memory', '/knowledge', '/subtasks', '/notifications'}
    if command not in commands:
        return False
    try:
        parts = shlex.split(argument)
        if command in {'/health', '/diagnostics', '/metrics'}:
            if parts:
                raise ValueError(f'usage: {command}')
            print_json(store.metrics() if command == '/metrics' else store.diagnostics())
        elif command == '/backup':
            if len(parts) != 1:
                raise ValueError('usage: /backup <new-backup.db>')
            print(f'Backup created: {store.backup(parts[0])}')
        elif command == '/cleanup':
            apply = '--apply' in parts
            parts = [part for part in parts if part != '--apply']
            if len(parts) > 1:
                raise ValueError('usage: /cleanup [days=30] [--apply] (default: preview only)')
            print_json(store.cleanup(int(parts[0]) if parts else 30, dry_run=not apply))
        elif command == '/limits':
            values = {}
            for item in parts:
                key, sep, value = item.partition('=')
                if not sep:
                    raise ValueError('usage: /limits [key=positive_integer ...]')
                values[key] = int(value)
            print_json(store.configure(**values) if values else store.settings())
        elif command == '/logs':
            if len(parts) > 1:
                raise ValueError('usage: /logs [job_id]')
            for row in store.logs(parts[0] if parts else None):
                print(json.dumps(row, ensure_ascii=False))
        elif command == '/notifications':
            if not parts:
                print_json(store.notifications())
            elif len(parts) == 2 and parts[0] == 'ack':
                print_json({'acknowledged': store.acknowledge_notification(int(parts[1]))})
            else:
                raise ValueError('usage: /notifications [ack <event_id>]')
        elif command == '/subtasks':
            if len(parts) != 1:
                raise ValueError('usage: /subtasks <parent_job_id>')
            print(store.children_summary(parts[0]) or 'No subtasks.')
        elif command == '/memory':
            if len(parts) < 2:
                raise ValueError('usage: /memory <on|off|list|add|search|delete> <workspace> [text|memory_id]')
            action, workspace, *rest = parts
            if action in {'on', 'off', 'list'} and rest:
                raise ValueError('unexpected memory arguments')
            if action in {'on', 'off'}:
                store.set_memory_enabled(workspace, action == 'on')
                print(f'Memory {action} for {workspace}')
            elif action == 'list':
                print_json(store.list_memories(workspace))
            elif action == 'add' and rest:
                print(await store.remember(workspace, ' '.join(rest)))
            elif action == 'search' and rest:
                print_json(await store.recall(workspace, ' '.join(rest)))
            elif action == 'delete' and len(rest) == 1:
                print_json({'deleted': store.forget_memory(workspace, rest[0])})
            else:
                raise ValueError('invalid memory action or arguments')
        elif command == '/knowledge':
            if len(parts) < 2:
                raise ValueError('usage: /knowledge <index|search|clear> <workspace> [query]')
            action, workspace, *rest = parts
            if action == 'index' and not rest:
                print_json(await asyncio.to_thread(store.index_workspace, workspace))
            elif action == 'search' and rest:
                print_json(store.search_knowledge(workspace, ' '.join(rest)))
            elif action == 'clear' and not rest:
                store.clear_knowledge(workspace)
                print('Project index cleared.')
            else:
                raise ValueError('invalid knowledge action or arguments')
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        from automation.storage.redaction import redact_text
        print(redact_text(error))
    return True


async def notify_tui(store):
    """Opt-in local delivery; acknowledgement follows printing (at least once)."""
    while True:
        for event in reversed(store.notifications()):
            print(f"\n[suto notification #{event['id']}] {event['job_id']}: {event['status']}", flush=True)
            store.acknowledge_notification(event['id'])
        await asyncio.sleep(1)


def _reminder_time(reminder) -> str:
    instant = datetime.fromisoformat(reminder.remind_at)
    try:
        instant = instant.astimezone(ZoneInfo(reminder.timezone))
    except ZoneInfoNotFoundError:
        pass
    return instant.strftime("%Y-%m-%d %H:%M %Z")


def print_due_reminders(store, user_id: str, *, overdue: bool) -> int:
    reminders = store.claim_due_reminders(user_id)
    for reminder in reminders:
        scheduled = _reminder_time(reminder)
        if overdue:
            print(
                f"\n[suto reminder — เลยเวลาแล้ว] {reminder.title}\n"
                f"ตั้งไว้เวลา {scheduled}",
                flush=True,
            )
        else:
            print(
                f"\n[suto reminder] {reminder.title}\n"
                f"ถึงเวลาแล้ว ({scheduled})",
                flush=True,
            )
    return len(reminders)


def print_pending_reminders(store, user_id: str) -> int:
    reminders = store.list_reminders(user_id, limit=None)
    if not reminders:
        print("No pending reminders.")
        return 0
    print("Pending reminders:")
    for reminder in reminders:
        print(f"{reminder.id}  {_reminder_time(reminder)}  {reminder.title}")
    return len(reminders)


async def notify_personal_reminders(store, user_id: str) -> None:
    """Deliver missed reminders on startup, then watch for newly due reminders."""
    print_due_reminders(store, user_id, overdue=True)
    while True:
        await asyncio.sleep(1)
        print_due_reminders(store, user_id, overdue=False)


def claim_due_daily_briefing(store, user_id: str, *, now=None) -> str | None:
    """Return today's scheduled briefing once, after its local delivery time."""
    if not briefing_is_due(store, user_id, now=now):
        return None
    user = store.get_user(user_id)
    current = now or datetime.now(UTC)
    local_date = current.astimezone(ZoneInfo(user.timezone)).date().isoformat()
    if not store.claim_briefing_delivery(
        user_id,
        local_date,
        delivered_at=current.isoformat(),
    ):
        return None
    return build_daily_briefing(store, user_id, now=current)


async def notify_daily_briefing(store, user_id: str) -> None:
    """Deliver the configured local-time briefing while the CLI is running."""
    while True:
        briefing = claim_due_daily_briefing(store, user_id)
        if briefing:
            print(f"\n[suto daily briefing]\n{briefing}", flush=True)
        await asyncio.sleep(30)
