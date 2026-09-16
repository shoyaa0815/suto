"""Plain CLI session orchestration with compatibility exports for parked helpers."""

import asyncio
import os
from collections.abc import Awaitable, Callable

from ai import ask_local_ai, execute_local_ai
from application.configuration import load_settings
from application.modes import CLARIFICATIONS_ENABLED
from assistant import AssistantContext
from capabilities.developer.cli import (
    TERMINAL_JOB_STATUSES,
    _definition_options,
    _parse_automation_run,
    _parse_run,
    _parse_schedule,
    _print_automatic_job_result,
    _print_automation,
    _print_automation_history,
    _print_automations,
    _print_changes,
    _print_commands,
    _print_job_status,
    _print_jobs,
    _print_plan,
    _print_schedules,
    _print_skill,
    _print_skills,
    _read_skill_file,
    _single_argument,
    _wait_for_job,
)
from interfaces.cli import CLI_STORAGE_INTERFACE
from interfaces.cli.commands import (
    COMMAND_HANDLERS,
    CommandContext,
    handle_command,
    print_help as _print_help,
)
from interfaces.cli.language import (
    choose_reply_language_async as _choose_reply_language_async,
)
from interfaces.cli.operations import (
    notify_personal_reminders,
    notify_cli,
)
from interfaces.cli.output import set_activity, write as print
from interfaces.cli.progress import activity_text, print_progress
from workflows.runtime.runner import JobRunner
from workflows.runtime.worker import AutomationWorker
from workflows.storage.store import JobStore


EXIT_COMMANDS = frozenset({"/exit"})

# Private helpers used to live in this module. Keep them importable during the
# refactor while new code imports them from their owning modules directly.
__all__ = [
    "COMMAND_HANDLERS",
    "EXIT_COMMANDS",
    "TERMINAL_JOB_STATUSES",
    "_choose_reply_language_async",
    "_definition_options",
    "_parse_automation_run",
    "_parse_run",
    "_parse_schedule",
    "_print_automatic_job_result",
    "_print_automation",
    "_print_automation_history",
    "_print_automations",
    "_print_changes",
    "_print_commands",
    "_print_help",
    "_print_job_status",
    "_print_jobs",
    "_print_plan",
    "_print_schedules",
    "_print_skill",
    "_print_skills",
    "_read_skill_file",
    "_single_argument",
    "_wait_for_job",
    "run_session",
]


async def _cancel_tasks(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_session(
    mode: str,
    read_prompt: Callable[[], Awaitable[str]],
) -> None:
    previous_language_code: str | None = None
    database_path = os.environ.get("SUTO_DB_PATH", "data/suto.db")
    store = JobStore(database_path)
    profile = load_settings().profile
    user = store.resolve_channel_identity(
        CLI_STORAGE_INTERFACE,
        "local",
        display_name=profile.display_name,
        timezone=profile.timezone,
        locale=profile.locale,
    )
    user = store.apply_profile_settings(
        user.id,
        display_name=profile.display_name,
        timezone=profile.timezone,
        locale=profile.locale,
    )
    conversation = store.get_or_create_conversation(
        user.id, CLI_STORAGE_INTERFACE, "local"
    )
    worker = AutomationWorker(store, JobRunner(store))
    worker_task = asyncio.create_task(worker.start())
    notification_task = (
        asyncio.create_task(notify_cli(store))
        if os.environ.get(
            "SUTO_NOTIFY_CLI", os.environ.get("SUTO_NOTIFY_TUI", "")
        ).lower()
        in {"1", "true", "yes"}
        else None
    )
    reminder_task = asyncio.create_task(notify_personal_reminders(store, user.id))
    background_tasks = [reminder_task]
    if notification_task is not None:
        background_tasks.append(notification_task)
    try:
        await asyncio.sleep(0)
        if worker_task.done():
            worker_task.result()
        while True:
            try:
                prompt = await read_prompt()
            except EOFError:
                print()
                return
            except KeyboardInterrupt:
                print("\nbye")
                return

            if not prompt:
                continue

            outcome = handle_command(
                CommandContext(store, user, conversation.id, mode),
                prompt,
            )
            if outcome.handled:
                if outcome.conversation_id is not None:
                    conversation = store.get_or_create_conversation(
                        user.id,
                        CLI_STORAGE_INTERFACE,
                        "local",
                    )
                if outcome.reset_language:
                    previous_language_code = None
                if outcome.exit_requested:
                    return
                continue

            history = store.conversation_history(conversation.id)
            store.add_message(conversation.id, "user", prompt)
            reply_language = await _choose_reply_language_async(
                prompt,
                previous_language_code,
            )
            previous_language_code = reply_language.code

            context = (
                AssistantContext(store, user.id, conversation.id)
                if mode == "agent"
                else None
            )
            clarification_reader = (
                getattr(read_prompt, "request_clarification", None)
                if CLARIFICATIONS_ENABLED
                else None
            )
            activity_writer = getattr(read_prompt, "set_activity", None)

            def report_progress(update: dict) -> None:
                if activity_writer is None:
                    print_progress(update)
                else:
                    activity_writer(activity_text(update))

            try:
                if activity_writer is not None:
                    activity_writer("Suto is thinking")
                if clarification_reader is None:
                    answer = await ask_local_ai(
                        prompt,
                        mode=mode,
                        reply_language=reply_language,
                        progress_callback=report_progress,
                        conversation_history=history,
                        assistant_context=context,
                    )
                else:
                    result = await execute_local_ai(
                        prompt,
                        mode=mode,
                        reply_language=reply_language,
                        progress_callback=report_progress,
                        conversation_history=history,
                        assistant_context=context,
                    )
                    while result.status == "waiting_input" and result.clarification:
                        answer = await clarification_reader(result.clarification)
                        if answer is None:
                            print("Clarification cancelled.")
                            break
                        question = str(result.clarification["question"])
                        options = result.clarification["options"]
                        store.add_message(
                            conversation.id,
                            "assistant",
                            question + "\n" + "\n".join(
                                f"- {option}" for option in options
                            ),
                        )
                        resumed_history = store.conversation_history(conversation.id)
                        store.add_message(conversation.id, "user", answer)
                        result = await execute_local_ai(
                            f"Original request: {prompt}\nUser's answer: {answer}",
                            mode=mode,
                            reply_language=reply_language,
                            progress_callback=report_progress,
                            conversation_history=resumed_history,
                            assistant_context=context,
                        )
                    else:
                        answer = result.text
                    if result.status == "waiting_input":
                        continue
            except KeyboardInterrupt:
                print("\nrequest cancelled")
                continue
            finally:
                set_activity(None)
                if activity_writer is not None:
                    activity_writer(None)

            store.add_message(conversation.id, "assistant", answer)
            print(answer)
    finally:
        await _cancel_tasks(background_tasks)
        await worker.stop()
        await worker_task
