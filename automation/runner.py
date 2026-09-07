import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from ai import AIExecutionResult, execute_local_ai
from core.settings import env_int

from .context import (
    COMMAND_TOOLS,
    PLANNING_TOOLS,
    READ_ONLY_WORKSPACE_TOOLS,
    WRITE_WORKSPACE_TOOLS,
    ApprovalRequired,
    ExecutionContext,
    ExecutionLimitExceeded,
)
from .models import Job, JobStatus, StepStatus
from .store import JobStore

AIExecutor = Callable[..., Awaitable[AIExecutionResult]]
DEFAULT_RETRY_DELAYS = (1.0, 2.0, 4.0)
APPROVAL_TTL_SECONDS = env_int(
    "APPROVAL_TTL_SECONDS",
    600,
    minimum=1,
)


class JobRunner:
    def __init__(
        self,
        store: JobStore,
        execute: AIExecutor | None = None,
        retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
    ) -> None:
        self.store = store
        self.execute = execute or execute_local_ai
        self.retry_delays = retry_delays

    @staticmethod
    def _is_transient(result: AIExecutionResult) -> bool:
        if result.status == "timed_out":
            return True
        error = (result.error or "").casefold()
        return any(
            marker in error
            for marker in (
                "ai server connection failed",
                "clientconnectorerror",
                "serverdisconnectederror",
                "connection reset",
                "providertransienterror",
            )
        )

    def _checkpoint_error(self, job: Job) -> str | None:
        if job.attempt_count <= 1:
            return None
        workspace = Path(job.workspace).resolve()
        for change in self.store.latest_changes_by_path(job.id):
            target = (workspace / change.path).resolve()
            try:
                target.relative_to(workspace)
            except ValueError:
                return f"cannot resume: changed path escapes workspace: {change.path}"
            if not target.is_file():
                return (
                    "cannot resume: previously changed file is missing: "
                    f"{change.path}"
                )
            current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            if current_hash != change.after_sha256:
                return (
                    f"cannot resume: workspace file changed after checkpoint: "
                    f"{change.path}"
                )
        return None

    def _resume_prompt(self, job: Job) -> str:
        steps = self.store.list_steps(job.id)
        checkpoint = [
            job.prompt,
            "",
            "Resume checkpoint from an earlier attempt:",
            "- Inspect the current workspace before continuing.",
            "- Do not repeat completed steps or recreate the existing plan.",
        ]
        if steps:
            checkpoint.extend(
                f"- Step {step.position} [{step.status.value}]: "
                f"{step.description}"
                + (f" — {step.result}" if step.result else "")
                for step in steps
            )
        else:
            checkpoint.append("- No durable plan was created in the earlier attempt.")
        return "\n".join(checkpoint)

    async def run(self, job: Job) -> None:
        run_started = time.perf_counter()
        base_prompt_tokens = job.prompt_tokens
        base_output_tokens = job.output_tokens

        def save_progress(update: dict) -> None:
            cumulative = dict(update)
            cumulative["prompt_tokens"] = base_prompt_tokens + int(
                update.get("prompt_tokens", 0)
            )
            cumulative["output_tokens"] = base_output_tokens + int(
                update.get("output_tokens", 0)
            )
            cumulative["total_tokens"] = (
                cumulative["prompt_tokens"] + cumulative["output_tokens"]
            )
            self.store.update_job_usage(
                job.id,
                cumulative["prompt_tokens"],
                cumulative["output_tokens"],
            )
            self.store.add_event(job.id, cumulative)

        def save_tool_event(event: dict) -> None:
            self.store.add_tool_event(job.id, event)

        def save_change_event(event: dict) -> None:
            self.store.add_change_event(job.id, event)

        def save_command_event(event: dict) -> None:
            self.store.add_command_event(job.id, event)

        def guard_file_change(path: str) -> None:
            limits = context.limits
            if self.store.has_changed_path(job.id, path):
                return
            if self.store.changed_file_count(job.id) >= limits.max_changed_files:
                raise ExecutionLimitExceeded(
                    "job file-change limit reached "
                    f"({limits.max_changed_files} distinct files)"
                )

        def require_approval(
            action_type: str,
            action: dict,
            summary: str,
            preview: str,
        ) -> None:
            authorized, approval = self.store.request_or_consume_approval(
                job.id,
                action_type,
                action,
                summary,
                preview,
                ttl_seconds=APPROVAL_TTL_SECONDS,
            )
            if not authorized:
                raise ApprovalRequired(approval.id, approval.action_summary)

        try:
            if checkpoint_error := self._checkpoint_error(job):
                self.store.block_job(
                    job.id,
                    checkpoint_error,
                    base_prompt_tokens,
                    base_output_tokens,
                )
                return
            allowed_tools = READ_ONLY_WORKSPACE_TOOLS
            allowed_tools = allowed_tools | PLANNING_TOOLS
            if job.allow_write:
                allowed_tools = allowed_tools | WRITE_WORKSPACE_TOOLS
            if job.allow_command:
                allowed_tools = allowed_tools | COMMAND_TOOLS
            context = ExecutionContext(
                job_id=job.id,
                workspace=Path(job.workspace),
                allowed_tools=allowed_tools,
                plan_store=self.store,
                command_event_callback=save_command_event,
                change_guard_callback=guard_file_change,
                approval_callback=require_approval,
            )
            prompt = self._resume_prompt(job) if job.attempt_count > 1 else job.prompt
            retry_count = job.retry_count
            while True:
                changes_before_attempt = self.store.change_event_count(job.id)
                commands_before_attempt = self.store.command_event_count(job.id)
                remaining_seconds = (
                    context.limits.max_elapsed_seconds
                    - (time.perf_counter() - run_started)
                )
                remaining_tokens = context.limits.max_tokens - (
                    base_prompt_tokens + base_output_tokens
                )
                remaining_tool_calls = (
                    context.limits.max_tool_calls
                    - self.store.tool_event_count(job.id)
                )
                if remaining_seconds <= 0:
                    result = AIExecutionResult(
                        "job elapsed-time limit reached",
                        "blocked",
                        "job elapsed-time limit reached",
                        0,
                        0,
                        0,
                    )
                elif remaining_tokens <= 0:
                    result = AIExecutionResult(
                        "job token limit reached",
                        "blocked",
                        "job token limit reached",
                        0,
                        0,
                        0,
                    )
                elif remaining_tool_calls <= 0:
                    result = AIExecutionResult(
                        "job tool-call limit reached",
                        "blocked",
                        "job tool-call limit reached",
                        0,
                        0,
                        0,
                    )
                else:
                    attempt_context = replace(
                        context,
                        limits=replace(
                            context.limits,
                            max_elapsed_seconds=remaining_seconds,
                            max_tokens=remaining_tokens,
                            max_tool_calls=remaining_tool_calls,
                        ),
                    )
                    try:
                        result = await asyncio.wait_for(
                            self.execute(
                                prompt,
                                mode=job.mode,
                                progress_callback=save_progress,
                                execution_context=attempt_context,
                                tool_event_callback=save_tool_event,
                                change_event_callback=save_change_event,
                            ),
                            timeout=remaining_seconds,
                        )
                    except TimeoutError:
                        result = AIExecutionResult(
                            "job elapsed-time limit reached",
                            "blocked",
                            "job elapsed-time limit reached",
                            0,
                            0,
                            remaining_seconds,
                        )
                total_prompt_tokens = base_prompt_tokens + result.prompt_tokens
                total_output_tokens = base_output_tokens + result.output_tokens
                can_retry = (
                    self._is_transient(result)
                    and retry_count < len(self.retry_delays)
                    and self.store.change_event_count(job.id)
                    == changes_before_attempt
                    and self.store.command_event_count(job.id)
                    == commands_before_attempt
                )
                if not can_retry:
                    break
                delay = self.retry_delays[retry_count]
                reason = result.error or result.text or result.status
                self.store.record_retry(
                    job.id,
                    reason,
                    delay,
                    total_prompt_tokens,
                    total_output_tokens,
                )
                retry_count += 1
                base_prompt_tokens = total_prompt_tokens
                base_output_tokens = total_output_tokens
                prompt = self._resume_prompt(job)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            current = self.store.get_job(job.id)
            if current is not None and current.status != JobStatus.CANCELLED:
                self.store.interrupt_job(
                    job.id,
                    "worker stopped before job completed; safe resume is available",
                )
            raise
        except Exception as error:
            self.store.fail_job(job.id, f"{type(error).__name__}: {error}")
            return

        if result.status == "waiting_approval":
            self.store.update_job_usage(
                job.id,
                total_prompt_tokens,
                total_output_tokens,
            )
            return
        if result.status == "completed":
            if not self.store.has_successful_verification_after_last_change(job.id):
                self.store.block_job(
                    job.id,
                    "workspace changes were not followed by a successful "
                    "pytest, compileall, or ruff verification",
                    total_prompt_tokens,
                    total_output_tokens,
                )
                return
            steps = self.store.list_steps(job.id)
            unfinished = [
                step.position
                for step in steps
                if step.status in {StepStatus.PENDING, StepStatus.IN_PROGRESS}
            ]
            failed_without_reason = [
                step.position
                for step in steps
                if step.status == StepStatus.FAILED and not step.result
            ]
            if unfinished or failed_without_reason:
                details = []
                if unfinished:
                    details.append(
                        "unfinished steps: " + ", ".join(map(str, unfinished))
                    )
                if failed_without_reason:
                    details.append(
                        "failed steps without a reason: "
                        + ", ".join(map(str, failed_without_reason))
                    )
                self.store.fail_job(
                    job.id,
                    "AI finished with an incomplete plan (" + "; ".join(details) + ")",
                    total_prompt_tokens,
                    total_output_tokens,
                )
                return
            self.store.complete_job(
                job.id,
                result.text,
                total_prompt_tokens,
                total_output_tokens,
            )
        elif result.status == "blocked":
            self.store.block_job(
                job.id,
                result.error or result.text or "execution blocked",
                total_prompt_tokens,
                total_output_tokens,
            )
        else:
            self.store.fail_job(
                job.id,
                result.error or result.text or result.status,
                total_prompt_tokens,
                total_output_tokens,
            )
