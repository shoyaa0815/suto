"""Public one-time job commands use durable workflow state."""

import hashlib
import asyncio

from interfaces.cli.commands import CommandContext, handle_command
from workflows.models import ApprovalStatus, JobStatus
from workflows.runtime.runner import JobRunner
from workflows.runtime.worker import AutomationWorker
from workflows.storage.store import JobStore


class FakeWorker:
    def __init__(self, store):
        self.store = store
        self.ready = True
        self.wakes = 0
        self.cancellations = 0

    def wake(self):
        self.wakes += 1

    def request_cancel(self, job_id):
        self.cancellations += 1
        return self.store.cancel_job(job_id)

    def resume(self, job_id):
        resumed = self.store.resume_job(job_id)
        if resumed:
            self.wake()
        return resumed


def context(store, worker=None):
    return CommandContext(store, None, "conversation", "agent", worker=worker)


def test_run_persists_canonical_workspace_permissions_and_reports_worker_state(tmp_path, capsys):
    store = JobStore(tmp_path / "jobs.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    worker = FakeWorker(store)

    assert handle_command(
        context(store, worker),
        f'/run --workspace "{alias}" --allow-write "inspect the repo"',
    ).handled
    job = store.list_jobs()[0]
    output = capsys.readouterr().out
    assert job.workspace == str(workspace)
    assert job.prompt == "inspect the repo"
    assert job.status == JobStatus.QUEUED
    assert job.allow_write and not job.allow_command
    assert job.attempt_id is None
    assert worker.wakes == 1
    assert f"ID: {job.id}" in output
    assert "Job queued." in output

    assert handle_command(context(store), '/run "second task"').handled
    second = store.list_jobs()[0]
    assert second.id != job.id
    assert second.workspace != ""
    assert "Job saved and waiting for worker." in capsys.readouterr().out


def test_run_rejects_invalid_input_and_quota_without_persisting(tmp_path, capsys):
    store = JobStore(tmp_path / "jobs.db")
    for command in (
        "/run",
        "/run --workspace",
        "/run --allow-write",
        "/run --unknown inspect",
        "/run --workspace missing inspect",
        '/run "unterminated',
    ):
        assert handle_command(context(store), command).handled
    assert store.list_jobs() == []
    assert "Cannot create job:" in capsys.readouterr().out

    store.configure(max_queued_jobs=1)
    handle_command(context(store), "/run first")
    handle_command(context(store), "/run second")
    assert len(store.list_jobs()) == 1
    assert "quota reached" in capsys.readouterr().out


def test_public_run_uses_existing_prompt_redaction(tmp_path, capsys):
    store = JobStore(tmp_path / "jobs.db")
    handle_command(context(store), "/run inspect api_key=example-secret-value")
    job = JobStore(store.path).list_jobs()[0]
    assert "example-secret-value" not in job.prompt
    handle_command(context(store), f"/status {job.id}")
    assert "example-secret-value" not in capsys.readouterr().out


def test_jobs_and_status_show_attempt_and_persisted_result(tmp_path, capsys):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("inspect architecture", workspace=str(tmp_path))
    first = store.claim_next_job()
    assert store.complete_job(job.id, "architecture summary", 3, 4)

    handle_command(context(store), "/jobs")
    handle_command(context(store), f"/status {job.id}")
    output = capsys.readouterr().out
    assert f"{job.id}  completed  inspect architecture" in output
    assert f"latest_attempt={first.attempt_id}" in output
    assert "created=" in output
    assert f"Attempt: 1 ({first.attempt_id})" in output
    assert "Result summary: architecture summary" in output
    assert "Started: none" not in output
    assert "Finished: none" not in output
    assert "Automation version: none" in output
    assert "Schedule trigger: none" in output


def test_cancel_is_idempotent_and_resume_reuses_job_with_new_attempt(tmp_path, capsys):
    store = JobStore(tmp_path / "jobs.db")
    worker = FakeWorker(store)
    queued = store.create_job("cancel queued")
    handle_command(context(store, worker), f"/cancel {queued.id}")
    handle_command(context(store, worker), f"/cancel {queued.id}")
    assert worker.cancellations == 1
    assert store.get_job(queued.id).status == JobStatus.CANCELLED
    assert store.list_job_attempts(queued.id) == []

    job = store.create_job("resume later", workspace=str(tmp_path))
    first = store.claim_next_job()
    assert store.interrupt_job(job.id, "restart")
    handle_command(context(store, worker), f"/resume {job.id}")
    queued_again = store.get_job(job.id)
    assert queued_again.status == JobStatus.QUEUED
    assert queued_again.attempt_id == first.attempt_id
    assert len(store.list_job_attempts(job.id)) == 1
    second = store.claim_next_job()
    assert second.id == job.id
    assert second.attempt_id != first.attempt_id
    assert [item.ordinal for item in store.list_job_attempts(job.id)] == [1, 2]
    assert worker.wakes == 1
    assert "Job queued." in capsys.readouterr().out
    assert [entry["event_type"] for entry in store.logs(job.id)] == [
        "queued", "running", "interrupted", "queued", "running"
    ]


def test_resume_rejects_changed_checkpoint_and_invalid_transitions(tmp_path, capsys):
    store = JobStore(tmp_path / "jobs.db")
    target = tmp_path / "file.txt"
    target.write_text("expected", encoding="utf-8")
    job = store.create_job("continue", workspace=str(tmp_path))
    store.claim_next_job()
    store.add_change_event(job.id, {
        "path": "file.txt", "diff": "change",
        "after_sha256": hashlib.sha256(b"expected").hexdigest(),
    })
    assert store.interrupt_job(job.id, "restart")
    target.write_text("external change", encoding="utf-8")
    handle_command(context(store), f"/resume {job.id}")
    assert store.get_job(job.id).status == JobStatus.INTERRUPTED
    assert len(store.list_job_attempts(job.id)) == 1
    assert "changed after checkpoint" in capsys.readouterr().out

    handle_command(context(store), f"/status {job.id} extra")
    handle_command(context(store), "/cancel missing")
    handle_command(context(store), f"/cancel {job.id}")
    handle_command(context(store), f"/resume {job.id}")
    output = capsys.readouterr().out
    assert "usage: /status <job_id>" in output
    assert "Job not found: missing" in output
    assert store.get_job(job.id).status == JobStatus.CANCELLED
    assert "cannot be resumed from cancelled" in output


def test_cancel_waiting_approval_invalidates_request(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("edit", allow_write=True)
    store.claim_next_job()
    authorized, request = store.request_or_consume_approval(
        job.id, "write", {"path": "file.txt"}, "edit file", "preview",
        ttl_seconds=600,
    )
    assert not authorized
    assert store.get_job(job.id).status == JobStatus.WAITING_APPROVAL

    handle_command(context(store), f"/cancel {job.id}")
    reopened = JobStore(store.path)
    assert reopened.get_job(job.id).status == JobStatus.CANCELLED
    assert reopened.latest_approval(job.id).id == request.id
    assert reopened.latest_approval(job.id).status == ApprovalStatus.INVALIDATED


async def test_public_cancel_stops_running_worker_task(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def execute(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    worker = AutomationWorker(store, JobRunner(store, execute=execute), poll_interval=0.01)
    worker_task = asyncio.create_task(worker.start())
    try:
        handle_command(context(store, worker), "/run wait forever")
        job = store.list_jobs()[0]
        await asyncio.wait_for(started.wait(), timeout=2)
        handle_command(context(store, worker), f"/cancel {job.id}")
        await asyncio.wait_for(stopped.wait(), timeout=2)
        assert JobStore(store.path).get_job(job.id).status == JobStatus.CANCELLED
        assert store.list_job_attempts(job.id)[0].status == JobStatus.CANCELLED
    finally:
        await worker.stop()
        await worker_task
