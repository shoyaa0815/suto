import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

from .definitions import render_automation_prompt
from .models import Job, JobStatus
from .runner import JobRunner
from .scheduler import Scheduler
from .store import JobStore
from .locking import ProcessLock


class AutomationWorker:
    def __init__(
        self,
        store: JobStore,
        runner: JobRunner,
        poll_interval: float = 1.0,
    ) -> None:
        self.store = store
        self.runner = runner
        self.scheduler = Scheduler(store)
        self.poll_interval = poll_interval
        self.active_job_id: str | None = None
        self._active_task: asyncio.Task | None = None
        self._stopping = False
        self._wake = asyncio.Event()
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = ProcessLock(store.path.with_suffix(store.path.suffix + '.worker.lock'))
        self._done = asyncio.Event()
        self._started = False

    def submit(
        self,
        prompt: str,
        source: str = "cli",
        source_ref: str | None = None,
        workspace: str | Path = ".",
        allow_write: bool = False,
        allow_command: bool = False,
        options: dict | None = None,
    ) -> Job:
        job = self.store.create_job(
            prompt,
            mode="agent",
            source=source,
            source_ref=source_ref,
            workspace=str(Path(workspace).expanduser().resolve()),
            allow_write=allow_write,
            allow_command=allow_command,
            options=options,
        )
        self._wake.set()
        return job

    def submit_automation(
        self,
        name: str,
        parameters: dict[str, Any] | None = None,
    ) -> Job:
        version = self.store.get_current_automation_version(name)
        if version is None:
            raise ValueError(f"automation not found: {name}")
        prompt = render_automation_prompt(version, parameters or {})
        return self.submit(
            prompt,
            source="automation",
            source_ref=version.id,
            workspace=version.workspace,
            allow_write=version.allow_write,
            allow_command=version.allow_command,
        )

    async def cancel(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        if job is None or job.status not in {
            JobStatus.QUEUED,
            JobStatus.RUNNING,
            JobStatus.WAITING_APPROVAL,
            JobStatus.WAITING_CHILDREN,
            JobStatus.INTERRUPTED,
        }:
            return False
        cancelled = self.store.cancel_job(job_id)
        if cancelled:
            for active_id, task in self._tasks.items():
                if self.store.get_job(active_id).status == JobStatus.CANCELLED:
                    task.cancel()
        self._wake.set()
        return cancelled

    def resume(self, job_id: str) -> bool:
        resumed = self.store.resume_job(job_id)
        if resumed:
            self._wake.set()
        return resumed

    def wake(self) -> None:
        self._wake.set()

    def approve(self, job_id: str, actor: str = "cli") -> tuple[bool, str]:
        decided, message = self.store.decide_approval(job_id, True, actor)
        if decided or "queued" in message:
            self._wake.set()
        return decided, message

    def reject(self, job_id: str, actor: str = "cli") -> tuple[bool, str]:
        decided, message = self.store.decide_approval(job_id, False, actor)
        if decided:
            self._wake.set()
        return decided, message

    async def start(self) -> None:
        self._started = True
        try:
            if not self._lock.acquire():
                raise RuntimeError('another worker already owns this database')
            self.store.recover_interrupted_jobs()
            last_heartbeat = 0.0
            last_cleanup = 0.0
            while not self._stopping:
                self._wake.clear()
                if time.monotonic() - last_heartbeat >= 5:
                    self.store.heartbeat('running')
                    last_heartbeat = time.monotonic()
                self.store.reconcile_children()
                for job_id, task in list(self._tasks.items()):
                    if self.store.get_job(job_id).status == JobStatus.CANCELLED:
                        task.cancel()
                    if task.done():
                        try:
                            task.result()
                        except asyncio.CancelledError:
                            pass
                        except Exception as error:
                            self.store.fail_job(job_id, f'worker runner failed: {error}')
                        del self._tasks[job_id]
                try:
                    self.scheduler.tick()
                except sqlite3.IntegrityError as error:
                    if 'quota' not in str(error) and 'rate limit' not in str(error):
                        raise
                capacity = self.store.settings()['concurrency']
                retention = self.store.settings()['retention_days']
                if retention and time.monotonic() - last_cleanup >= 3600:
                    self.store.cleanup(retention, dry_run=False)
                    last_cleanup = time.monotonic()
                while len(self._tasks) < capacity and (job := self.store.claim_next_job()):
                    task = asyncio.create_task(self.runner.run(job))
                    task.add_done_callback(lambda _: self._wake.set())
                    self._tasks[job.id] = task
                self.active_job_id = next(iter(self._tasks), None)
                self._active_task = self._tasks.get(self.active_job_id)
                try:
                    await asyncio.wait_for(
                        self._wake.wait(),
                        timeout=min(self.poll_interval, 1.0),
                    )
                except TimeoutError:
                    pass
        finally:
            for task in self._tasks.values():
                task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            self._tasks.clear()
            self.active_job_id = None
            self._active_task = None
            if self._lock.fd is not None:
                try:
                    self.store.heartbeat('stopped')
                finally:
                    self._lock.release()
                    self._done.set()
            else:
                self._done.set()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._started:
            await self._done.wait()
