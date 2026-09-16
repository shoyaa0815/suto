"""Public CLI command dispatch, isolated from session lifecycle concerns."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from interfaces.cli import CLI_STORAGE_INTERFACE
from interfaces.cli.operations import print_pending_reminders
from interfaces.cli.output import write as print


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
    print("  /notification  show reminders that have not been delivered")
    print("  /notification remove <name>  remove a pending reminder by name")
    print("  /exit  exit suto")


def _help(context: CommandContext, argument: str) -> CommandOutcome:
    print_help(context.mode)
    return CommandOutcome(handled=True)


def _exit(context: CommandContext, argument: str) -> CommandOutcome:
    print("bye")
    return CommandOutcome(handled=True, exit_requested=True)


def _notification(context: CommandContext, argument: str) -> CommandOutcome:
    if not argument:
        print_pending_reminders(context.store, context.user.id)
        return CommandOutcome(handled=True)
    action, _, title = argument.partition(" ")
    if action.casefold() != "remove" or not title.strip():
        print("usage: /notification [remove <name>]")
        return CommandOutcome(handled=True)
    reminder, matches = context.store.cancel_reminder_by_title(
        context.user.id,
        title,
    )
    if len(matches) > 1:
        print(f'Multiple pending reminders are named "{title.strip()}":')
        for match in matches:
            print(f"{match.id}  {match.remind_at}  {match.title}")
    elif reminder is None:
        print(f"Reminder not found: {title.strip()}")
    else:
        print(f"Reminder removed: {reminder.title}")
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


COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "/help": _help,
    "/exit": _exit,
    "/notification": _notification,
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
