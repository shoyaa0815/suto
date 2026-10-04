"""Plain CLI session orchestration with compatibility exports for parked helpers."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from uuid import uuid4

from agent import AgentRequest
from ai import ask_local_ai, execute_local_ai
from application.configuration import load_settings
from application.modes import CLARIFICATIONS_ENABLED, get_mode_policy
from assistant import AssistantContext
from permissions import ApprovalBroker
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
from interfaces.cli.skill_catalog import load_cli_skills
from interfaces.cli.language import (
    choose_reply_language_async as _choose_reply_language_async,
)
from interfaces.cli.operations import (
    notify_personal_reminders,
    notify_cli,
)
from interfaces.cli.output import set_activity, write as print
from interfaces.cli.progress import activity_text, print_progress
from sessions import SessionService, SessionStore
from skills import SkillSelection
from workflows.runtime.runner import JobRunner
from workflows.runtime.worker import AutomationWorker
from workflows.storage.store import JobStore
from workflows.storage.runs import SessionBusyError, TERMINAL


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
    get_mode_policy(mode)
    previous_language_code: str | None = None
    database_path = os.environ.get("SUTO_DB_PATH", "data/suto.db")
    store = JobStore(database_path)
    sessions = SessionService(SessionStore(store))
    skill_registry, skill_warnings = load_cli_skills()
    skills = SkillSelection(skill_registry)
    for warning in skill_warnings:
        print(warning)
    async def on_approval(request):
        set_activity(None)
        activity_writer = getattr(read_prompt, "set_activity", None)
        if activity_writer is not None:
            activity_writer(None)
        print(f"Approve {request.tool_name} for this call? Type allow or deny (ID {request.id})")
        try:
            answer = (await read_prompt()).strip().casefold()
        except (EOFError, KeyboardInterrupt):
            answer = "deny"
        finally:
            if activity_writer is not None:
                activity_writer("Suto is thinking")
        approvals.submit(request.id, "allow_once" if answer == "allow" else "deny")

    approvals = ApprovalBroker(on_approval)
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
    thread_id = f"local:{uuid4().hex}"
    conversation = sessions.resume(
        user.id, CLI_STORAGE_INTERFACE, thread_id
    )
    skills.bind(store, conversation.id)
    notification_cursor = store.latest_notification_id()
    worker = AutomationWorker(store, JobRunner(store))
    worker_task = asyncio.create_task(worker.start())
    notification_task = (
        asyncio.create_task(notify_cli(store, after_id=notification_cursor))
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
                CommandContext(
                    store, user, conversation.id, mode, skills,
                    thread_id=thread_id,
                    reset_display=getattr(read_prompt, "reset_display", None),
                    worker=worker,
                ),
                prompt,
            )
            if outcome.handled:
                if outcome.conversation_id is not None:
                    conversation = sessions.resume(
                        user.id,
                        CLI_STORAGE_INTERFACE,
                        thread_id,
                    )
                    skills.bind(store, conversation.id)
                if outcome.reset_language:
                    previous_language_code = None
                if outcome.exit_requested:
                    return
                continue

            if outcome.request_prompt is not None:
                prompt = outcome.request_prompt

            try:
                active_skills = skills.require_available()
                if outcome.request_skill is not None and outcome.request_skill not in active_skills:
                    active_skills += (outcome.request_skill,)
                run_id = store.begin_agent_run(conversation.id)
            except ValueError as error:
                print(f"Selected Skill unavailable: {error}")
                continue
            except SessionBusyError:
                print("Session already has an active run")
                continue
            store.start_agent_run(run_id)
            run_status = "failed"
            run_text = "Request failed."
            run_usage = {}
            try:
                history = sessions.before_prompt(conversation.id, user.id)
                sessions.append(conversation.id, user.id, "user", prompt)
                reply_language = await _choose_reply_language_async(
                    prompt,
                    previous_language_code,
                )
            except BaseException as error:
                status = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
                store.finish_agent_run(run_id, status, final_text="Request did not complete.", error=status)
                raise
            previous_language_code = reply_language.code

            context = (
                AssistantContext(
                    store, user.id, conversation.id, allow_personal_tools=False
                )
                if mode == "agent"
                else None
            )
            clarification_reader = (
                getattr(read_prompt, "request_clarification", None)
                if CLARIFICATIONS_ENABLED
                else None
            )
            activity_writer = getattr(read_prompt, "set_activity", None)
            cancellation_event = getattr(read_prompt, "cancellation_event", None)

            async def wait_for_request(request):
                if cancellation_event is None:
                    return await request
                request_task = asyncio.create_task(request)
                cancellation_task = asyncio.create_task(cancellation_event.wait())
                done, _ = await asyncio.wait(
                    {request_task, cancellation_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if request_task in done:
                    cancellation_task.cancel()
                    await asyncio.gather(cancellation_task, return_exceptions=True)
                    return request_task.result()
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
                raise KeyboardInterrupt()

            def report_progress(update: dict) -> None:
                if activity_writer is None:
                    print_progress(update)
                else:
                    activity_writer(activity_text(update))

            def start_request_activity() -> None:
                if activity_writer is None:
                    return
                if cancellation_event is not None:
                    cancellation_event.clear()
                activity_writer("Suto is thinking")

            try:
                start_request_activity()
                if clarification_reader is None:
                    def record_result(result):
                        nonlocal run_status, run_text, run_usage
                        run_status = result.status if result.status in TERMINAL else "failed"
                        run_text = result.text if result.status == "completed" else "Request did not complete."
                        run_usage = {"prompt_tokens": result.prompt_tokens,
                                     "output_tokens": result.output_tokens}

                    answer = await wait_for_request(
                        ask_local_ai(
                            prompt,
                            mode=mode,
                            reply_language=reply_language,
                            progress_callback=report_progress,
                            conversation_history=history,
                            assistant_context=context,
                            active_skills=active_skills,
                            skill_registry=skill_registry,
                            run_id=run_id,
                            result_callback=record_result,
                            approval_broker=approvals,
                        )
                    )
                    if run_status == "failed" and run_text == "Request failed.":
                        # Compatibility callers may replace ask_local_ai in tests.
                        run_status, run_text = "completed", answer
                else:
                    result = await wait_for_request(
                        execute_local_ai(
                            AgentRequest(prompt, session_id=conversation.id,
                                         active_skills=active_skills, run_id=run_id),
                            mode=mode,
                            reply_language=reply_language,
                            progress_callback=report_progress,
                            conversation_history=history,
                            assistant_context=context,
                            active_skills=active_skills,
                            skill_registry=skill_registry,
                            approval_broker=approvals,
                        )
                    )
                    while result.status == "waiting_input" and result.clarification:
                        store.finish_agent_run(run_id, "waiting_input", final_text=result.text,
                                               usage={"prompt_tokens": result.prompt_tokens,
                                                      "output_tokens": result.output_tokens})
                        run_id = None
                        if activity_writer is not None:
                            activity_writer(None)
                        answer = await clarification_reader(result.clarification)
                        if answer is None:
                            print("Clarification cancelled.")
                            break
                        question = str(result.clarification["question"])
                        options = result.clarification["options"]
                        sessions.append(
                            conversation.id,
                            user.id,
                            "assistant",
                            question + "\n" + "\n".join(
                                f"- {option}" for option in options
                            ),
                        )
                        resumed_history = sessions.before_prompt(conversation.id, user.id)
                        sessions.append(conversation.id, user.id, "user", answer)
                        run_id = store.begin_agent_run(conversation.id)
                        store.start_agent_run(run_id)
                        start_request_activity()
                        result = await wait_for_request(
                            execute_local_ai(
                                AgentRequest(f"Original request: {prompt}\nUser's answer: {answer}",
                                             session_id=conversation.id,
                                             active_skills=active_skills, run_id=run_id),
                                mode=mode,
                                reply_language=reply_language,
                                progress_callback=report_progress,
                                conversation_history=resumed_history,
                                assistant_context=context,
                                active_skills=active_skills,
                                skill_registry=skill_registry,
                                approval_broker=approvals,
                            )
                        )
                    else:
                        answer = result.text
                    run_status = result.status if result.status in TERMINAL else "failed"
                    run_text = result.text if result.status == "completed" else "Request did not complete."
                    run_usage = {"prompt_tokens": result.prompt_tokens,
                                 "output_tokens": result.output_tokens}
                    if result.status == "waiting_input":
                        continue
            except KeyboardInterrupt:
                run_status, run_text = "cancelled", "Request cancelled."
                print("\nrequest cancelled")
                continue
            finally:
                if run_id is not None:
                    store.finish_agent_run(run_id, run_status, final_text=run_text,
                                           error=None if run_status == "completed" else run_status,
                                           usage=run_usage)
                set_activity(None)
                if activity_writer is not None:
                    activity_writer(None)

            sessions.append(conversation.id, user.id, "assistant", answer)
            print(answer)
    finally:
        await _cancel_tasks(background_tasks)
        await worker.stop()
        await worker_task
