import asyncio
import os
import sys

from ai import ask_local_ai
from automation.runner import JobRunner
from automation.store import JobStore
from automation.worker import AutomationWorker
from language import choose_reply_language
from modes import get_mode_policy
from progress import format_elapsed, print_progress

EXIT_COMMANDS = frozenset({"/exit", "/quit"})


def _print_help() -> None:
    print("Commands:")
    print("  /help  show available commands")
    print("  /run <task>  create an automation job")
    print("  /jobs  list recent automation jobs")
    print("  /status <job_id>  show job status and result")
    print("  /cancel <job_id>  cancel a queued or running job")
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
    print(f"Tokens: {job.total_tokens:,}")
    event = store.latest_event(job.id)
    if event is not None:
        print(
            f"Current: {event.detail} "
            f"({format_elapsed(event.elapsed_seconds)}, "
            f"tokens {event.total_tokens:,})"
        )
    if job.result:
        print(f"Result:\n{job.result}")
    if job.error:
        print(f"Error: {job.error}")


async def _chat_loop(mode: str) -> None:
    previous_language_code: str | None = None
    database_path = os.environ.get("SUTO_DB_PATH", "data/suto.db")
    store = JobStore(database_path)
    worker = AutomationWorker(store, JobRunner(store))
    worker_task = asyncio.create_task(worker.start())
    print(f"suto CLI (mode: {mode}) — type /help for commands")

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
                _print_help()
                continue
            if command in EXIT_COMMANDS:
                print("bye")
                return
            if command == "/run":
                if not argument:
                    print("usage: /run <task>")
                    continue
                job = worker.submit(argument)
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
            if command == "/cancel":
                if not argument:
                    print("usage: /cancel <job_id>")
                    continue
                if await worker.cancel(argument):
                    print(f"Cancelled job {argument}")
                else:
                    print(f"Job cannot be cancelled: {argument}")
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
