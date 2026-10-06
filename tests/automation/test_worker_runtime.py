"""The production worker lifecycle does not need an interactive session."""

import asyncio
import os
import signal
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context

import pytest

from agent import AgentRuntime
from ai import AIExecutionResult
from application import worker as worker_application
from application.automation import ApprovalService, JobService, ScheduleService
from llm.types import ModelResponse, ModelUsage, ToolCall
from tests.support.ai_helpers import FakeClientSession
from workflows.models import JobStatus, ScheduleKind
from workflows.runtime.context import ApprovalRequired
from workflows.runtime.runner import JobRunner
from workflows.runtime.scheduler import Scheduler
from workflows.storage.store import JobStore


NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def freeze_scheduler(monkeypatch, instant):
    tick = Scheduler.tick
    monkeypatch.setattr(Scheduler, "tick", lambda self, now=None: tick(self, instant))


async def test_headless_worker_runs_jobs_and_pinned_schedule_through_agent_runtime(
    tmp_path, monkeypatch,
):
    from ai import executor, response
    from ai.execution import loop

    monkeypatch.setenv("SUTO_DB_PATH", str(tmp_path / "jobs.db"))
    freeze_scheduler(monkeypatch, NOW)
    store = JobStore(tmp_path / "jobs.db")
    (tmp_path / "source.txt").write_text("verified input", encoding="utf-8")
    store.create_skill("review", "Read the source before reporting.")
    store.create_automation("review", "Read source.txt and report in English.", tmp_path, {},
                            skill_names=["review"])
    schedule = ScheduleService(store).create_automation(
        automation_name="review", parameters={}, kind=ScheduleKind.ONCE,
        expression=NOW.isoformat(), timezone="Asia/Bangkok",
    )
    store.revise_skill("review", "Changed instructions.")
    store.revise_automation("review", "New prompt.", tmp_path, {}, allow_write=True)
    queued = JobService(store).submit("Read source.txt and reply in English.", workspace=tmp_path)
    stopped = asyncio.Event()
    completed = []
    requests = []
    models = []
    complete = JobStore.complete_job
    runtime_run = AgentRuntime.run

    def record_completion(self, job_id, *args, **kwargs):
        result = complete(self, job_id, *args, **kwargs)
        completed.append(job_id)
        if len(completed) == 2:
            stopped.set()
        return result

    async def record_run(self, request, *args, **kwargs):
        requests.append(request)
        assert request.session_id is None
        return await runtime_run(self, request, *args, **kwargs)

    class Model:
        async def generate(self, request):
            models.append(request)
            if request.messages[-1]["role"] == "tool":
                assert "verified input" in request.messages[-1]["content"]
                return ModelResponse("Verified input was read.", usage=ModelUsage(3, 2))
            return ModelResponse(None, [ToolCall("read_workspace_file", {"path": "source.txt"})])

    def no_session(*args, **kwargs):
        raise AssertionError("worker attempted to open an interactive session")

    monkeypatch.setattr(JobStore, "resolve_channel_identity", no_session)
    monkeypatch.setattr(JobStore, "get_or_create_conversation", no_session)
    monkeypatch.setattr(JobStore, "begin_agent_run", no_session)
    monkeypatch.setattr(JobStore, "complete_job", record_completion)
    monkeypatch.setattr(AgentRuntime, "run", record_run)
    monkeypatch.setattr(executor.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(loop, "build_model_router", lambda session: Model())
    monkeypatch.setattr(response, "detect_language_code", lambda text: "en")

    await asyncio.wait_for(worker_application.run_worker(stop_event=stopped), timeout=5)

    reopened = JobStore(store.path)
    assert len(requests) == 2
    assert {request.metadata["job_id"] for request in requests} == set(completed)
    jobs = reopened.list_jobs()
    assert {job.id for job in jobs} == set(completed)
    scheduled = next(job for job in jobs if job.id != queued.id)
    assert scheduled.source_ref == schedule.id
    assert reopened.get_job_automation_version_id(scheduled.id) == schedule.automation.automation_version_id
    assert any("Read the source before reporting." in str(request.messages) for request in models)
    assert all("Changed instructions." not in str(request.messages) for request in models)
    for job in jobs:
        result = JobService(reopened).result(job.id)
        assert result.status == JobStatus.COMPLETED
        assert job.result == "Verified input was read."
        assert job.total_tokens == 5
        assert job.attempt_count == 1
        assert reopened.list_tool_events(job.id)[0].status == "finished"
    assert {event["job_id"] for event in reopened.notifications()} == set(completed)
    assert all(event["read_at"] is None for event in reopened.notifications())
    assert not reopened.diagnostics()["worker_alive"]
    with reopened._connect() as db:
        for table in ("users", "conversations", "messages", "agent_runs"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


async def test_worker_continues_after_cli_exit_and_accepts_database_cancellation(
    tmp_path, monkeypatch, capsys,
):
    from interfaces.cli import backend

    monkeypatch.setenv("SUTO_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("SUTO_NOTIFY_CLI", "0")
    store = JobStore(tmp_path / "jobs.db")
    store.create_automation("review", "Inspect project.", tmp_path, {})
    tick = Scheduler.tick
    instant = [NOW]
    monkeypatch.setattr(Scheduler, "tick", lambda self, now=None: tick(self, instant[0]))
    entered = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    complete = asyncio.Event()
    stop = asyncio.Event()
    attempts = []
    original_complete = JobStore.complete_job

    async def execute(prompt, **kwargs):
        attempts.append(kwargs["execution_context"].job_id)
        entered.set()
        if prompt == "wait for cancellation":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        await release.wait()
        return AIExecutionResult("done", "completed", None, 1, 1, 0)

    def record_completion(self, *args, **kwargs):
        result = original_complete(self, *args, **kwargs)
        complete.set()
        return result

    monkeypatch.setattr(worker_application, "JobRunner", lambda store: JobRunner(store, execute=execute))
    monkeypatch.setattr(JobStore, "complete_job", record_completion)
    task = asyncio.create_task(worker_application.run_worker(stop_event=stop))
    try:
        # Wait for a real job claim instead of timing worker startup.
        probe = JobService(store).submit("wait for cancellation", workspace=tmp_path)
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert JobService(JobStore(store.path)).worker_ready
        # A CLI session can open alongside an owner without claiming/recovering jobs.
        due = NOW + timedelta(seconds=1)
        commands = iter((
            "/automation run review",
            f"/schedule automation review --at {due.isoformat()} --timezone UTC",
            "/exit",
        ))

        async def read_prompt():
            return next(commands)

        await backend.run_session("agent", read_prompt)
        assert "Job queued." in capsys.readouterr().out
        assert store.get_job(probe.id).status == JobStatus.RUNNING
        assert not task.done()
        pending = next(job for job in store.list_jobs() if job.id != probe.id)
        assert pending.status == JobStatus.QUEUED
        entered.clear()
        JobService(JobStore(store.path)).cancel(probe.id)
        await asyncio.wait_for(cancelled.wait(), timeout=5)
        await asyncio.wait_for(entered.wait(), timeout=5)
        release.set()
        await asyncio.wait_for(complete.wait(), timeout=5)
        assert store.get_job(probe.id).status == JobStatus.CANCELLED
        assert store.get_job(pending.id).status == JobStatus.COMPLETED
        assert attempts == [probe.id, pending.id]
        # The occurrence becomes due only after the CLI session has closed.
        complete.clear()
        instant[0] = due
        await asyncio.wait_for(complete.wait(), timeout=5)
        schedule = store.list_schedules()[0]
        history = store.list_trigger_history(schedule.id)
        assert len(history) == 1
        scheduled = store.get_job(history[0].job_id)
        assert scheduled.status == JobStatus.COMPLETED
        assert attempts == [probe.id, pending.id, scheduled.id]
        assert not task.done()
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)


async def test_worker_observes_durable_approval_from_another_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SUTO_DB_PATH", str(tmp_path / "jobs.db"))
    store = JobStore(tmp_path / "jobs.db")
    job = JobService(store).submit("write report", workspace=tmp_path, allow_write=True)
    waiting = asyncio.Event()
    stop = asyncio.Event()
    side_effects = []
    complete = JobStore.complete_job

    async def execute(prompt, **kwargs):
        context = kwargs["execution_context"]
        try:
            context.require_approval("write", {"path": "report.txt", "content": "done"}, "write report", "done")
        except ApprovalRequired as error:
            waiting.set()
            return AIExecutionResult(str(error), "waiting_approval", str(error), 0, 0, 0)
        side_effects.append(context.job_id)
        return AIExecutionResult("done", "completed", None, 1, 1, 0)

    def record_completion(self, *args, **kwargs):
        result = complete(self, *args, **kwargs)
        stop.set()
        return result

    monkeypatch.setattr(worker_application, "JobRunner", lambda store: JobRunner(store, execute=execute))
    monkeypatch.setattr(JobStore, "complete_job", record_completion)
    task = asyncio.create_task(worker_application.run_worker(stop_event=stop))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=5)
        assert store.get_job(job.id).status == JobStatus.WAITING_APPROVAL
        assert not side_effects
        other = JobStore(store.path)
        approval = ApprovalService(other).list_pending()[0]
        assert ApprovalService(other).decide(approval.id, True)[2]
        await asyncio.wait_for(task, timeout=5)
        assert side_effects == [job.id]
        assert other.get_job(job.id).status == JobStatus.COMPLETED
        assert other.get_approval(approval.id).status.value == "consumed"
        assert {item["kind"] for item in other.notifications()} == {"approval_required", "completed"}
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)


async def test_schedule_retry_survives_worker_restart_without_duplicate_job(tmp_path, monkeypatch):
    monkeypatch.setenv("SUTO_DB_PATH", str(tmp_path / "jobs.db"))
    store = JobStore(tmp_path / "jobs.db")
    store.create_automation("review", "Inspect project.", tmp_path, {})
    schedule = ScheduleService(store).create_automation(
        automation_name="review", parameters={}, kind=ScheduleKind.ONCE,
        expression=NOW.isoformat(), timezone="UTC", retry_limit=1, retry_delay_seconds=1,
    )
    tick = Scheduler.tick
    instant = [NOW]
    monkeypatch.setattr(Scheduler, "tick", lambda self, now=None: tick(self, instant[0]))
    stop = asyncio.Event()
    failed = JobStore.fail_job
    completed = JobStore.complete_job

    async def fail(prompt, **kwargs):
        return AIExecutionResult("failed", "failed", "providertransienterror", 1, 1, 0)

    def record_failure(self, *args, **kwargs):
        result = failed(self, *args, **kwargs)
        stop.set()
        return result

    def record_completion(self, *args, **kwargs):
        result = completed(self, *args, **kwargs)
        stop.set()
        return result

    monkeypatch.setattr(worker_application, "JobRunner", lambda store: JobRunner(store, execute=fail, retry_delays=()))
    monkeypatch.setattr(JobStore, "fail_job", record_failure)
    await asyncio.wait_for(worker_application.run_worker(stop_event=stop), timeout=5)
    job = store.list_jobs()[0]
    assert job.status == JobStatus.FAILED
    first_attempt = job.attempt_id
    # Advance a deterministic scheduler clock beyond the committed retry deadline.
    instant[0] = datetime.fromisoformat(job.finished_at) + timedelta(seconds=2)
    stop.clear()

    async def succeed(prompt, **kwargs):
        return AIExecutionResult("recovered result", "completed", None, 2, 1, 0)

    monkeypatch.setattr(worker_application, "JobRunner", lambda store: JobRunner(store, execute=succeed))
    monkeypatch.setattr(JobStore, "complete_job", record_completion)
    await asyncio.wait_for(worker_application.run_worker(stop_event=stop), timeout=5)
    final = JobStore(store.path).get_job(job.id)
    assert final.status == JobStatus.COMPLETED
    assert final.result == "recovered result"
    assert final.attempt_count == 2 and final.attempt_id != first_attempt
    assert final.retry_count == 1
    assert len(store.list_jobs()) == 1
    assert {event.job_id for event in store.list_trigger_history(schedule.id)} == {job.id}
    assert len(store.list_trigger_history(schedule.id)) == 2
    assert {event["kind"] for event in store.notifications()} == {"failed", "completed"}


async def test_duplicate_worker_fails_without_recovering_owner_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("SUTO_DB_PATH", str(tmp_path / "jobs.db"))
    store = JobStore(tmp_path / "jobs.db")
    job = JobService(store).submit("inspect", workspace=tmp_path)
    entered = asyncio.Event()
    stop = asyncio.Event()

    async def execute(prompt, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_application, "JobRunner", lambda store: JobRunner(store, execute=execute))
    owner = asyncio.create_task(worker_application.run_worker(stop_event=stop))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        with pytest.raises(RuntimeError, match="another worker") as error:
            await asyncio.wait_for(worker_application.run_worker(stop_event=asyncio.Event()), timeout=5)
        assert error.value.error_code.value == "WORKER_UNAVAILABLE"
        assert store.get_job(job.id).status == JobStatus.RUNNING
        assert store.diagnostics()["worker_alive"]
    finally:
        stop.set()
        await asyncio.wait_for(owner, timeout=5)


def _worker_process(database, pipe, blocking):
    """Real signal/lock lifecycle with deterministic execution and IPC barriers."""
    os.environ["SUTO_DB_PATH"] = str(database)
    os.environ["SUTO_MCP_CONFIG"] = ""
    heartbeat = JobStore.heartbeat
    complete = JobStore.complete_job
    tick = Scheduler.tick
    reported_running = False

    def record_heartbeat(self, state):
        nonlocal reported_running
        heartbeat(self, state)
        if state != "running" or not reported_running:
            pipe.send((state, None))
        reported_running |= state == "running"

    def record_completion(self, job_id, *args, **kwargs):
        result = complete(self, job_id, *args, **kwargs)
        pipe.send(("completed", job_id))
        return result

    async def execute(prompt, **kwargs):
        context = kwargs["execution_context"]
        if blocking:
            context.plan_store.create_plan(context.job_id, ["Inspect project"])
            context.plan_store.update_step(context.job_id, 1, "in_progress")
            pipe.send(("executing", context.job_id))
            await asyncio.Event().wait()
        for step in context.plan_store.list_steps(context.job_id):
            context.plan_store.update_step(context.job_id, step.position, "completed", "done")
        return AIExecutionResult("restarted result", "completed", None, 1, 1, 0)

    JobStore.heartbeat = record_heartbeat
    JobStore.complete_job = record_completion
    Scheduler.tick = lambda self, now=None: tick(self, NOW)
    worker_application.JobRunner = lambda store: JobRunner(store, execute=execute)
    try:
        worker_application.run()
    finally:
        pipe.close()


def receive(pipe, expected):
    assert pipe.poll(10), f"worker did not report {expected}"
    event, job_id = pipe.recv()
    assert event == expected
    return job_id


@pytest.mark.parametrize("shutdown_signal", [signal.SIGINT, signal.SIGTERM, signal.SIGKILL])
def test_process_shutdown_crash_and_restart_recover_attempts_and_schedules(
    tmp_path, shutdown_signal,
):
    database = tmp_path / "jobs.db"
    store = JobStore(database)
    job = JobService(store).submit("inspect", workspace=tmp_path)
    context = get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_worker_process, args=(database, child, True))
    process.start()
    child.close()
    try:
        receive(parent, "running")
        assert receive(parent, "executing") == job.id
        claimed = store.get_job(job.id)
        os.kill(process.pid, shutdown_signal)
        process.join(timeout=10)
        assert not process.is_alive()
        assert process.exitcode == (-signal.SIGKILL if shutdown_signal == signal.SIGKILL else 0)
        expected = JobStatus.RUNNING if shutdown_signal == signal.SIGKILL else JobStatus.INTERRUPTED
        assert store.get_job(job.id).status == expected
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        parent.close()

    # A due occurrence and interrupted work must survive across process owners.
    schedule = ScheduleService(JobStore(database)).create(
        kind=ScheduleKind.ONCE, expression=NOW.isoformat(), prompt="scheduled inspection",
        timezone="UTC", workspace=tmp_path,
    )
    parent, child = context.Pipe(duplex=False)
    restarted = context.Process(target=_worker_process, args=(database, child, False))
    restarted.start()
    child.close()
    try:
        receive(parent, "running")
        recovered = JobStore(database)
        interrupted = recovered.get_job(job.id)
        assert interrupted.status == JobStatus.INTERRUPTED
        assert interrupted.attempt_id == claimed.attempt_id
        assert recovered.list_job_attempts(job.id)[0].status == JobStatus.INTERRUPTED
        assert recovered.list_steps(job.id)[0].status.value == "pending"
        JobService(recovered).resume(job.id)
        completed = {receive(parent, "completed"), receive(parent, "completed")}
        assert job.id in completed
        resumed = recovered.get_job(job.id)
        assert resumed.status == JobStatus.COMPLETED
        assert resumed.attempt_count == 2 and resumed.attempt_id != claimed.attempt_id
        assert resumed.result == "restarted result"
        history = recovered.list_trigger_history(schedule.id)
        assert len(history) == 1
        assert history[0].job_id in completed
        assert recovered.get_job(history[0].job_id).status == JobStatus.COMPLETED
        assert {item["job_id"] for item in recovered.notifications() if item["kind"] == "completed"} == completed
    finally:
        if restarted.is_alive():
            os.kill(restarted.pid, signal.SIGTERM)
            restarted.join(timeout=10)
        if restarted.is_alive():
            restarted.kill()
            restarted.join(timeout=10)
        parent.close()
    assert restarted.exitcode == 0
    assert not JobStore(database).diagnostics()["worker_alive"]
