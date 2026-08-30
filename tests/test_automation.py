import asyncio

from ai import AIExecutionResult
from automation.models import JobStatus
from automation.runner import JobRunner
from automation.store import JobStore
from automation.worker import AutomationWorker


def test_job_store_persists_lifecycle_and_events(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    created = store.create_job("prepare report")

    assert created.status == JobStatus.QUEUED
    claimed = store.claim_next_job()
    assert claimed is not None
    assert claimed.id == created.id
    assert claimed.status == JobStatus.RUNNING
    assert store.claim_next_job() is None

    store.add_event(
        created.id,
        {
            "activity": "model",
            "detail": "waiting for AI",
            "elapsed_seconds": 2.5,
            "total_tokens": 100,
        },
    )
    assert store.latest_event(created.id).detail == "waiting for AI"

    assert store.complete_job(created.id, "report", 80, 20)
    completed = store.get_job(created.id)
    assert completed.status == JobStatus.COMPLETED
    assert completed.result == "report"
    assert completed.total_tokens == 100

    reopened = JobStore(tmp_path / "suto.db")
    assert reopened.get_job(created.id) == completed


def test_store_recovers_interrupted_jobs_as_failed(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("long task")
    store.claim_next_job()

    assert store.recover_interrupted_jobs() == 1
    recovered = store.get_job(job.id)
    assert recovered.status == JobStatus.FAILED
    assert recovered.error == "worker stopped before job completed"


async def test_worker_runs_job_and_records_progress(tmp_path):
    store = JobStore(tmp_path / "suto.db")

    async def execute(prompt, mode, progress_callback):
        progress_callback(
            {
                "activity": "model",
                "detail": "working",
                "elapsed_seconds": 1,
                "total_tokens": 25,
            }
        )
        return AIExecutionResult(
            text=f"finished: {prompt}",
            status="completed",
            error=None,
            prompt_tokens=20,
            output_tokens=5,
            elapsed_seconds=1,
        )

    worker = AutomationWorker(
        store,
        JobRunner(store, execute=execute),
        poll_interval=0.01,
    )
    worker_task = asyncio.create_task(worker.start())
    job = worker.submit("prepare report")

    async def wait_until_completed():
        while store.get_job(job.id).status != JobStatus.COMPLETED:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_until_completed(), timeout=1)
    await worker.stop()
    await worker_task

    completed = store.get_job(job.id)
    assert completed.result == "finished: prepare report"
    assert completed.total_tokens == 25
    assert store.latest_event(job.id).detail == "working"


async def test_worker_cancels_running_job(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    started = asyncio.Event()

    async def execute(prompt, mode, progress_callback):
        started.set()
        await asyncio.Event().wait()

    worker = AutomationWorker(
        store,
        JobRunner(store, execute=execute),
        poll_interval=0.01,
    )
    worker_task = asyncio.create_task(worker.start())
    job = worker.submit("wait forever")
    await asyncio.wait_for(started.wait(), timeout=1)

    assert await worker.cancel(job.id)

    async def wait_until_cancelled():
        while store.get_job(job.id).status != JobStatus.CANCELLED:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_until_cancelled(), timeout=1)
    await worker.stop()
    await worker_task

    assert store.get_job(job.id).status == JobStatus.CANCELLED
