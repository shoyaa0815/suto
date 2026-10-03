"""Application boundary for public one-time automation jobs."""

from pathlib import Path
from typing import TYPE_CHECKING

from workflows.models import Job, JobStatus
from workflows.runtime.checkpoints import checkpoint_error
from workflows.storage.store import JobStore

if TYPE_CHECKING:
    from workflows.runtime.worker import AutomationWorker


class JobService:
    def __init__(self, store: JobStore, worker: "AutomationWorker | None" = None) -> None:
        self.store = store
        self.worker = worker

    @property
    def worker_ready(self) -> bool:
        return self.worker is not None and self.worker.ready

    def list_recent(self) -> list[Job]:
        return self.store.list_jobs()

    def get(self, job_id: str) -> Job | None:
        return self.store.get_job(job_id)

    def schedule_trigger_id(self, job: Job) -> int | None:
        return self.store.get_job_trigger_id(job.id) if job.source == "schedule" else None

    def submit(
        self, prompt: str, *, workspace: str | Path = ".",
        allow_write: bool = False, allow_command: bool = False,
    ) -> Job:
        if not prompt.strip():
            raise ValueError("/run requires a task")
        path = Path(workspace).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"workspace is not a directory: {path}")
        job = self.store.create_job(
            prompt.strip(), source="cli", workspace=str(path),
            allow_write=allow_write, allow_command=allow_command,
        )
        if self.worker is not None:
            self.worker.wake()
        return job

    def cancel(self, job_id: str) -> Job:
        job = self._require_job(job_id)
        if job.status == JobStatus.CANCELLED:
            return job
        if self.worker is not None:
            self.worker.request_cancel(job_id)
        else:
            self.store.cancel_job(job_id)
        current = self._require_job(job_id)
        if current.status != JobStatus.CANCELLED:
            raise ValueError(f"job cannot be cancelled from {current.status.value}")
        return current

    def resume(self, job_id: str) -> Job:
        job = self._require_job(job_id)
        if job.status not in {JobStatus.INTERRUPTED, JobStatus.BLOCKED}:
            raise ValueError(f"job cannot be resumed from {job.status.value}")
        if error := checkpoint_error(self.store, job, resuming=True):
            raise ValueError(error)
        if self.worker is not None:
            resumed = self.worker.resume(job_id)
        else:
            resumed = self.store.resume_job(job_id)
        if not resumed:
            current = self._require_job(job_id)
            raise ValueError(f"job cannot be resumed from {current.status.value}")
        return self._require_job(job_id)

    def _require_job(self, job_id: str) -> Job:
        job = self.store.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")
        return job
