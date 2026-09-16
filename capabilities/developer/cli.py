"""Parked developer command parsing and presentation helpers."""

import asyncio
import json
import shlex
from pathlib import Path

from workflows.library.definitions import (
    automation_options,
    load_definition_file,
    parse_parameter_value,
)
from workflows.models import Job, JobStatus, MissedRunPolicy, ScheduleKind
from workflows.storage.store import JobStore
from interfaces.cli.output import write as print
from interfaces.cli.progress import format_elapsed

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
    from workflows.runtime.options import validate_options
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
