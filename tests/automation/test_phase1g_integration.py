"""Public automation paths through the worker and real agent runtime."""

import asyncio
import json
from datetime import UTC, datetime

import ai
import aiohttp
import pytest

from application.automation import AutomationService, JobService
from workflows.errors import ErrorCode
from interfaces.cli.commands import CommandContext, handle_command
from tests.support.ai_helpers import FakeClientSession, patch_model_chat
from workflows.models import ApprovalStatus, JobStatus
from workflows.runtime import scheduler as scheduler_module
from workflows.runtime.runner import JobRunner
from workflows.runtime.scheduler import Scheduler
from workflows.runtime.worker import AutomationWorker
from workflows.storage.store import JobStore


def _context(store, worker=None):
    return CommandContext(store, None, "conversation", "agent", worker=worker)


def _fake_provider(monkeypatch, chat):
    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", chat)
    patch_model_chat(monkeypatch)


async def _run_worker_until(store, count, *, retry_delays=None):
    options = {"retry_delays": retry_delays} if retry_delays is not None else {}
    runner = JobRunner(store, **options)
    real_run = runner.run
    finished = asyncio.Event()
    completed = 0

    async def observed_run(job):
        nonlocal completed
        await real_run(job)
        completed += 1
        if completed == count:
            finished.set()

    runner.run = observed_run
    worker = AutomationWorker(store, runner, poll_interval=0.01)
    task = asyncio.create_task(worker.start())
    try:
        await asyncio.wait_for(finished.wait(), timeout=5)
    finally:
        await worker.stop()
        await task


async def test_cli_automation_runs_pinned_versions_through_runtime_after_restart(
    tmp_path, monkeypatch, capsys,
):
    store = JobStore(tmp_path / "jobs.db")
    definition = tmp_path / "definition.json"
    observed = []

    async def chat(session, messages, schemas, think=False):
        observed.append([dict(message) for message in messages])
        return {"message": {"content": "review complete"}}

    _fake_provider(monkeypatch, chat)
    definition.write_text(json.dumps({
        "name": "review", "prompt_template": "Review {{repo}} with version one.",
        "parameter_schema": {"repo": {"type": "string", "required": True}},
        "workspace": str(tmp_path),
    }), encoding="utf-8")
    handle_command(_context(store), f"/automation create {definition}")
    handle_command(_context(store), "/automation run review repo=first")
    first = store.list_jobs()[0]
    definition.write_text(json.dumps({
        "name": "review", "prompt_template": "Review {{repo}} with version two.",
        "parameter_schema": {"repo": {"type": "string", "required": True}},
        "workspace": str(tmp_path),
    }), encoding="utf-8")
    handle_command(_context(store), f"/automation update review {definition}")
    handle_command(_context(store), "/automation run review repo=second")
    second = store.list_jobs()[0]
    assert first.id != second.id

    reopened = JobStore(store.path)
    await _run_worker_until(reopened, 2)
    persisted = JobStore(store.path)
    assert [persisted.get_job(job.id).status for job in (first, second)] == [
        JobStatus.COMPLETED, JobStatus.COMPLETED,
    ]
    assert [persisted.get_automation_version(job.source_ref).version for job in (first, second)] == [1, 2]
    assert persisted.get_automation_run_parameters(first.id) == {"repo": "first"}
    assert persisted.get_automation_run_parameters(second.id) == {"repo": "second"}
    assert any("Review first with version one." in str(messages) for messages in observed)
    assert any("Review second with version two." in str(messages) for messages in observed)
    assert all(persisted.list_job_attempts(job.id)[0].status == JobStatus.COMPLETED for job in (first, second))
    handle_command(_context(persisted), "/automation history review")
    assert "version=1" in capsys.readouterr().out


@pytest.mark.parametrize("command", [
    "/schedule create --at 2030-01-01T08:00:00+07:00 inspect",
    "/schedule create --every 60 inspect",
    '/schedule create --cron "0 8 * * *" --timezone Asia/Bangkok inspect',
])
async def test_cli_schedule_occurrence_runs_once_through_runtime_after_restart(
    tmp_path, monkeypatch, command,
):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 1, 1, tzinfo=UTC)
            return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(scheduler_module, "datetime", FixedDatetime)
    store = JobStore(tmp_path / "jobs.db")
    calls = []

    async def chat(session, messages, schemas, think=False):
        calls.append(messages)
        return {"message": {"content": "inspection complete"}}

    _fake_provider(monkeypatch, chat)
    handle_command(_context(store), command)
    schedule = JobStore(store.path).list_schedules()[0]
    due = datetime.fromisoformat(schedule.next_run_at).astimezone(UTC)
    assert Scheduler(JobStore(store.path)).tick(due) == 1
    assert Scheduler(JobStore(store.path)).tick(due) == 0

    reopened = JobStore(store.path)
    await _run_worker_until(reopened, 1)
    persisted = JobStore(store.path)
    history = persisted.list_trigger_history(schedule.id)
    assert len(history) == 1
    assert [job.id for job in persisted.list_jobs()] == [history[0].job_id]
    assert persisted.get_job(history[0].job_id).status == JobStatus.COMPLETED
    assert persisted.list_job_attempts(history[0].job_id)[0].status == JobStatus.COMPLETED
    assert "inspect" in str(calls[0])
    assert len(persisted.list_job_attempts(history[0].job_id)) == 1


async def test_cli_job_retries_transient_provider_failure_with_one_logical_job(
    tmp_path, monkeypatch,
):
    store = JobStore(tmp_path / "jobs.db")
    requests = 0

    async def chat(session, messages, schemas, think=False):
        nonlocal requests
        requests += 1
        if requests <= 2:
            raise aiohttp.ClientConnectorError(None, OSError(111, "connection refused"))
        return {"message": {"content": "recovered"}}

    _fake_provider(monkeypatch, chat)
    handle_command(_context(store), f'/run --workspace "{tmp_path}" "inspect after outage"')
    job = store.list_jobs()[0]
    await _run_worker_until(JobStore(store.path), 1, retry_delays=(0, 0))
    persisted = JobStore(store.path)
    completed = persisted.get_job(job.id)
    assert completed.status == JobStatus.COMPLETED
    assert completed.retry_count == 2
    assert completed.result == "recovered"
    assert len(persisted.list_jobs()) == 1
    assert len(persisted.list_job_attempts(job.id)) == 1


async def test_cli_write_request_waits_for_approval_and_denial_has_no_effect(
    tmp_path, monkeypatch,
):
    store = JobStore(tmp_path / "jobs.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "note.txt"

    async def chat(session, messages, schemas, think=False):
        return {"message": {"content": "", "tool_calls": [{"function": {
            "name": "apply_workspace_patch",
            "arguments": {"path": "note.txt", "content": "unapproved\n"},
        }}]}}

    _fake_provider(monkeypatch, chat)
    handle_command(_context(store), f'/run --workspace "{workspace}" --allow-write "write note"')
    job = store.list_jobs()[0]
    await _run_worker_until(JobStore(store.path), 1)
    persisted = JobStore(store.path)
    approval = persisted.latest_approval(job.id)
    assert approval is not None and approval.status == ApprovalStatus.PENDING
    assert persisted.get_job(job.id).status == JobStatus.WAITING_APPROVAL
    assert not target.exists()
    handle_command(_context(persisted), f"/approval deny {approval.id}")
    assert JobStore(store.path).get_job(job.id).status == JobStatus.BLOCKED
    assert JobStore(store.path).get_approval(approval.id).status == ApprovalStatus.REJECTED
    assert not target.exists()


async def test_cli_default_read_only_blocks_runtime_write_tool(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs.db")
    target = tmp_path / "note.txt"

    async def chat(session, messages, schemas, think=False):
        return {"message": {"content": "", "tool_calls": [{"function": {
            "name": "apply_workspace_patch",
            "arguments": {"path": "note.txt", "content": "unapproved\n"},
        }}]}}

    _fake_provider(monkeypatch, chat)
    handle_command(_context(store), f'/run --workspace "{tmp_path}" "write note"')
    job = store.list_jobs()[0]
    await _run_worker_until(JobStore(store.path), 1)
    persisted = JobStore(store.path)
    assert persisted.get_job(job.id).status == JobStatus.BLOCKED
    assert persisted.latest_approval(job.id) is None
    assert not target.exists()


@pytest.mark.parametrize("source", ["run", "automation", "schedule", "scheduled_automation"])
async def test_queued_job_cannot_rebind_pinned_workspace_after_restart(tmp_path, source):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("outside data")
    store = JobStore(tmp_path / "jobs.db")
    if source in {"automation", "scheduled_automation"}:
        store.create_automation("inspect", "inspect files", workspace)
    if source == "run":
        job = JobService(store).submit("inspect files", workspace=workspace)
    elif source == "automation":
        job, _ = AutomationService(store).run("inspect", {})
    else:
        scheduler = Scheduler(store)
        options = dict(kind="once", expression="2030-01-01T00:00:00+00:00",
                       timezone="UTC", now=datetime(2029, 1, 1, tzinfo=UTC))
        if source == "schedule":
            scheduler.create(prompt="inspect files", workspace=workspace, **options)
        else:
            scheduler.create_automation(automation_name="inspect", parameters={}, **options)
        assert scheduler.tick(datetime(2030, 1, 1, tzinfo=UTC)) == 1
        job = store.list_jobs()[0]
    workspace.rename(tmp_path / "original-workspace")
    workspace.symlink_to(outside, target_is_directory=True)
    calls = []

    async def execute(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("provider must not run in a rebound workspace")

    reopened = JobStore(store.path)
    await JobRunner(reopened, execute=execute).run(reopened.claim_next_job())
    persisted = JobStore(store.path)
    result = persisted.get_job_result(job.id)
    assert result.status == JobStatus.BLOCKED
    assert result.error_code == ErrorCode.SANDBOX_VIOLATION
    assert persisted.get_job(job.id).workspace == str(workspace)
    assert persisted.list_job_attempts(job.id)[0].error_code == ErrorCode.SANDBOX_VIOLATION
    assert calls == []


def test_resume_rejects_rebound_workspace_without_checkpoint_changes(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    store = JobStore(tmp_path / "jobs.db")
    job = JobService(store).submit("inspect files", workspace=workspace)
    claimed = store.claim_next_job()
    store.interrupt_job(job.id, "restart")
    workspace.rename(tmp_path / "original-workspace")
    workspace.symlink_to(outside, target_is_directory=True)
    reopened = JobStore(store.path)
    with pytest.raises(ValueError) as caught:
        JobService(reopened).resume(job.id)
    assert caught.value.error_code == ErrorCode.SANDBOX_VIOLATION
    assert reopened.get_job(job.id).status == JobStatus.INTERRUPTED
    assert reopened.list_job_attempts(job.id)[0].id == claimed.attempt_id
