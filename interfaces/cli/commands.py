"""Public CLI command dispatch, isolated from session lifecycle concerns."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import tomllib
from typing import Any

from interfaces.cli import CLI_STORAGE_INTERFACE
from interfaces.cli.operations import print_pending_reminders
from interfaces.cli.output import write as print
from interfaces.cli.reminder_input import parse_reminder_input
from skills import SkillSelection


@dataclass(frozen=True)
class CommandContext:
    store: Any
    user: Any
    conversation_id: str
    mode: str
    skills: SkillSelection | None = None


@dataclass(frozen=True)
class CommandOutcome:
    handled: bool
    exit_requested: bool = False
    conversation_id: str | None = None
    reset_language: bool = False
    request_prompt: str | None = None
    request_skill: str | None = None


CommandHandler = Callable[[CommandContext, str], CommandOutcome]
PROJECT_FILE = Path(__file__).resolve().parents[2] / "pyproject.toml"


def print_help(mode: str | None = None) -> None:
    print("Commands:")
    print("  /help  show available commands")
    print("  /version  show Suto's version")
    print("  /clear  clear this chat context")
    print("  /reset all  delete all saved conversations")
    print("  /reminder [time and title]  list or create reminders")
    print("  /reminder remove <name-or-id>  delete a pending reminder")
    print("  /task [title]  list or create personal tasks")
    print("  /task remove <name-or-id>  delete an open task")
    print("  /jobs  show recent automation jobs (read-only)")
    print("  /skills  list available and active skills")
    print("  /skill activate <name>  activate a skill for this CLI session")
    print("  /skill deactivate <name>  deactivate a skill")
    print("  /<skill-name> <message>  use a skill for one message")
    print("  /exit  exit suto")


def _help(context: CommandContext, argument: str) -> CommandOutcome:
    if argument:
        print("usage: /help")
        return CommandOutcome(handled=True)
    print_help(context.mode)
    return CommandOutcome(handled=True)


def _version(context: CommandContext, argument: str) -> CommandOutcome:
    if argument:
        print("usage: /version")
        return CommandOutcome(handled=True)
    try:
        with PROJECT_FILE.open("rb") as project_file:
            version = tomllib.load(project_file)["project"]["version"]
        if not isinstance(version, str) or not version:
            raise ValueError("invalid project version")
    except (OSError, ValueError, KeyError, TypeError):
        print("Suto version unavailable.")
    else:
        print(f"Suto {version}")
    return CommandOutcome(handled=True)


def _exit(context: CommandContext, argument: str) -> CommandOutcome:
    print("bye")
    return CommandOutcome(handled=True, exit_requested=True)


def _jobs(context: CommandContext, argument: str) -> CommandOutcome:
    if argument:
        print("usage: /jobs")
        return CommandOutcome(handled=True)
    jobs = context.store.list_jobs()
    if not jobs:
        print("No automation jobs.")
        return CommandOutcome(handled=True)
    print("Recent automation jobs (newest first):")
    for job in jobs:
        prompt = " ".join(job.prompt.split())
        if len(prompt) > 60:
            prompt = prompt[:57] + "..."
        print(f"{job.id}  {job.status.value}  {prompt}")
    return CommandOutcome(handled=True)


def _task(context: CommandContext, argument: str) -> CommandOutcome:
    if not argument:
        tasks = context.store.list_tasks(context.user.id)
        if not tasks:
            print("No open tasks.")
        else:
            print("Open tasks:")
            for task in tasks:
                print(f"{task.id}  {task.title}")
        return CommandOutcome(handled=True)
    if argument.casefold() == "remove":
        print("usage: /task remove <name-or-id>")
        return CommandOutcome(handled=True)
    if argument.casefold().startswith("remove "):
        reference = argument[7:].strip()
        try:
            task, matches = context.store.remove_task(context.user.id, reference)
        except sqlite3.Error:
            print("ลบงานไม่สำเร็จ กรุณาลองอีกครั้ง")
            return CommandOutcome(handled=True)
        if len(matches) > 1:
            print(f'Multiple open tasks are named "{reference}":')
            for match in matches:
                print(f"{match.id}  {match.title}")
        elif task is None:
            print(f"Open task not found: {reference}")
        else:
            print(f"Task deleted: {task.title}")
        return CommandOutcome(handled=True)
    try:
        task = context.store.create_task(context.user.id, argument)
    except ValueError as error:
        print(str(error))
    except sqlite3.Error:
        print("สร้างงานไม่สำเร็จ กรุณาลองอีกครั้ง")
    else:
        print(f"Task created: {task.id}  {task.title}")
    return CommandOutcome(handled=True)


def _reminder(context: CommandContext, argument: str) -> CommandOutcome:
    if not argument:
        print_pending_reminders(context.store, context.user.id)
        return CommandOutcome(handled=True)
    if argument.casefold() == "remove":
        print("usage: /reminder remove <name-or-id>")
        return CommandOutcome(handled=True)
    if argument.casefold().startswith("remove "):
        reference = argument[7:].strip()
        try:
            reminder, matches = context.store.remove_reminder(context.user.id, reference)
        except sqlite3.Error:
            print("ลบการแจ้งเตือนไม่สำเร็จ กรุณาลองอีกครั้ง")
            return CommandOutcome(handled=True)
        if len(matches) > 1:
            print(f'Multiple pending reminders are named "{reference}":')
            for match in matches:
                print(f"{match.id}  {match.remind_at}  {match.title}")
        elif reminder is None:
            print(f"Pending reminder not found: {reference}")
        else:
            print(f"Reminder deleted: {reminder.title}")
        return CommandOutcome(handled=True)
    try:
        request = parse_reminder_input(argument)
        if request.minutes is not None:
            reminder = context.store.create_relative_reminder(
                context.user.id, request.title, request.minutes,
                timezone=context.user.timezone,
            )
        else:
            reminder = context.store.create_clock_reminder(
                context.user.id, request.title, request.clock_time,
                timezone=context.user.timezone,
            )
    except ValueError as error:
        print(str(error))
    except sqlite3.Error:
        print("สร้างการแจ้งเตือนไม่สำเร็จ กรุณาลองอีกครั้ง")
    else:
        print(f"Reminder created: {reminder.id}  {reminder.remind_at}  {reminder.title}")
    return CommandOutcome(handled=True)


def _clear(context: CommandContext, argument: str) -> CommandOutcome:
    if argument:
        print("usage: /clear")
        return CommandOutcome(handled=True)
    context.store.clear_conversation(context.conversation_id)
    print("Chat context cleared.")
    return CommandOutcome(handled=True, reset_language=True)


def _reset(context: CommandContext, argument: str) -> CommandOutcome:
    if argument.casefold() != "all":
        print("usage: /reset all")
        return CommandOutcome(handled=True)
    count = context.store.reset_conversations()
    conversation = context.store.get_or_create_conversation(
        context.user.id,
        CLI_STORAGE_INTERFACE,
        "local",
    )
    print(f"All saved conversations deleted ({count}).")
    return CommandOutcome(
        handled=True,
        conversation_id=conversation.id,
        reset_language=True,
    )


def _skills(context: CommandContext, argument: str) -> CommandOutcome:
    if argument or context.skills is None:
        print("usage: /skills")
        return CommandOutcome(handled=True)
    for skill in context.skills.registry.list_skills():
        marker = "active" if skill.name in context.skills.names else "available"
        print(f"{skill.name} ({marker})  {skill.description}")
    return CommandOutcome(handled=True)


def _skill(context: CommandContext, argument: str) -> CommandOutcome:
    action, _, name = argument.partition(" ")
    if (
        context.skills is None or action not in {"activate", "deactivate"}
        or not name.strip() or " " in name.strip()
    ):
        print("usage: /skill activate|deactivate <name>")
        return CommandOutcome(handled=True)
    name = name.strip()
    try:
        if action == "activate":
            context.skills.activate(name)
        else:
            context.skills.deactivate(name)
    except ValueError as error:
        print(str(error))
    else:
        print(f"Skill {name} {action}d.")
    return CommandOutcome(handled=True)


COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "/help": _help,
    "/version": _version,
    "/exit": _exit,
    "/reminder": _reminder,
    "/task": _task,
    "/jobs": _jobs,
    "/clear": _clear,
    "/reset": _reset,
    "/skills": _skills,
    "/skill": _skill,
}


def handle_command(context: CommandContext, prompt: str) -> CommandOutcome:
    """Dispatch one slash command without coupling it to the input loop."""
    if not prompt.startswith("/"):
        return CommandOutcome(handled=False)
    command, _, argument = prompt.partition(" ")
    command = command.casefold()
    handler = COMMAND_HANDLERS.get(command)
    if handler is None:
        name = command.removeprefix("/")
        if context.skills is not None:
            try:
                context.skills.registry.resolve(name)
            except ValueError:
                pass
            else:
                if not argument.strip():
                    print(f"usage: /{name} <message>")
                    return CommandOutcome(handled=True)
                return CommandOutcome(
                    handled=False,
                    request_prompt=argument.strip(),
                    request_skill=name,
                )
        print(f"Unknown command: {command}. Type /help for commands.")
        return CommandOutcome(handled=True)
    return handler(context, argument.strip())
