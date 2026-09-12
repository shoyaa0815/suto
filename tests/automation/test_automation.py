import asyncio
import hashlib
import sqlite3

from ai import AIExecutionResult
from automation.runtime.context import COMMAND_TOOLS, PLANNING_TOOLS, WRITE_WORKSPACE_TOOLS
from automation.models import JobStatus, StepStatus
from automation.runtime.runner import JobRunner
from automation.storage.store import JobStore
from automation.runtime.worker import AutomationWorker


def test_job_store_persists_lifecycle_and_events(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    created = store.create_job("prepare report")

    assert created.status == JobStatus.QUEUED
    assert created.workspace == "."
    assert created.allow_write is False
    assert created.allow_command is False
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
    assert job.allow_command is False
    assert change.path == "README.md"
    assert change.before_sha256 == "before"
    assert change.after_sha256 == "after"


def test_job_store_records_command_output(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("run checks", allow_command=True)
    store.add_command_event(
        job.id,
        {
            "command": ["pytest", "-q"],
            "status": "completed",
            "exit_code": 0,
            "stdout": "3 passed",
            "stderr": "",
            "elapsed_seconds": 0.5,
        },
    )

    event = store.latest_command_event(job.id)
    assert job.allow_command is True
    assert event.command == '["pytest", "-q"]'
    assert event.exit_code == 0
    assert event.stdout == "3 passed"


def test_job_store_persists_plan_and_step_progress(tmp_path):
    database = tmp_path / "suto.db"
    store = JobStore(database)
    job = store.create_job("inspect and update docs")
    store.claim_next_job()

    steps = store.create_plan(job.id, ["Inspect files", "Update documentation"])
    assert [step.position for step in steps] == [1, 2]
    assert all(step.status == StepStatus.PENDING for step in steps)

    active = store.update_step(job.id, 1, StepStatus.IN_PROGRESS)
    assert active.status == StepStatus.IN_PROGRESS
    assert store.current_step(job.id) == active

    completed = store.update_step(
        job.id,
        1,
        StepStatus.COMPLETED,
        "README inspected",
    )
    assert completed.result == "README inspected"
    assert store.current_step(job.id).position == 2

    reopened = JobStore(database)
    persisted = reopened.list_steps(job.id)
    assert persisted[0].status == StepStatus.COMPLETED
    assert persisted[1].status == StepStatus.PENDING


def test_job_store_revises_plan_atomically(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("update project")
    store.claim_next_job()
    store.create_plan(job.id, ["Old first step", "Old second step"])

    revised = store.revise_plan(job.id, ["Inspect configuration", "Run checks"])

    assert [step.description for step in revised] == [
        "Inspect configuration",
        "Run checks",
    ]
    assert all(step.status == StepStatus.PENDING for step in revised)


def test_job_store_rejects_second_plan_without_revision(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("update project")
    store.claim_next_job()
    store.create_plan(job.id, ["Inspect project"])

    try:
        store.create_plan(job.id, ["Replace project"])
    except ValueError as error:
        assert "already exists" in str(error)
    else:
        raise AssertionError("an existing plan was silently replaced")


def test_completed_plan_step_cannot_be_reopened(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("update project")
    store.claim_next_job()
    store.create_plan(job.id, ["Inspect project"])
    store.update_step(job.id, 1, StepStatus.COMPLETED, "done")

    try:
        store.update_step(job.id, 1, StepStatus.IN_PROGRESS)
    except ValueError as error:
        assert "cannot be reopened" in str(error)
    else:
        raise AssertionError("a completed checkpoint step was reopened")


def test_store_recovers_interrupted_jobs_for_safe_resume(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("long task")
    store.claim_next_job()

    assert store.recover_interrupted_jobs() == 1
    recovered = store.get_job(job.id)
    assert recovered.status == JobStatus.INTERRUPTED
    assert "safe resume is available" in recovered.error


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
    assert job.allow_command is False
    assert job.attempt_count == 0
    assert job.retry_count == 0


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
        assert PLANNING_TOOLS <= execution_context.allowed_tools
        assert COMMAND_TOOLS <= execution_context.allowed_tools
        assert execution_context.plan_store is store
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
        execution_context.command_event_callback(
            {
                "command": ["pytest", "-q"],
                "status": "completed",
                "exit_code": 0,
                "stdout": "1 passed",
                "stderr": "",
                "elapsed_seconds": 0.1,
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
        allow_command=True,
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


def test_job_store_blocks_job_with_persistent_reason(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("unsafe loop")
    store.claim_next_job()

    assert store.block_job(job.id, "tool loop detected", 20, 5)

    blocked = store.get_job(job.id)
    assert blocked.status == JobStatus.BLOCKED
    assert blocked.error == "tool loop detected"
    assert blocked.total_tokens == 25


def test_verification_must_succeed_after_latest_change(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("update code", allow_write=True, allow_command=True)
    store.add_change_event(
        job.id,
        {
            "path": "app.py",
            "diff": "changed",
            "before_sha256": "before",
            "after_sha256": "after",
        },
    )

    assert not store.has_successful_verification_after_last_change(job.id)

    store.add_command_event(
        job.id,
        {
            "command": ["git", "status", "--short"],
            "status": "completed",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "elapsed_seconds": 0.1,
        },
    )
    assert not store.has_successful_verification_after_last_change(job.id)

    store.add_command_event(
        job.id,
        {
            "command": ["pytest", "-q"],
            "status": "completed",
            "exit_code": 0,
            "stdout": "1 passed",
            "stderr": "",
            "elapsed_seconds": 0.1,
        },
    )
    assert store.has_successful_verification_after_last_change(job.id)


async def test_runner_blocks_unverified_workspace_changes(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("change code", workspace=str(tmp_path), allow_write=True)
    claimed = store.claim_next_job()

    async def execute(prompt, **kwargs):
        kwargs["change_event_callback"](
            {
                "path": "app.py",
                "diff": "changed",
                "before_sha256": "before",
                "after_sha256": "after",
            }
        )
        return AIExecutionResult("done", "completed", None, 10, 2, 0.1)

    await JobRunner(store, execute=execute).run(claimed)

    blocked = store.get_job(job.id)
    assert blocked.status == JobStatus.BLOCKED
    assert "not followed by a successful" in blocked.error


async def test_runner_retries_transient_failure_without_workspace_changes(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("inspect project", workspace=str(tmp_path))
    claimed = store.claim_next_job()
    prompts = []

    async def execute(prompt, **kwargs):
        prompts.append(prompt)
        if len(prompts) == 1:
            return AIExecutionResult(
                "try again",
                "timed_out",
                "AI processing timed out",
                10,
                2,
                1,
            )
        return AIExecutionResult("done", "completed", None, 5, 1, 1)

    await JobRunner(store, execute=execute, retry_delays=(0,)).run(claimed)

    completed = store.get_job(job.id)
    assert completed.status == JobStatus.COMPLETED
    assert completed.retry_count == 1
    assert completed.total_tokens == 18
    assert len(prompts) == 2
    assert "Resume checkpoint from an earlier attempt" in prompts[1]


async def test_runner_does_not_retry_after_workspace_change(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("change project", workspace=str(tmp_path), allow_write=True)
    claimed = store.claim_next_job()
    calls = 0

    async def execute(prompt, change_event_callback, **kwargs):
        nonlocal calls
        calls += 1
        change_event_callback(
            {
                "path": "app.py",
                "diff": "changed",
                "before_sha256": "before",
                "after_sha256": "after",
            }
        )
        return AIExecutionResult("timeout", "timed_out", "timeout", 1, 1, 1)

    await JobRunner(store, execute=execute, retry_delays=(0,)).run(claimed)

    failed = store.get_job(job.id)
    assert failed.status == JobStatus.FAILED
    assert failed.retry_count == 0
    assert calls == 1


async def test_interrupted_job_resumes_from_persisted_plan(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    target = tmp_path / "app.py"
    target.write_text("checkpoint\n", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    job = store.create_job(
        "finish update",
        workspace=str(tmp_path),
        allow_write=True,
        allow_command=True,
    )
    store.claim_next_job()
    store.create_plan(job.id, ["Inspect", "Verify"])
    store.update_step(job.id, 1, StepStatus.COMPLETED, "inspection done")
    store.update_step(job.id, 2, StepStatus.IN_PROGRESS)
    store.add_change_event(
        job.id,
        {
            "path": "app.py",
            "diff": "changed",
            "before_sha256": "before",
            "after_sha256": digest,
        },
    )
    store.recover_interrupted_jobs()

    assert store.list_steps(job.id)[1].status == StepStatus.PENDING
    assert store.resume_job(job.id)
    resumed = store.claim_next_job()
    observed = {}

    async def execute(prompt, execution_context, **kwargs):
        observed["prompt"] = prompt
        execution_context.plan_store.update_step(
            job.id,
            2,
            StepStatus.COMPLETED,
            "verification passed",
        )
        execution_context.command_event_callback(
            {
                "command": ["pytest", "-q"],
                "status": "completed",
                "exit_code": 0,
                "stdout": "1 passed",
                "stderr": "",
                "elapsed_seconds": 0.1,
            }
        )
        return AIExecutionResult("done", "completed", None, 5, 1, 1)

    await JobRunner(store, execute=execute).run(resumed)

    completed = store.get_job(job.id)
    assert completed.status == JobStatus.COMPLETED
    assert completed.attempt_count == 2
    assert "Step 1 [completed]: Inspect" in observed["prompt"]
    assert "Step 2 [pending]: Verify" in observed["prompt"]


async def test_resume_blocks_when_checkpoint_file_changed_externally(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    target = tmp_path / "app.py"
    target.write_text("checkpoint\n", encoding="utf-8")
    job = store.create_job("continue", workspace=str(tmp_path))
    store.claim_next_job()
    store.add_change_event(
        job.id,
        {
            "path": "app.py",
            "diff": "changed",
            "before_sha256": "before",
            "after_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        },
    )
    store.recover_interrupted_jobs()
    target.write_text("external change\n", encoding="utf-8")
    store.resume_job(job.id)
    resumed = store.claim_next_job()
    called = False

    async def execute(prompt, **kwargs):
        nonlocal called
        called = True

    await JobRunner(store, execute=execute).run(resumed)

    blocked = store.get_job(job.id)
    assert blocked.status == JobStatus.BLOCKED
    assert "changed after checkpoint" in blocked.error
    assert called is False


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


async def test_worker_shutdown_marks_running_job_interrupted(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    started = asyncio.Event()

    async def execute(prompt, **kwargs):
        started.set()
        await asyncio.Event().wait()

    worker = AutomationWorker(
        store,
        JobRunner(store, execute=execute),
        poll_interval=0.01,
    )
    worker_task = asyncio.create_task(worker.start())
    job = worker.submit("wait for restart", workspace=tmp_path)
    await asyncio.wait_for(started.wait(), timeout=1)

    await worker.stop()
    await worker_task

    interrupted = store.get_job(job.id)
    assert interrupted.status == JobStatus.INTERRUPTED
    assert "safe resume is available" in interrupted.error


async def test_runner_rejects_completed_result_with_unfinished_plan(tmp_path):
    store = JobStore(tmp_path / "suto.db")

    async def execute(
        prompt,
        mode,
        progress_callback,
        execution_context,
        tool_event_callback,
        change_event_callback,
    ):
        execution_context.plan_store.create_plan(
            execution_context.job_id,
            ["Inspect project", "Write report"],
        )
        execution_context.plan_store.update_step(
            execution_context.job_id,
            1,
            StepStatus.COMPLETED,
            "inspection complete",
        )
        return AIExecutionResult(
            text="done",
            status="completed",
            error=None,
            prompt_tokens=10,
            output_tokens=5,
            elapsed_seconds=1,
        )

    job = store.create_job("inspect and report")
    claimed = store.claim_next_job()
    await JobRunner(store, execute=execute).run(claimed)

    failed = store.get_job(job.id)
    assert failed.status == JobStatus.FAILED
    assert "unfinished steps: 2" in failed.error
    assert failed.total_tokens == 15
