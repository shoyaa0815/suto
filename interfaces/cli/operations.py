import asyncio
import json
import shlex
import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from interfaces.cli.output import write as print


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


async def handle_operations(store, command: str, argument: str) -> bool:
    commands = {'/health', '/diagnostics', '/metrics', '/backup', '/cleanup', '/limits',
                '/logs', '/knowledge', '/subtasks', '/notifications'}
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
        from workflows.storage.redaction import redact_text
        print(redact_text(error))
    return True


_NOTIFICATION_SUMMARIES = {
    'completed': 'Job completed.',
    'failed': 'Job failed.',
    'cancelled': 'Job cancelled.',
    'approval_required': 'Approval required. Use /approvals to review.',
    'retry_exhausted': 'Job failed after retry allowance was exhausted.',
}


async def notify_cli(store, *, after_id: int):
    """Deliver unread notifications after the cursor in ID order."""
    pending_ack = None
    while True:
        try:
            if pending_ack is not None:
                await asyncio.to_thread(store.acknowledge_notification, pending_ack)
                pending_ack = None
            events = await asyncio.to_thread(store.notifications_after, after_id)
            for event in events:
                summary = _NOTIFICATION_SUMMARIES.get(event['kind'])
                if summary is not None:
                    print(
                        f"\n[suto notification #{event['id']}] "
                        f"{event['job_id']}: {summary}",
                        flush=True,
                    )
                after_id = event['id']
                if summary is not None:
                    # Keep the cursor ahead of the acknowledgement so a retry
                    # cannot print the same notification again.
                    pending_ack = after_id
                    await asyncio.to_thread(store.acknowledge_notification, pending_ack)
                    pending_ack = None
        except (OSError, sqlite3.Error):
            # Delivery is optional; storage/worker transitions do not depend on it.
            await asyncio.sleep(1)
            continue
        if not events:
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
