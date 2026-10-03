"""Public CLI command dispatch, isolated from session lifecycle concerns."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import shlex
import sqlite3
import tomllib
from typing import Any

from application.automation import ApprovalService, AutomationService, JobService, ScheduleService
from workflows.library.definitions import parse_parameter_value
from workflows.models import MissedRunPolicy, ScheduleKind
from workflows.storage.redaction import redact_text
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
    thread_id: str = "local"
    reset_display: Callable[[], None] | None = None
    worker: Any = None


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
    print("  /clear  clear this chat context and terminal view")
    print("  /reset all  delete all saved conversations")
    print("  /reminder [time and title]  list or create reminders")
    print("  /reminder remove <name-or-id>  delete a pending reminder")
    print("  /task [title]  list or create personal tasks")
    print("  /task remove <name-or-id>  delete an open task")
    print("  /run [--workspace <path>] [--allow-write] [--allow-command] <task>  queue a job")
    print("  /jobs  show recent automation jobs")
    print("  /approvals  list pending job approvals")
    print("  /approval show|allow|deny <approval_id>  inspect or decide an approval")
    print("  /status <job_id>  show job details")
    print("  /cancel <job_id>  cancel a job")
    print("  /resume <job_id>  resume an interrupted or blocked job")
    print("  /schedule create (--at <ISO> | --every <seconds> | --cron <expr>) [options] <task>")
    print("  /schedule list|show|pause|resume|history [<schedule_id>]")
    print("  /automation create <definition.json>")
    print("  /automation update <name> <definition.json>")
    print("  /automation list|show|run|history <name> [key=value ...]")
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
    jobs = JobService(context.store, context.worker).list_recent()
    if not jobs:
        print("No automation jobs.")
        return CommandOutcome(handled=True)
    print("Recent automation jobs (newest first):")
    for job in jobs:
        prompt = " ".join(job.prompt.split())
        if len(prompt) > 60:
            prompt = prompt[:57] + "..."
        print(
            f"{job.id}  {job.status.value}  {prompt}  "
            f"created={job.created_at}  latest_attempt={job.attempt_id or 'none'}"
        )
    return CommandOutcome(handled=True)


def _approval_details(approval, job) -> None:
    tool = {"write": "apply_workspace_patch", "command": "run_workspace_command"}.get(
        approval.action_type.value, "unknown"
    )
    print(f"Approval: {approval.id}")
    print(f"Job: {job.id} ({job.status.value})")
    print(f"Attempt: {job.attempt_count} ({job.attempt_id or 'none'})")
    print(f"Requested action: {redact_text(approval.action_summary)}")
    print(f"Tool: {tool}")
    print(f"Workspace: {redact_text(job.workspace)}")
    print(f"Permission requested: {approval.action_type.value} approval")
    print(f"Status: {approval.status.value}")
    print(f"Created: {approval.requested_at}")
    print(f"Expires: {approval.expires_at}")
    print(f"Decided: {approval.decided_at or 'none'}")


def _approvals(context: CommandContext, argument: str) -> CommandOutcome:
    if argument:
        print("usage: /approvals")
        return CommandOutcome(handled=True)
    try:
        service = ApprovalService(context.store, context.worker)
        approvals = service.list_pending()
        if not approvals:
            print("No pending approvals.")
        for approval in approvals:
            _, job = service.get(approval.id)
            _approval_details(approval, job)
    except sqlite3.Error:
        print("Cannot list approvals: storage error.")
    return CommandOutcome(handled=True)


def _approval(context: CommandContext, argument: str) -> CommandOutcome:
    usage = "usage: /approval show|allow|deny <approval_id>"
    try:
        parts = shlex.split(argument)
    except ValueError:
        parts = []
    if len(parts) != 2 or parts[0] not in {"show", "allow", "deny"}:
        print(usage)
        return CommandOutcome(handled=True)
    action, approval_id = parts
    service = ApprovalService(context.store, context.worker)
    try:
        if action == "show":
            approval, job = service.get(approval_id)
        else:
            approval, job, decided, message = service.decide(approval_id, action == "allow")
            print(f"Decision: {redact_text(message)}")
        _approval_details(approval, job)
    except ValueError as error:
        print(str(error))
    except sqlite3.Error:
        print("Cannot access approval: storage error.")
    return CommandOutcome(handled=True)


def _parse_public_run(argument: str) -> tuple[str, str, bool, bool]:
    usage = "usage: /run [--workspace <path>] [--allow-write] [--allow-command] <task>"
    try:
        parts = shlex.split(argument)
    except ValueError:
        raise ValueError(usage) from None
    workspace = "."
    allow_write = False
    allow_command = False
    while parts and parts[0].startswith("--"):
        option = parts.pop(0)
        if option == "--workspace":
            if not parts or parts[0].startswith("--"):
                raise ValueError("--workspace requires a path")
            workspace = parts.pop(0)
        elif option == "--allow-write":
            allow_write = True
        elif option == "--allow-command":
            allow_command = True
        else:
            raise ValueError(f"unknown /run option: {option}")
    if not parts:
        raise ValueError(usage)
    return " ".join(parts), workspace, allow_write, allow_command


def _run(context: CommandContext, argument: str) -> CommandOutcome:
    try:
        prompt, workspace, allow_write, allow_command = _parse_public_run(argument)
        service = JobService(context.store, context.worker)
        job = service.submit(
            prompt, workspace=workspace,
            allow_write=allow_write, allow_command=allow_command,
        )
    except (ValueError, OSError) as error:
        print(f"Cannot create job: {error}")
    except sqlite3.IntegrityError as error:
        message = str(error)
        print("Cannot create job: quota reached" if "quota" in message or "rate limit" in message else "Cannot create job.")
    except sqlite3.Error:
        print("Cannot create job: storage error.")
    else:
        print("Job created")
        print(f"ID: {job.id}")
        print(f"Status: {job.status.value}")
        print(f"Workspace: {job.workspace}")
        print(f"Write: {'allowed' if job.allow_write else 'denied'}")
        print(f"Command: {'allowed' if job.allow_command else 'denied'}")
        print("Job queued." if service.worker_ready else "Job saved and waiting for worker.")
    return CommandOutcome(handled=True)


def _job_id(argument: str, command: str) -> str:
    try:
        parts = shlex.split(argument)
    except ValueError:
        parts = []
    if len(parts) != 1:
        raise ValueError(f"usage: /{command} <job_id>")
    return parts[0]


def _status(context: CommandContext, argument: str) -> CommandOutcome:
    try:
        job_id = _job_id(argument, "status")
        service = JobService(context.store, context.worker)
        job = service.get(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")
        trigger_id = service.schedule_trigger_id(job)
    except ValueError as error:
        print(str(error))
        return CommandOutcome(handled=True)
    except sqlite3.Error:
        print("Cannot read job: storage error.")
        return CommandOutcome(handled=True)
    print(f"Job: {job.id}")
    print(f"Status: {job.status.value}")
    print(f"Attempt: {job.attempt_count} ({job.attempt_id or 'none'})")
    print(f"Workspace: {job.workspace}")
    print(f"Created: {job.created_at}")
    print(f"Started: {job.started_at or 'none'}")
    print(f"Finished: {job.finished_at or 'none'}")
    print(f"Automation version: {job.source_ref if job.source == 'automation' else 'none'}")
    print(f"Schedule trigger: {trigger_id if trigger_id is not None else 'none'}")
    result = " ".join(redact_text(job.result).split()) if job.result else "none"
    if len(result) > 200:
        result = result[:197] + "..."
    print(f"Result summary: {result}")
    print(f"Safe error: {redact_text(job.error) if job.error else 'none'}")
    return CommandOutcome(handled=True)


def _change_job(context: CommandContext, argument: str, action: str) -> CommandOutcome:
    try:
        job_id = _job_id(argument, action)
        service = JobService(context.store, context.worker)
        job = service.cancel(job_id) if action == "cancel" else service.resume(job_id)
    except ValueError as error:
        print(str(error))
    except sqlite3.Error:
        print(f"Cannot {action} job: storage error.")
    else:
        print(f"Job {job.id}: {job.status.value}")
        if action == "resume":
            print("Job queued." if service.worker_ready else "Job saved and waiting for worker.")
    return CommandOutcome(handled=True)


def _cancel(context: CommandContext, argument: str) -> CommandOutcome:
    return _change_job(context, argument, "cancel")


def _resume(context: CommandContext, argument: str) -> CommandOutcome:
    return _change_job(context, argument, "resume")


def _parse_schedule_create(argument: str, default_timezone: str) -> dict:
    usage = ("usage: /schedule create (--at <ISO> | --every <seconds> | --cron <expr>) "
             "[--timezone <zone>] [--workspace <path>] [--allow-write] "
             "[--allow-command] [--missed-run <run_once|skip>] "
             "[--retry <count>] [--retry-delay <seconds>] <task>")
    try:
        parts = shlex.split(argument)
    except ValueError:
        raise ValueError(usage) from None
    options: dict = {
        "timezone": default_timezone, "workspace": ".",
        "allow_write": False, "allow_command": False,
        "missed_run_policy": MissedRunPolicy.RUN_ONCE,
        "retry_limit": 0, "retry_delay_seconds": 60,
    }
    schedule_flags = {"--at": ScheduleKind.ONCE,
                      "--every": ScheduleKind.INTERVAL, "--cron": ScheduleKind.CRON}
    value_flags = {
        "--timezone": "timezone", "--workspace": "workspace",
        "--missed-run": "missed_run_policy", "--retry": "retry_limit",
        "--retry-delay": "retry_delay_seconds",
    }
    while parts and parts[0].startswith("--"):
        flag = parts.pop(0)
        if flag in {"--allow-write", "--allow-command"}:
            options[flag.removeprefix("--").replace("-", "_")] = True
            continue
        if flag == "--":
            break
        if flag not in schedule_flags and flag not in value_flags:
            raise ValueError(f"unknown /schedule option: {flag}")
        if not parts or parts[0].startswith("--"):
            raise ValueError(f"{flag} requires a value")
        value = parts.pop(0)
        if flag in schedule_flags:
            if "kind" in options:
                raise ValueError("choose exactly one of --at, --every, or --cron")
            options.update(kind=schedule_flags[flag], expression=value)
        elif flag == "--missed-run":
            try:
                options["missed_run_policy"] = MissedRunPolicy(value)
            except ValueError:
                raise ValueError("--missed-run must be run_once or skip") from None
        elif flag in {"--retry", "--retry-delay"}:
            try:
                options[value_flags[flag]] = int(value)
            except ValueError:
                raise ValueError(f"{flag} must be a whole number") from None
        else:
            options[value_flags[flag]] = value
    if "kind" not in options or not parts:
        raise ValueError(usage)
    options["prompt"] = " ".join(parts)
    return options


def _schedule(context: CommandContext, argument: str) -> CommandOutcome:
    service = ScheduleService(context.store, context.worker)
    try:
        action, _, rest = argument.strip().partition(" ")
        if action == "create":
            timezone = getattr(context.user, "timezone", "UTC")
            schedule = service.create(**_parse_schedule_create(rest, timezone))
            print(f"Schedule created: {schedule.id}")
            print(f"State: {service.state(schedule)}")
            print(f"Next run: {schedule.next_run_at or 'none'}")
            return CommandOutcome(handled=True)
        if action == "list" and not rest:
            schedules = service.list_recent()
            if not schedules:
                print("No schedules yet.")
            for schedule in schedules:
                print(f"{schedule.id}  {service.state(schedule)}  {schedule.kind.value}  "
                      f"next={schedule.next_run_at or 'none'}")
            return CommandOutcome(handled=True)
        if action in {"show", "pause", "resume", "history"}:
            try:
                ids = shlex.split(rest)
            except ValueError:
                ids = []
            if len(ids) != 1:
                raise ValueError(f"usage: /schedule {action} <schedule_id>")
            schedule_id = ids[0]
            if action == "history":
                history = service.history(schedule_id)
                if not history:
                    print("No trigger history.")
                for event in history:
                    print(f"{event.id}  {event.scheduled_for}  attempt={event.attempt}  "
                          f"{event.status.value}  job={event.job_id or 'none'}  {event.detail}")
                return CommandOutcome(handled=True)
            if action in {"pause", "resume"}:
                schedule = service.set_paused(schedule_id, action == "pause")
            else:
                schedule = service.get(schedule_id)
            print(f"Schedule: {schedule.id}")
            print(f"State: {service.state(schedule)}")
            print(f"Kind: {schedule.kind.value}")
            print(f"Expression: {schedule.expression}")
            print(f"Timezone: {schedule.timezone}")
            print(f"Task: {schedule.prompt}")
            print(f"Workspace: {schedule.workspace}")
            print(f"Write: {'allowed' if schedule.allow_write else 'denied'}")
            print(f"Command: {'allowed' if schedule.allow_command else 'denied'}")
            print(f"Missed run: {schedule.missed_run_policy.value}")
            print(f"Retry: {schedule.retry_limit} delay={schedule.retry_delay_seconds}s")
            print(f"Next run: {schedule.next_run_at or 'none'}")
            print(f"Last run: {schedule.last_run_at or 'none'}")
            return CommandOutcome(handled=True)
        raise ValueError("usage: /schedule create|list|show|pause|resume|history ...")
    except (ValueError, OSError) as error:
        print(f"Cannot {action or 'use'} schedule: {error}")
    except sqlite3.IntegrityError as error:
        message = str(error)
        print("Cannot create schedule: quota reached" if "quota" in message or "rate limit" in message else "Cannot update schedule.")
    except sqlite3.Error:
        print("Cannot use schedule: storage error.")
    return CommandOutcome(handled=True)


def _automation(context: CommandContext, argument: str) -> CommandOutcome:
    service = AutomationService(context.store, context.worker)
    action = "use"
    try:
        parts = shlex.split(argument)
        if not parts:
            raise ValueError("usage: /automation create|update|list|show|run|history ...")
        action, *args = parts
        if action == "create" and len(args) == 1:
            automation = service.create(args[0])
            print(f"Automation created: {automation.name} version {automation.current_version}")
        elif action == "update" and len(args) == 2:
            version = service.update(args[0], args[1])
            print(f"Automation updated: {args[0]} version {version.version}")
        elif action == "list" and not args:
            automations = service.list_recent()
            if not automations:
                print("No automations yet.")
            for automation in automations:
                print(f"{automation.name}  version={automation.current_version}")
        elif action == "show" and len(args) == 1:
            automation, version, skills = service.show(args[0])
            print(f"Automation: {automation.name}")
            print(f"Version: {version.version}")
            print(f"Description: {redact_text(version.description)}")
            print(f"Workspace: {version.workspace}")
            print(f"Write: {'allowed' if version.allow_write else 'denied'}")
            print(f"Command: {'allowed' if version.allow_command else 'denied'}")
            print(f"Parameters: {', '.join(version.parameter_schema) or 'none'}")
            print(f"Skills: {', '.join(skills) or 'none'}")
        elif action == "run" and args:
            name, *assignments = args
            parameters = {}
            for assignment in assignments:
                key, separator, value = assignment.partition("=")
                if not separator or not key or key in parameters:
                    raise ValueError("run parameters must be unique key=value pairs")
                parameters[key] = parse_parameter_value(value)
            job, version = service.run(name, parameters)
            print(f"Automation: {name}")
            print(f"Version: {version.version}")
            print(f"Job: {job.id}")
            print(f"Status: {job.status.value}")
            print("Job queued." if context.worker is not None and context.worker.ready else "Job saved and waiting for worker.")
        elif action == "history" and len(args) == 1:
            history = service.history(args[0])
            if not history:
                print("No automation runs yet.")
            for job in history:
                pinned = context.store.get_automation_version(job.source_ref)
                print(f"{job.id}  version={pinned.version if pinned else job.source_ref}  "
                      f"status={job.status.value}  created={job.created_at}")
        else:
            raise ValueError(f"usage: /automation {action} ...")
    except (ValueError, OSError) as error:
        print(f"Cannot {action} automation: {redact_text(error)}")
    except sqlite3.IntegrityError as error:
        message = str(error)
        print("Cannot run automation: quota reached" if "quota" in message or "rate limit" in message else "Cannot use automation: storage error.")
    except sqlite3.Error:
        print("Cannot use automation: storage error.")
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
    if context.reset_display is not None:
        context.reset_display()
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
        context.thread_id,
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
    "/approvals": _approvals,
    "/approval": _approval,
    "/run": _run,
    "/status": _status,
    "/cancel": _cancel,
    "/resume": _resume,
    "/schedule": _schedule,
    "/automation": _automation,
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
