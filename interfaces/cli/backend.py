"""Plain CLI session orchestration with compatibility exports for parked helpers."""

import asyncio
import os
from collections.abc import Awaitable, Callable

from application.modes import get_mode_policy
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
from interfaces.cli.commands import (
    COMMAND_HANDLERS,
    CommandContext,
    handle_command,
    print_help as _print_help,
)
from interfaces.cli.operations import notify_cli
from interfaces.cli.output import write as print
from workflows.storage.store import JobStore


EXIT_COMMANDS = frozenset({"/exit"})

# Private helpers used to live in this module. Keep them importable during the
# refactor while new code imports them from their owning modules directly.
__all__ = [
    "COMMAND_HANDLERS",
    "EXIT_COMMANDS",
    "TERMINAL_JOB_STATUSES",
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
    get_mode_policy(mode)
    database_path = os.environ.get("SUTO_DB_PATH", "data/suto.db")
    store = JobStore(database_path)
    context = CommandContext(store, mode=mode)
    background_tasks = []
    if os.environ.get(
        "SUTO_NOTIFY_CLI", os.environ.get("SUTO_NOTIFY_TUI", "")
    ).lower() in {"1", "true", "yes"}:
        background_tasks.append(asyncio.create_task(notify_cli(store, after_id=0)))
    try:
        await asyncio.sleep(0)
        while True:
            try:
                prompt = (await read_prompt()).strip()
            except EOFError:
                print()
                return
            except KeyboardInterrupt:
                print("\nbye")
                return
            if not prompt:
                continue
            outcome = handle_command(context, prompt)
            if outcome.exit_requested:
                return
            if not outcome.handled:
                print("Use /run <task> to queue a job, or /help for commands.")
    finally:
        await _cancel_tasks(background_tasks)
