import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

from ai import ask_local_ai
from automation.models import Job, JobStatus
from automation.runner import JobRunner
from automation.store import JobStore
from automation.worker import AutomationWorker
from clients.cli.progress import format_elapsed, print_progress
from core.language import choose_reply_language
from core.modes import get_mode_policy

EXIT_COMMANDS = frozenset({"/exit", "/quit"})
TERMINAL_JOB_STATUSES = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.BLOCKED,
        JobStatus.INTERRUPTED,
        JobStatus.WAITING_APPROVAL,
    }
)


def _print_help(mode: str | None = None) -> None:
    if mode == "agent":
        print(
            "Type a task normally to run it in the current workspace with "
            "file-write and verification-command access."
        )
    print("Commands:")
    print("  /help  show available commands")
    print(
        "  /run [--workspace <path>] [--allow-write] [--allow-command] <task>  "
        "create an automation job"
    )
    print("  /jobs  list recent automation jobs")
    print("  /status <job_id>  show job status and result")
    print("  /plan <job_id>  show the current automation plan")
    print("  /commands <job_id>  show commands executed by a job")
    print("  /changes <job_id>  show files changed by a job")
    print("  /cancel <job_id>  cancel a queued, running, or waiting job")
    print("  /resume <job_id>  safely resume an interrupted job")
    print("  /approve <job_id>  approve the pending exact action")
    print("  /reject <job_id>  reject the pending action and block the job")
    print("  /exit  exit suto")
    print("  /quit  exit suto")


async def _read_prompt() -> str:
    """Read stdin without blocking the automation worker event loop."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def read_ready() -> None:
        try:
            line = sys.stdin.readline()
            if not future.done():
                future.set_result(line)
        except Exception as error:
            if not future.done():
                future.set_exception(error)
        finally:
            loop.remove_reader(sys.stdin)

    print("> ", end="", flush=True)
    loop.add_reader(sys.stdin, read_ready)
    try:
        line = await future
    finally:
        loop.remove_reader(sys.stdin)
    if line == "":
        raise EOFError
    return line.strip()


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


def _parse_run(argument: str) -> tuple[str, Path, bool, bool]:
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
        else:
            raise ValueError(f"unknown /run option: {flag}")
    if not parts:
        raise ValueError("/run requires a task")
    if not workspace.is_dir():
        raise ValueError(f"workspace is not a directory: {workspace}")
    return " ".join(parts), workspace, allow_write, allow_command


def _submit_agent_prompt(
    worker: AutomationWorker,
    prompt: str,
    workspace: Path | None = None,
) -> Job:
    """Submit a conversational agent prompt with full local-workspace access."""
    return worker.submit(
        prompt,
        workspace=(workspace or Path.cwd()).resolve(),
        allow_write=True,
        allow_command=True,
    )


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
    else:
        print(f"suto> Job {job.id} failed: {job.error or 'unknown error'}")


async def _chat_loop(mode: str) -> None:
    previous_language_code: str | None = None
    database_path = os.environ.get("SUTO_DB_PATH", "data/suto.db")
    store = JobStore(database_path)
    worker = AutomationWorker(store, JobRunner(store))
    worker_task = asyncio.create_task(worker.start())
    print(f"suto CLI (mode: {mode}) — type /help for commands")
    if mode == "agent":
        print(
            "Type a task normally. Suto will work in the current directory "
            "with file-write and verification-command access."
        )

    try:
        while True:
            try:
                prompt = await _read_prompt()
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
            if command == "/run":
                try:
                    task, workspace, allow_write, allow_command = _parse_run(argument)
                except ValueError as error:
                    print(error)
                    continue
                job = worker.submit(
                    task,
                    workspace=workspace,
                    allow_write=allow_write,
                    allow_command=allow_command,
                )
                print(f"Created job {job.id}")
                continue
            if command == "/jobs":
                _print_jobs(store)
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
                    print("usage: /resume <job_id>")
                    continue
                if worker.resume(argument):
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

            if mode == "agent":
                job = _submit_agent_prompt(worker, prompt)
                print(f"Working on {job.id}...")
                completed_job = await _wait_for_job(store, job.id)
                _print_automatic_job_result(completed_job)
                continue

            reply_language = choose_reply_language(
                prompt,
                previous_code=previous_language_code,
            )
            previous_language_code = reply_language.code

            try:
                answer = await ask_local_ai(
                    prompt,
                    mode=mode,
                    reply_language=reply_language,
                    progress_callback=print_progress,
                )
            except KeyboardInterrupt:
                print("\nrequest cancelled")
                continue

            print(f"suto> {answer}")
    finally:
        await worker.stop()
        await worker_task


def run(mode: str) -> None:
    # Keep direct use of this client subject to the same validation as main.py.
    get_mode_policy(mode)
    asyncio.run(_chat_loop(mode))
