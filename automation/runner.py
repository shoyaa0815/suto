import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from ai import AIExecutionResult, execute_local_ai

from .context import (
    COMMAND_TOOLS,
    PLANNING_TOOLS,
    READ_ONLY_WORKSPACE_TOOLS,
    WRITE_WORKSPACE_TOOLS,
    ExecutionContext,
    ExecutionLimitExceeded,
)
from .models import Job, JobStatus, StepStatus
from .store import JobStore

AIExecutor = Callable[..., Awaitable[AIExecutionResult]]


class JobRunner:
    def __init__(
        self,
        store: JobStore,
        execute: AIExecutor | None = None,
    ) -> None:
        self.store = store
        self.execute = execute or execute_local_ai

    async def run(self, job: Job) -> None:
        def save_progress(update: dict) -> None:
            self.store.add_event(job.id, update)

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

        try:
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
            )
            result = await self.execute(
                job.prompt,
                mode=job.mode,
                progress_callback=save_progress,
                execution_context=context,
                tool_event_callback=save_tool_event,
                change_event_callback=save_change_event,
            )
        except asyncio.CancelledError:
            current = self.store.get_job(job.id)
            if current is not None and current.status != JobStatus.CANCELLED:
                self.store.fail_job(
                    job.id,
                    "worker stopped before job completed",
                )
            raise
        except Exception as error:
            self.store.fail_job(job.id, f"{type(error).__name__}: {error}")
            return

        if result.status == "completed":
            if not self.store.has_successful_verification_after_last_change(job.id):
                self.store.block_job(
                    job.id,
                    "workspace changes were not followed by a successful "
                    "pytest, compileall, or ruff verification",
                    result.prompt_tokens,
                    result.output_tokens,
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
                    result.prompt_tokens,
                    result.output_tokens,
                )
                return
            self.store.complete_job(
                job.id,
                result.text,
                result.prompt_tokens,
                result.output_tokens,
            )
        elif result.status == "blocked":
            self.store.block_job(
                job.id,
                result.error or result.text or "execution blocked",
                result.prompt_tokens,
                result.output_tokens,
            )
        else:
            self.store.fail_job(
                job.id,
                result.error or result.text or result.status,
                result.prompt_tokens,
                result.output_tokens,
            )
