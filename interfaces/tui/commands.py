"""Public TUI command dispatch, isolated from session lifecycle concerns."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from assistant.briefing import (
    briefing_schedule_status,
    build_daily_briefing,
    disable_daily_briefing,
    set_daily_briefing_time,
)
from interfaces.tui.operations import print_pending_reminders
from interfaces.tui.output import write as print


@dataclass(frozen=True)
class CommandContext:
    store: Any
    user: Any
    conversation_id: str
    mode: str


@dataclass(frozen=True)
class CommandOutcome:
    handled: bool
    exit_requested: bool = False
    conversation_id: str | None = None
    reset_language: bool = False


CommandHandler = Callable[[CommandContext, str], CommandOutcome]


def print_help(mode: str | None = None) -> None:
    print("Commands:")
    print("  /help  show available commands")
    print("  /clear  clear this chat context")
    print("  /reset all  delete all saved conversations")
    print("  /setting  open profile settings")
    print("  /noti  show reminders that have not been delivered")
    print("  /noti del <reminder_id>  remove a pending reminder")
    print("  /brief  show today's briefing")
    print("  /brief at <HH:MM>  schedule a daily briefing")
    print("  /brief status|off  show or disable the daily schedule")
    print("  /exit  exit suto")


def _help(context: CommandContext, argument: str) -> CommandOutcome:
    print_help(context.mode)
    return CommandOutcome(handled=True)


def _exit(context: CommandContext, argument: str) -> CommandOutcome:
    print("bye")
    return CommandOutcome(handled=True, exit_requested=True)


def _noti(context: CommandContext, argument: str) -> CommandOutcome:
    if not argument:
        print_pending_reminders(context.store, context.user.id)
        return CommandOutcome(handled=True)
    parts = argument.split()
    if len(parts) != 2 or parts[0].casefold() != "del":
        print("usage: /noti [del <reminder_id>]")
        return CommandOutcome(handled=True)
    reminder = context.store.cancel_reminder(context.user.id, parts[1])
    if reminder is None:
        print(f"Reminder not found: {parts[1]}")
    else:
        print(f"Reminder removed: {reminder.id}")
    return CommandOutcome(handled=True)


def _brief(context: CommandContext, argument: str) -> CommandOutcome:
    parts = argument.split()
    try:
        if not parts:
            print(f"suto> {build_daily_briefing(context.store, context.user.id)}")
        elif len(parts) == 2 and parts[0].casefold() == "at":
            clock_time = set_daily_briefing_time(
                context.store,
                context.user.id,
                parts[1],
            )
            print(
                f"Daily briefing scheduled for {clock_time} "
                f"({context.store.get_user(context.user.id).timezone})."
            )
        elif len(parts) == 1 and parts[0].casefold() == "status":
            clock_time = briefing_schedule_status(context.store, context.user.id)
            if clock_time:
                print(
                    f"Daily briefing: {clock_time} "
                    f"({context.store.get_user(context.user.id).timezone})"
                )
            else:
                print("Daily briefing is off.")
        elif len(parts) == 1 and parts[0].casefold() == "off":
            disable_daily_briefing(context.store, context.user.id)
            print("Daily briefing disabled.")
        else:
            print("usage: /brief [at <HH:MM>|status|off]")
    except ValueError as error:
        print(error)
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
        "tui",
        "local",
    )
    print(f"All saved conversations deleted ({count}).")
    return CommandOutcome(
        handled=True,
        conversation_id=conversation.id,
        reset_language=True,
    )


COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "/help": _help,
    "/exit": _exit,
    "/noti": _noti,
    "/brief": _brief,
    "/clear": _clear,
    "/reset": _reset,
}


def handle_command(context: CommandContext, prompt: str) -> CommandOutcome:
    """Dispatch one slash command without coupling it to the input loop."""
    if not prompt.startswith("/"):
        return CommandOutcome(handled=False)
    command, _, argument = prompt.partition(" ")
    command = command.casefold()
    handler = COMMAND_HANDLERS.get(command)
    if handler is None:
        print(f"Unknown command: {command}. Type /help for commands.")
        return CommandOutcome(handled=True)
    return handler(context, argument.strip())
