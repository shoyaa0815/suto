import asyncio
import json
import os
import shlex
import sqlite3
import threading
from pathlib import Path
from collections.abc import Awaitable, Callable

from ai import ask_local_ai
from assistant import AssistantContext
from automation.library.bundles import import_bundle, write_bundle
from automation.library.definitions import (
    automation_options,
    load_definition_file,
    parse_parameter_value,
)
from automation.models import Job, JobStatus, MissedRunPolicy, ScheduleKind
from automation.runtime.runner import JobRunner
from automation.storage.store import JobStore
from automation.runtime.worker import AutomationWorker
from clients.tui.output import write as print
from clients.tui.progress import format_elapsed, print_progress
from clients.tui.operations import (
    notify_personal_reminders,
    notify_tui,
    print_pending_reminders,
)
from core.language import choose_reply_language

EXIT_COMMANDS = frozenset({"/exit"})
TERMINAL_JOB_STATUSES = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.BLOCKED,
        JobStatus.INTERRUPTED,
        JobStatus.WAITING_APPROVAL,
        JobStatus.WAITING_CHILDREN,
    }
)


async def _choose_reply_language_async(
    prompt: str,
    previous_code: str | None,
):
    """Run Lingua off the UI event loop; its first detection can be expensive."""
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def detect() -> None:
        try:
            choice = choose_reply_language(prompt, previous_code=previous_code)
        except BaseException as error:
            loop.call_soon_threadsafe(deliver, None, error)
        else:
            loop.call_soon_threadsafe(deliver, choice, None)

    def deliver(choice, error: BaseException | None) -> None:
        if result.done():
            return
        if error is not None:
            result.set_exception(error)
        else:
            result.set_result(choice)

    threading.Thread(target=detect, name="suto-language", daemon=True).start()
    return await result


def _print_help(mode: str | None = None) -> None:
    print("Commands:")
    print("  /help  show available commands")
    print("  /setting  open profile settings")
    print("  /noti  show reminders that have not been delivered")
    print("  /noti del <reminder_id>  remove a pending reminder")
    print("  /exit  exit suto")


def _print_jobs(store: JobStore) -> None:
    jobs = store.list_jobs()
    if not jobs:
        print("No automation jobs yet.")
        return
    for job in jobs:
        prompt = " ".join(job.prompt.split())
        if len(prompt) > 60:
            prompt = prompt[:57] + "..."
        print(
            f"{job.id}  {job.status.value:<9}  "
            f"tokens={job.total_tokens:<6}  {prompt}"
        )


def _print_schedules(store: JobStore) -> None:
    schedules = store.list_schedules()
    if not schedules:
        print("No schedules yet.")
        return
    for schedule in schedules:
        if not schedule.enabled:
            state = "paused"
        elif schedule.next_run_at is None:
            state = "finished"
        else:
            state = "active"
        prompt = " ".join(schedule.prompt.split())
        if len(prompt) > 50:
            prompt = prompt[:47] + "..."
        next_run = schedule.next_run_at or "none"
        print(
            f"{schedule.id}  {state:<8}  {schedule.kind.value:<8}  "
            f"next={next_run}  {prompt}"
        )


def _print_job_status(store: JobStore, job_id: str) -> None:
    job = store.get_job(job_id)
    if job is None:
        print(f"Job not found: {job_id}")
        return

    print(f"Job: {job.id}")
    print(f"Status: {job.status.value}")
    print(f"Workspace: {job.workspace}")
    permission = "read/write" if job.allow_write else "read-only"
    print(f"Workspace access: {permission}")
    print(f"Command execution: {'allowed' if job.allow_command else 'blocked'}")
    print(f"Attempts: {job.attempt_count} (automatic retries: {job.retry_count})")
    print(f"Tokens: {job.total_tokens:,}")
    current_step = store.current_step(job.id)
    if current_step is not None:
        print(
            f"Current step: {current_step.position}. "
            f"[{current_step.status.value}] {current_step.description}"
        )
    event = store.latest_event(job.id)
    if event is not None:
        print(
            f"Current: {event.detail} "
            f"({format_elapsed(event.elapsed_seconds)}, "
            f"tokens {event.total_tokens:,})"
        )
    tool_event = store.latest_tool_event(job.id)
    if tool_event is not None:
        print(
            f"Last tool: {tool_event.tool_name} ({tool_event.status}, "
            f"{tool_event.elapsed_seconds:.1f}s, "
            f"result {tool_event.result_size} bytes)"
        )
        if tool_event.error:
            print(f"Tool error: {tool_event.error}")
    command_event = store.latest_command_event(job.id)
    if command_event is not None:
        command = shlex.join(json.loads(command_event.command))
        exit_code = (
            "none" if command_event.exit_code is None else command_event.exit_code
        )
        print(
            f"Last command: {command} ({command_event.status}, "
            f"exit {exit_code}, {command_event.elapsed_seconds:.1f}s)"
        )
    approval = store.latest_approval(job.id)
    if approval is not None:
        print(
            f"Latest approval: {approval.id} "
            f"({approval.action_type.value}, {approval.status.value})"
        )
        print(f"Action: {approval.action_summary}")
        print(f"Expires: {approval.expires_at}")
        if approval.preview:
            print(f"Preview:\n{approval.preview}")
    if job.result:
        print(f"Result:\n{job.result}")
    if job.error:
        label = "Blocked reason" if job.status == JobStatus.BLOCKED else "Error"
        print(f"{label}: {job.error}")


def _print_plan(store: JobStore, job_id: str) -> None:
    if store.get_job(job_id) is None:
        print(f"Job not found: {job_id}")
        return
    steps = store.list_steps(job_id)
    if not steps:
        print(f"No plan created for {job_id}.")
        return
    print(f"Plan for {job_id}:")
    for step in steps:
        line = f"{step.position}. [{step.status.value}] {step.description}"
        if step.result:
            line += f" — {step.result}"
        print(line)


def _print_commands(store: JobStore, job_id: str) -> None:
    if store.get_job(job_id) is None:
        print(f"Job not found: {job_id}")
        return
    events = store.list_command_events(job_id)
    if not events:
        print(f"No commands recorded for {job_id}.")
        return
    print(f"Commands for {job_id} (newest first):")
    for event in events:
        command = shlex.join(json.loads(event.command))
        exit_code = "none" if event.exit_code is None else event.exit_code
        print(
            f"[{event.status}] exit={exit_code} "
            f"time={event.elapsed_seconds:.1f}s  {command}"
        )
        if event.stdout:
            print(f"stdout:\n{event.stdout}")
        if event.stderr:
            print(f"stderr:\n{event.stderr}")


def _print_changes(store: JobStore, job_id: str) -> None:
    if store.get_job(job_id) is None:
        print(f"Job not found: {job_id}")
        return
    changes = store.list_change_events(job_id)
    if not changes:
        print(f"No file changes recorded for {job_id}.")
        return
    for index, change in enumerate(changes, start=1):
        before = change.before_sha256 or "new file"
        print(f"Change {index}: {change.path}")
        print(f"SHA256: {before} -> {change.after_sha256}")
        print(change.diff)


def _print_automations(store: JobStore) -> None:
    items = store.list_automations()
    if not items:
        print("No reusable automations yet.")
        return
    for item in items:
        version = store.get_current_automation_version(item.id)
        skills = store.list_automation_skill_versions(version.id) if version else []
        print(
            f"{item.name}  version={item.current_version}  "
            f"skills={len(skills)}  "
            f"workspace={version.workspace if version else 'unknown'}"
        )


def _print_automation(store: JobStore, name: str) -> None:
    item = store.get_automation(name)
    if item is None:
        print(f"Automation not found: {name}")
        return
    version = store.get_current_automation_version(item.id)
    if version is None:
        print(f"Automation has no version: {name}")
        return
    skills = store.list_automation_skill_versions(version.id)
    print(f"Automation: {item.name}")
    print(f"Version: {version.version} ({version.id})")
    print(f"Description: {version.description or 'none'}")
    print(f"Workspace: {version.workspace}")
    print(f"Write: {'allowed' if version.allow_write else 'blocked'}")
    print(f"Command: {'allowed' if version.allow_command else 'blocked'}")
    print("Skills: " + (", ".join(name for name, _ in skills) or "none"))
    print(
        "Parameters: "
        + (json.dumps(version.parameter_schema, ensure_ascii=False) or "{}")
    )
    print(f"Prompt template:\n{version.prompt_template}")


def _print_automation_history(store: JobStore, name: str) -> None:
    item = store.get_automation(name)
    if item is None:
        print(f"Automation not found: {name}")
        return
    jobs = store.list_automation_jobs(item.id)
    if not jobs:
        print(f"No runs for automation: {item.name}")
        return
    for job in jobs:
        version = store.get_automation_version(job.source_ref or "")
        version_number = version.version if version else "unknown"
        print(
            f"{job.id}  {job.status.value:<16}  version={version_number}  "
            f"tokens={job.total_tokens}  created={job.created_at}"
        )


def _print_skills(store: JobStore) -> None:
    skills = store.list_skills()
    if not skills:
        print("No reusable skills yet.")
        return
    for skill in skills:
        print(f"{skill.name}  version={skill.current_version}")


def _print_skill(store: JobStore, name: str) -> None:
    skill = store.get_skill(name)
    if skill is None:
        print(f"Skill not found: {name}")
        return
    version = store.get_current_skill_version(skill.id)
    if version is None:
        print(f"Skill has no version: {name}")
        return
    print(f"Skill: {skill.name}")
    print(f"Version: {version.version} ({version.id})")
    print(f"Instructions:\n{version.instructions}")


def _parse_automation_run(argument: str) -> tuple[str, dict]:
    try:
        parts = shlex.split(argument)
    except ValueError as error:
        raise ValueError(f"invalid automation run arguments: {error}") from error
    if not parts:
        raise ValueError("usage: /automation run <name> [key=value ...]")
    name = parts.pop(0)
    parameters = {}
    for item in parts:
        key, separator, raw_value = item.partition("=")
        if not separator or not key:
            raise ValueError(f"parameter must use key=value: {item}")
        if key in parameters:
            raise ValueError(f"parameter was provided more than once: {key}")
        parameters[key] = parse_parameter_value(raw_value)
    return name, parameters


def _definition_options(path: str, forced_name: str | None = None) -> dict:
    options = automation_options(load_definition_file(path), forced_name)
    if forced_name is not None:
        options["name"] = forced_name
    return options


def _single_argument(argument: str, usage: str) -> str:
    try:
        parts = shlex.split(argument)
    except ValueError as error:
        raise ValueError(f"invalid arguments: {error}") from error
    if len(parts) != 1:
        raise ValueError(usage)
    return parts[0]


def _read_skill_file(path: str) -> str:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"skill file not found: {source}")
    try:
        return source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"cannot read skill file: {error}") from error


def _parse_run(argument: str, *, include_options: bool = False):
    try:
        parts = shlex.split(argument)
    except ValueError as error:
        raise ValueError(f"invalid /run arguments: {error}") from error
    if not parts:
        raise ValueError(
            "usage: /run [--workspace <path>] [--allow-write] "
            "[--allow-command] <task>"
        )

    workspace = Path.cwd()
    allow_write = False
    allow_command = False
    options = {}
    while parts and parts[0].startswith("--"):
        flag = parts.pop(0)
        if flag == "--allow-write":
            allow_write = True
        elif flag == "--allow-command":
            allow_command = True
        elif flag == "--workspace":
            if not parts:
                raise ValueError("--workspace requires a path")
            workspace = Path(parts.pop(0)).expanduser().resolve()
        elif flag in {'--memory', '--retrieval', '--subtasks'}:
            options[flag[2:]] = True
        elif flag in {'--sandbox', '--max-tokens', '--max-tool-calls', '--max-seconds', '--max-files'}:
            if not parts:
                raise ValueError(f'{flag} requires a value')
            value = parts.pop(0)
            name = {'--max-seconds': 'max_elapsed_seconds', '--max-files': 'max_changed_files'}.get(flag, flag[2:].replace('-', '_'))
            options[name] = value if flag == '--sandbox' else int(value)
        else:
            raise ValueError(f"unknown /run option: {flag}")
    if not parts:
        raise ValueError("/run requires a task")
    if not workspace.is_dir():
        raise ValueError(f"workspace is not a directory: {workspace}")
    from automation.runtime.options import validate_options
    options = validate_options(options)
    result = (" ".join(parts), workspace, allow_write, allow_command)
    return (*result, options) if include_options else result


def _parse_schedule(argument: str) -> dict:
    try:
        parts = shlex.split(argument)
    except ValueError as error:
        raise ValueError(f"invalid /schedule arguments: {error}") from error
    usage = (
        "usage: /schedule (--at <ISO> | --every <seconds> | --cron <expr>) "
        "[--timezone <zone>] [--workspace <path>] [--allow-write] "
        "[--allow-command] [--missed-run <run_once|skip>] "
        "[--retry <count>] [--retry-delay <seconds>] <task>"
    )
    if not parts:
        raise ValueError(usage)

    kind = None
    expression = None
    timezone = "UTC"
    workspace = Path.cwd()
    allow_write = False
    allow_command = False
    missed_run_policy = MissedRunPolicy.RUN_ONCE
    retry_limit = 0
    retry_delay_seconds = 60
    while parts and parts[0].startswith("--"):
        flag = parts.pop(0)
        if flag in {"--at", "--every", "--cron"}:
            if kind is not None:
                raise ValueError("choose exactly one of --at, --every, or --cron")
            if not parts:
                raise ValueError(f"{flag} requires a value")
            kind = {
                "--at": ScheduleKind.ONCE,
                "--every": ScheduleKind.INTERVAL,
                "--cron": ScheduleKind.CRON,
            }[flag]
            expression = parts.pop(0)
        elif flag in {
            "--timezone",
            "--workspace",
            "--missed-run",
            "--retry",
            "--retry-delay",
        }:
            if not parts:
                raise ValueError(f"{flag} requires a value")
            value = parts.pop(0)
            if flag == "--timezone":
                timezone = value
            elif flag == "--workspace":
                workspace = Path(value).expanduser().resolve()
            elif flag == "--missed-run":
                try:
                    missed_run_policy = MissedRunPolicy(value)
                except ValueError as error:
                    raise ValueError(
                        "--missed-run must be run_once or skip"
                    ) from error
            elif flag == "--retry":
                try:
                    retry_limit = int(value)
                except ValueError as error:
                    raise ValueError("--retry must be a whole number") from error
            else:
                try:
                    retry_delay_seconds = int(value)
                except ValueError as error:
                    raise ValueError("--retry-delay must be a whole number") from error
        elif flag == "--allow-write":
            allow_write = True
        elif flag == "--allow-command":
            allow_command = True
        else:
            raise ValueError(f"unknown /schedule option: {flag}")
    if kind is None:
        raise ValueError("choose one of --at, --every, or --cron")
    if not parts:
        raise ValueError("/schedule requires a task")
    if not workspace.is_dir():
        raise ValueError(f"workspace is not a directory: {workspace}")
    return {
        "kind": kind,
        "expression": expression,
        "prompt": " ".join(parts),
        "timezone": timezone,
        "workspace": workspace,
        "allow_write": allow_write,
        "allow_command": allow_command,
        "missed_run_policy": missed_run_policy,
        "retry_limit": retry_limit,
        "retry_delay_seconds": retry_delay_seconds,
    }


async def _wait_for_job(store: JobStore, job_id: str) -> Job:
    last_event_id: int | None = None
    while True:
        job = store.get_job(job_id)
        if job is None:
            raise RuntimeError(f"automation job disappeared: {job_id}")

        event = store.latest_event(job_id)
        if event is not None and event.id != last_event_id:
            print(
                f"[{job_id}] {event.detail} "
                f"({format_elapsed(event.elapsed_seconds)}, "
                f"tokens {event.total_tokens:,})"
            )
            last_event_id = event.id

        if job.status in TERMINAL_JOB_STATUSES:
            return job
        await asyncio.sleep(0.25)


def _print_automatic_job_result(job: Job) -> None:
    if job.status == JobStatus.COMPLETED:
        print(f"suto> {job.result or 'Task completed.'}")
    elif job.status == JobStatus.CANCELLED:
        print(f"suto> Job {job.id} was cancelled.")
    elif job.status == JobStatus.BLOCKED:
        print(f"suto> Job {job.id} was blocked: {job.error or 'unknown reason'}")
    elif job.status == JobStatus.INTERRUPTED:
        print(f"suto> Job {job.id} was interrupted. Resume it with /resume {job.id}.")
    elif job.status == JobStatus.WAITING_APPROVAL:
        print(
            f"suto> Job {job.id} is waiting for approval. "
            f"Review it with /status {job.id}, then use /approve {job.id} "
            f"or /reject {job.id}."
        )
    elif job.status == JobStatus.WAITING_CHILDREN:
        print(f'suto> Job {job.id} is waiting for subtasks. Inspect with /subtasks {job.id}.')
    else:
        print(f"suto> Job {job.id} failed: {job.error or 'unknown error'}")


async def run_session(
    mode: str,
    read_prompt: Callable[[], Awaitable[str]],
) -> None:
    previous_language_code: str | None = None
    database_path = os.environ.get("SUTO_DB_PATH", "data/suto.db")
    store = JobStore(database_path)
    user = store.resolve_channel_identity(
        "tui",
        "local",
        display_name=os.environ.get("SUTO_USER_NAME", "User"),
        timezone=os.environ.get("SUTO_TIMEZONE", "UTC"),
        locale=os.environ.get("SUTO_LOCALE", "th"),
    )
    conversation = store.get_or_create_conversation(user.id, "tui", "local")
    worker = AutomationWorker(store, JobRunner(store))
    worker_task = asyncio.create_task(worker.start())
    notification_task = (asyncio.create_task(notify_tui(store))
                         if os.environ.get('SUTO_NOTIFY_TUI', '').lower() in {'1', 'true', 'yes'} else None)
    reminder_task = asyncio.create_task(notify_personal_reminders(store, user.id))
    print("Type /help for commands")
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
            command, _, argument = prompt.partition(" ")
            command = command.lower()
            argument = argument.strip()

            if command == "/help":
                _print_help(mode)
                continue
            if command in EXIT_COMMANDS:
                print("bye")
                return
            if command == "/noti":
                if not argument:
                    print_pending_reminders(store, user.id)
                else:
                    parts = argument.split()
                    if len(parts) != 2 or parts[0].casefold() != "del":
                        print("usage: /noti [del <reminder_id>]")
                    else:
                        reminder = store.cancel_reminder(user.id, parts[1])
                        if reminder is None:
                            print(f"Reminder not found: {parts[1]}")
                        else:
                            print(f"Reminder removed: {reminder.id}")
                continue
            if prompt.startswith("/"):
                print(f"Unknown command: {command}. Type /help for commands.")
                continue
            if command == "/run":
                try:
                    task, workspace, allow_write, allow_command, options = _parse_run(argument, include_options=True)
                    job = worker.submit(task, workspace=workspace, allow_write=allow_write,
                                        allow_command=allow_command, options=options)
                except (ValueError, sqlite3.IntegrityError) as error:
                    print(error)
                    continue
                print(f"Created job {job.id}")
                continue
            if command == "/jobs":
                _print_jobs(store)
                continue
            if command == "/automation":
                subcommand, _, remainder = argument.partition(" ")
                subcommand = subcommand.casefold()
                remainder = remainder.strip()
                try:
                    if subcommand == "list":
                        _print_automations(store)
                    elif subcommand == "show":
                        if not remainder:
                            raise ValueError("usage: /automation show <name>")
                        _print_automation(store, remainder)
                    elif subcommand == "create":
                        definition_path = _single_argument(
                            remainder,
                            "usage: /automation create <definition.json>",
                        )
                        item = store.create_automation(
                            **_definition_options(definition_path)
                        )
                        print(f"Created automation {item.name} version 1")
                    elif subcommand == "edit":
                        parts = shlex.split(remainder)
                        if len(parts) != 2:
                            raise ValueError(
                                "usage: /automation edit <name> <definition.json>"
                            )
                        version = store.revise_automation(
                            **_definition_options(parts[1], parts[0])
                        )
                        print(
                            f"Created automation {parts[0]} version {version.version}"
                        )
                    elif subcommand == "run":
                        name, parameters = _parse_automation_run(remainder)
                        job = worker.submit_automation(name, parameters)
                        print(f"Created job {job.id} from automation {name}")
                    elif subcommand == "history":
                        if not remainder:
                            raise ValueError("usage: /automation history <name>")
                        _print_automation_history(store, remainder)
                    elif subcommand == "export":
                        parts = shlex.split(remainder)
                        if len(parts) != 2:
                            raise ValueError(
                                "usage: /automation export <name> <output.json>"
                            )
                        target = write_bundle(store, parts[0], parts[1])
                        print(f"Exported automation {parts[0]} to {target}")
                    elif subcommand == "import":
                        bundle_path = _single_argument(
                            remainder,
                            "usage: /automation import <bundle.json>",
                        )
                        name, version = import_bundle(store, bundle_path)
                        print(f"Imported automation {name} version {version}")
                    else:
                        raise ValueError(
                            "usage: /automation "
                            "<create|edit|list|show|run|history|export|import> ..."
                        )
                except (OSError, ValueError, sqlite3.IntegrityError) as error:
                    print(error)
                continue
            if command == "/skill":
                subcommand, _, remainder = argument.partition(" ")
                subcommand = subcommand.casefold()
                remainder = remainder.strip()
                try:
                    if subcommand == "list":
                        _print_skills(store)
                    elif subcommand == "show":
                        if not remainder:
                            raise ValueError("usage: /skill show <name>")
                        _print_skill(store, remainder)
                    elif subcommand in {"create", "edit"}:
                        parts = shlex.split(remainder)
                        if len(parts) != 2:
                            raise ValueError(
                                f"usage: /skill {subcommand} <name> <instructions.txt>"
                            )
                        instructions = _read_skill_file(parts[1])
                        if subcommand == "create":
                            skill = store.create_skill(parts[0], instructions)
                            print(f"Created skill {skill.name} version 1")
                        else:
                            version = store.revise_skill(parts[0], instructions)
                            print(
                                f"Created skill {parts[0]} version {version.version}"
                            )
                    else:
                        raise ValueError(
                            "usage: /skill <create|edit|list|show> ..."
                        )
                except (OSError, ValueError) as error:
                    print(error)
                continue
            if command == "/schedule":
                try:
                    options = _parse_schedule(argument)
                    schedule = scheduler.create(**options)
                except ValueError as error:
                    print(error)
                    continue
                worker.wake()
                print(
                    f"Created schedule {schedule.id}; "
                    f"next run {schedule.next_run_at}"
                )
                continue
            if command == "/schedules":
                _print_schedules(store)
                continue
            if command == "/pause":
                if not argument:
                    print("usage: /pause <schedule_id>")
                elif store.set_schedule_enabled(argument, False):
                    print(f"Paused schedule {argument}")
                else:
                    print(f"Schedule not found: {argument}")
                continue
            if command == "/status":
                if not argument:
                    print("usage: /status <job_id>")
                    continue
                _print_job_status(store, argument)
                continue
            if command == "/plan":
                if not argument:
                    print("usage: /plan <job_id>")
                    continue
                _print_plan(store, argument)
                continue
            if command == "/commands":
                if not argument:
                    print("usage: /commands <job_id>")
                    continue
                _print_commands(store, argument)
                continue
            if command == "/changes":
                if not argument:
                    print("usage: /changes <job_id>")
                    continue
                _print_changes(store, argument)
                continue
            if command == "/cancel":
                if not argument:
                    print("usage: /cancel <job_id>")
                    continue
                if await worker.cancel(argument):
                    print(f"Cancelled job {argument}")
                else:
                    print(f"Job cannot be cancelled: {argument}")
                continue
            if command == "/resume":
                if not argument:
                    print("usage: /resume <job_id|schedule_id>")
                    continue
                if argument.startswith("sch_"):
                    if store.set_schedule_enabled(argument, True):
                        worker.wake()
                        print(f"Resumed schedule {argument}")
                    else:
                        print(f"Schedule not found: {argument}")
                elif worker.resume(argument):
                    print(f"Resumed job {argument}")
                else:
                    print(f"Job cannot be resumed: {argument}")
                continue
            if command == "/approve":
                if not argument:
                    print("usage: /approve <job_id>")
                    continue
                _, message = worker.approve(argument)
                print(message)
                continue
            if command == "/reject":
                if not argument:
                    print("usage: /reject <job_id>")
                    continue
                _, message = worker.reject(argument)
                print(message)
                continue

            if prompt.startswith("/"):
                print(f"Unknown command: {command}. Type /help for commands.")
                continue

            print("Preparing request...")
            history = store.conversation_history(conversation.id)
            store.add_message(conversation.id, "user", prompt)
            reply_language = await _choose_reply_language_async(
                prompt,
                previous_language_code,
            )
            previous_language_code = reply_language.code

            try:
                answer = await ask_local_ai(
                    prompt,
                    mode=mode,
                    reply_language=reply_language,
                    progress_callback=print_progress,
                    conversation_history=history,
                    assistant_context=(
                        AssistantContext(store, user.id, conversation.id)
                        if mode == "agent"
                        else None
                    ),
                )
            except KeyboardInterrupt:
                print("\nrequest cancelled")
                continue

            store.add_message(conversation.id, "assistant", answer)
            print(f"suto> {answer}")
    finally:
        reminder_task.cancel()
        await asyncio.gather(reminder_task, return_exceptions=True)
        if notification_task:
            notification_task.cancel()
            await asyncio.gather(notification_task, return_exceptions=True)
        await worker.stop()
        await worker_task
