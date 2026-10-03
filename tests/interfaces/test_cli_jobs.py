"""Public one-time job commands use durable workflow state."""

import hashlib
import asyncio
import sqlite3

from ai import AIExecutionResult
from capabilities.developer.workspace import build_workspace_tools

from interfaces.cli.commands import CommandContext, handle_command
from workflows.models import ApprovalStatus, JobStatus
from workflows.runtime.context import ApprovalRequired
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


async def test_public_approval_allows_only_the_exact_write_and_shows_context(tmp_path, capsys):
    store = JobStore(tmp_path / "jobs.db")
    handle_command(context(store), f'/run --workspace "{tmp_path}" --allow-write "write note"')
    job = store.list_jobs()[0]
    capsys.readouterr()

    async def execute(prompt, execution_context, change_event_callback, **kwargs):
        write = build_workspace_tools(execution_context, change_event_callback)["apply_workspace_patch"]
        try:
            result = write("note.txt", "approved content\n")
        except ApprovalRequired as error:
            return AIExecutionResult(str(error), "waiting_approval", str(error), 4, 1, 0)
        return AIExecutionResult(result, "completed", None, 4, 1, 0)

    runner = JobRunner(store, execute=execute)
    first = store.claim_next_job()
    await runner.run(first)
    approval = store.latest_approval(job.id)
    assert approval.status == ApprovalStatus.PENDING
    assert not (tmp_path / "note.txt").exists()

    handle_command(context(store), "/approvals")
    handle_command(context(store), f"/approval show {approval.id}")
    output = capsys.readouterr().out
    assert approval.id in output
    assert job.id in output and first.attempt_id in output
    assert "replace workspace file note.txt" in output
    assert "Tool: apply_workspace_patch" in output
    assert f"Workspace: {tmp_path}" in output
    assert "Permission requested: write approval" in output
    assert "Created:" in output and "Expires:" in output
    assert "Status: pending" in output

    worker = AutomationWorker(store, runner)
    handle_command(context(store, worker), f"/approval allow {approval.id}")
    assert store.get_approval(approval.id).status == ApprovalStatus.APPROVED
    assert store.get_job(job.id).status == JobStatus.QUEUED
    assert worker._wake.is_set()
    assert "Decision: approved" in capsys.readouterr().out
    second = store.claim_next_job()
    await runner.run(second)
    assert (tmp_path / "note.txt").read_text() == "approved content\n"
    assert store.get_approval(approval.id).status == ApprovalStatus.CONSUMED


def test_public_approval_deny_cancel_and_duplicate_fail_closed(tmp_path, capsys):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("write note", workspace=str(tmp_path), allow_write=True)
    store.claim_next_job()
    _, approval = store.request_or_consume_approval(
        job.id, "write", {"tool": "apply_workspace_patch", "path": "note.txt"},
        "replace workspace file note.txt api_key=example-secret-value", "api_key=example-secret-value",
    )
    handle_command(context(store), f"/approval deny {approval.id}")
    assert store.get_approval(approval.id).status == ApprovalStatus.REJECTED
    assert store.get_job(job.id).status == JobStatus.BLOCKED
    handle_command(context(store), f"/approval allow {approval.id}")
    handle_command(context(store), f"/approval deny {approval.id}")
    assert store.get_job(job.id).status == JobStatus.BLOCKED
    assert not (tmp_path / "note.txt").exists()

    other = store.create_job("write later", workspace=str(tmp_path), allow_write=True)
    store.claim_next_job()
    _, pending = store.request_or_consume_approval(
        other.id, "write", {"path": "note.txt"}, "replace workspace file note.txt", "",
    )
    handle_command(context(store), f"/cancel {other.id}")
    handle_command(context(store), f"/approval allow {pending.id}")
    handle_command(context(store), "/approval allow missing")
    handle_command(context(store), "/approval allow")
    output = capsys.readouterr().out
    assert "job is not waiting for approval" in output
    assert "Approval not found." in output
    assert "usage: /approval show|allow|deny <approval_id>" in output
    assert "example-secret-value" not in output
    assert store.get_approval(pending.id).status == ApprovalStatus.INVALIDATED
    assert store.get_job(other.id).status == JobStatus.CANCELLED
    assert not (tmp_path / "note.txt").exists()


def test_public_approval_expiry_and_stale_id_cannot_approve_new_request(tmp_path, capsys):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("write note", workspace=str(tmp_path), allow_write=True)
    store.claim_next_job()
    action = {"tool": "apply_workspace_patch", "path": "note.txt"}
    _, expired = store.request_or_consume_approval(
        job.id, "write", action, "replace workspace file note.txt", "",
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE approval_requests SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", expired.id),
        )
    handle_command(context(store), f"/approval allow {expired.id}")
    assert store.get_approval(expired.id).status == ApprovalStatus.EXPIRED
    assert store.get_job(job.id).status == JobStatus.QUEUED
    assert "expired" in capsys.readouterr().out
    assert not (tmp_path / "note.txt").exists()

    store.claim_next_job()
    authorized, fresh = store.request_or_consume_approval(
        job.id, "write", action, "replace workspace file note.txt", "",
    )
    assert not authorized and fresh.id != expired.id
    handle_command(context(store), f"/approval allow {expired.id}")
    assert "no longer pending" in capsys.readouterr().out
    assert store.get_approval(fresh.id).status == ApprovalStatus.PENDING
    assert store.get_job(job.id).status == JobStatus.WAITING_APPROVAL
    assert not (tmp_path / "note.txt").exists()


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
