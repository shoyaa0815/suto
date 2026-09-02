import asyncio
import sqlite3

from ai import AIExecutionResult
from automation.context import WRITE_WORKSPACE_TOOLS
from automation.models import JobStatus
from automation.runner import JobRunner
from automation.store import JobStore
from automation.worker import AutomationWorker


def test_job_store_persists_lifecycle_and_events(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    created = store.create_job("prepare report")

    assert created.status == JobStatus.QUEUED
    assert created.workspace == "."
    assert created.allow_write is False
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


def test_job_store_records_file_changes(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("update docs", allow_write=True)

    store.add_change_event(
        job.id,
        {
            "path": "README.md",
            "diff": "--- a/README.md\n+++ b/README.md\n",
            "before_sha256": "before",
            "after_sha256": "after",
        },
    )

    change = store.list_change_events(job.id)[0]
    assert job.allow_write is True
    assert change.path == "README.md"
    assert change.before_sha256 == "before"
    assert change.after_sha256 == "after"


def test_store_recovers_interrupted_jobs_as_failed(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("long task")
    store.claim_next_job()

    assert store.recover_interrupted_jobs() == 1
    recovered = store.get_job(job.id)
    assert recovered.status == JobStatus.FAILED
    assert recovered.error == "worker stopped before job completed"


def test_store_migrates_legacy_jobs_with_workspace_default(tmp_path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                prompt TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                source_ref TEXT,
                result TEXT,
                error TEXT,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            )
            """
        )

    store = JobStore(database)
    job = store.create_job("migrated")

    assert job.workspace == "."
    assert job.allow_write is False


async def test_worker_runs_job_and_records_progress(tmp_path):
    store = JobStore(tmp_path / "suto.db")

    async def execute(
        prompt,
        mode,
        progress_callback,
        execution_context,
        tool_event_callback,
        change_event_callback,
    ):
        assert execution_context.workspace == tmp_path.resolve()
        assert WRITE_WORKSPACE_TOOLS <= execution_context.allowed_tools
        progress_callback(
            {
                "activity": "model",
                "detail": "working",
                "elapsed_seconds": 1,
                "total_tokens": 25,
            }
        )
        tool_event_callback(
            {
                "tool_name": "read_workspace_file",
                "arguments": {"path": "README.md"},
                "status": "finished",
                "elapsed_seconds": 0.1,
                "result_size": 20,
                "error": None,
            }
        )
        change_event_callback(
            {
                "path": "README.md",
                "diff": "--- a/README.md\n+++ b/README.md\n",
                "before_sha256": "before",
                "after_sha256": "after",
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
    job = worker.submit(
        "prepare report",
        workspace=tmp_path,
        allow_write=True,
    )

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
    tool_event = store.latest_tool_event(job.id)
    assert tool_event.tool_name == "read_workspace_file"
    assert tool_event.status == "finished"
    assert store.list_change_events(job.id)[0].path == "README.md"


async def test_worker_cancels_running_job(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    started = asyncio.Event()

    async def execute(
        prompt,
        mode,
        progress_callback,
        execution_context,
        tool_event_callback,
        change_event_callback,
    ):
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
