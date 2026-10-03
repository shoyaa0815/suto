"""Application boundary for public automation job reads."""

from workflows.models import Job
from workflows.storage.store import JobStore


class JobService:
    def __init__(self, store: JobStore) -> None:
        self.store = store

    def list_recent(self) -> list[Job]:
        return self.store.list_jobs()
