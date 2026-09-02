import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from ai import AIExecutionResult, execute_local_ai

from .context import (
    READ_ONLY_WORKSPACE_TOOLS,
    WRITE_WORKSPACE_TOOLS,
    ExecutionContext,
)
from .models import Job, JobStatus
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

        try:
            allowed_tools = READ_ONLY_WORKSPACE_TOOLS
            if job.allow_write:
                allowed_tools = allowed_tools | WRITE_WORKSPACE_TOOLS
            context = ExecutionContext(
                job_id=job.id,
                workspace=Path(job.workspace),
                allowed_tools=allowed_tools,
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
            self.store.complete_job(
                job.id,
                result.text,
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
