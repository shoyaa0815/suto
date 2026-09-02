import asyncio
from pathlib import Path

from .models import Job, JobStatus
from .runner import JobRunner
from .store import JobStore


class AutomationWorker:
    def __init__(
        self,
        store: JobStore,
        runner: JobRunner,
        poll_interval: float = 1.0,
    ) -> None:
        self.store = store
        self.runner = runner
        self.poll_interval = poll_interval
        self.active_job_id: str | None = None
        self._active_task: asyncio.Task | None = None
        self._stopping = False
        self._wake = asyncio.Event()

    def submit(
        self,
        prompt: str,
        source: str = "cli",
        source_ref: str | None = None,
        workspace: str | Path = ".",
        allow_write: bool = False,
    ) -> Job:
        job = self.store.create_job(
            prompt,
            mode="agent",
            source=source,
            source_ref=source_ref,
            workspace=str(workspace),
            allow_write=allow_write,
        )
        self._wake.set()
        return job

    async def cancel(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        if job is None or job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            return False
        cancelled = self.store.cancel_job(job_id)
        if cancelled and self.active_job_id == job_id and self._active_task:
            self._active_task.cancel()
        self._wake.set()
        return cancelled

    async def start(self) -> None:
        self.store.recover_interrupted_jobs()
        while not self._stopping:
            job = self.store.claim_next_job()
            if job is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(
                        self._wake.wait(),
                        timeout=self.poll_interval,
                    )
                except TimeoutError:
                    pass
                continue

            self.active_job_id = job.id
            self._active_task = asyncio.create_task(self.runner.run(job))
            try:
                await self._active_task
            except asyncio.CancelledError:
                if not self._stopping:
                    continue
            finally:
                self.active_job_id = None
                self._active_task = None

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._active_task is not None:
            self._active_task.cancel()
            try:
                await self._active_task
            except asyncio.CancelledError:
                pass
