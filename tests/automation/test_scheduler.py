import asyncio
from datetime import UTC, datetime, timedelta

from agent import AgentRequest, AgentRuntime
from llm.types import ModelResponse, ModelUsage
from tests.support.ai_helpers import FakeClientSession
from workflows.models import JobStatus, MissedRunPolicy, ScheduleKind, TriggerStatus
from workflows.runtime.runner import JobRunner
from workflows.runtime.scheduler import CronExpression, Scheduler
from workflows.runtime.worker import AutomationWorker
from workflows.storage.store import JobStore


def test_schedule_persists_across_store_restart(tmp_path):
    database = tmp_path / "suto.db"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    scheduler = Scheduler(JobStore(database))
    created = scheduler.create(
        kind="interval",
        expression="300",
        prompt="inspect project",
        workspace=tmp_path,
        allow_write=True,
        retry_limit=2,
        now=start,
    )

    reopened = JobStore(database).get_schedule(created.id)

    assert reopened == created
    assert reopened.kind == ScheduleKind.INTERVAL
    assert reopened.next_run_at == (start + timedelta(seconds=300)).isoformat()
    assert reopened.allow_write is True
    assert reopened.retry_limit == 2


def test_interval_tick_creates_exactly_one_job_with_saved_permissions(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    scheduler = Scheduler(store)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    schedule = scheduler.create(
        kind="interval",
        expression="60",
        prompt="run tests",
        workspace=tmp_path,
        allow_write=True,
        allow_command=True,
        now=start,
    )
    due = start + timedelta(seconds=60)

    assert scheduler.tick(due) == 1
    assert scheduler.tick(due) == 0

    jobs = store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].source == "schedule"
    assert jobs[0].source_ref == schedule.id
    assert jobs[0].workspace == str(tmp_path.resolve())
    assert jobs[0].allow_write is True
    assert jobs[0].allow_command is True
    history = store.list_trigger_history(schedule.id)
    assert len(history) == 1
    assert history[0].job_id == jobs[0].id


def test_schedule_skips_overlap_and_advances(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    scheduler = Scheduler(store)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    schedule = scheduler.create(
        kind="interval",
        expression="60",
        prompt="slow task",
        now=start,
    )

    assert scheduler.tick(start + timedelta(seconds=60)) == 1
    assert scheduler.tick(start + timedelta(seconds=120)) == 0

    history = store.list_trigger_history(schedule.id)
    assert len(history) == 2
    assert history[0].status == TriggerStatus.SKIPPED
    assert "still active" in history[0].detail
    assert len(store.list_jobs()) == 1


def test_schedule_skips_overlap_while_parent_waits_for_subtasks(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    scheduler = Scheduler(store)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    schedule = scheduler.create(
        kind="interval",
        expression="60",
        prompt="delegated task",
        now=start,
    )
    assert scheduler.tick(start + timedelta(seconds=60)) == 1
    job = store.claim_next_job()
    with store._connect() as connection:
        connection.execute(
            "UPDATE jobs SET status = ? WHERE id = ?",
            (JobStatus.WAITING_CHILDREN, job.id),
        )

    assert scheduler.tick(start + timedelta(seconds=120)) == 0
    assert len(store.list_jobs()) == 1
    assert store.list_trigger_history(schedule.id)[0].status == TriggerStatus.SKIPPED


def test_skip_missed_run_collapses_backlog_without_creating_job(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    scheduler = Scheduler(store)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    schedule = scheduler.create(
        kind="interval",
        expression="60",
        prompt="frequent task",
        missed_run_policy=MissedRunPolicy.SKIP,
        now=start,
    )

    assert scheduler.tick(start + timedelta(minutes=5)) == 0

    refreshed = store.get_schedule(schedule.id)
    assert refreshed.next_run_at == (start + timedelta(minutes=6)).isoformat()
    assert store.list_jobs() == []
    assert (
        store.list_trigger_history(schedule.id)[0].detail
        == "skipped missed occurrence"
    )


def test_run_once_missed_policy_creates_one_job_not_full_backlog(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    scheduler = Scheduler(store)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    schedule = scheduler.create(
        kind="interval",
        expression="60",
        prompt="frequent task",
        now=start,
    )

    assert scheduler.tick(start + timedelta(minutes=5)) == 1

    refreshed = store.get_schedule(schedule.id)
    assert refreshed.next_run_at == (start + timedelta(minutes=6)).isoformat()
    assert len(store.list_jobs()) == 1


def test_failed_scheduled_job_is_retried_with_same_occurrence(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    scheduler = Scheduler(store)
    start = datetime.now(UTC).replace(microsecond=0)
    schedule = scheduler.create(
        kind="interval",
        expression="60",
        prompt="flaky task",
        retry_limit=1,
        retry_delay_seconds=1,
        now=start,
    )
    scheduler.tick(start + timedelta(seconds=60))
    first_job = store.claim_next_job()
    assert first_job is not None
    assert store.fail_job(first_job.id, "temporary failure")

    assert scheduler.tick(start + timedelta(seconds=70)) == 1

    history = store.list_trigger_history(schedule.id)
    assert [item.attempt for item in history] == [2, 1]
    assert history[0].scheduled_for == history[1].scheduled_for
    assert history[0].job_id != history[1].job_id
    assert store.get_job(history[0].job_id).status == JobStatus.QUEUED


def test_pause_prevents_trigger_and_resume_allows_it(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    scheduler = Scheduler(store)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    schedule = scheduler.create(
        kind="interval", expression="60", prompt="task", now=start
    )
    assert store.set_schedule_enabled(schedule.id, False)
    assert scheduler.tick(start + timedelta(seconds=60)) == 0
    assert store.set_schedule_enabled(schedule.id, True)
    assert scheduler.tick(start + timedelta(seconds=60)) == 1


def test_cron_uses_requested_timezone_and_standard_weekday_alias():
    expression = CronExpression.parse("0 9 * * 1-5")
    thursday = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)

    next_run = expression.next_after(thursday, "Asia/Bangkok")

    assert next_run == datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
    assert CronExpression.parse("0 0 * * 7").weekday.values == frozenset({0})


def test_once_schedule_runs_after_restart_only_once(tmp_path):
    database = tmp_path / "suto.db"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = Scheduler(JobStore(database))
    schedule = first.create(
        kind="once",
        expression=(start + timedelta(minutes=1)).isoformat(),
        prompt="one time task",
        now=start,
    )

    restarted = Scheduler(JobStore(database))
    assert restarted.tick(start + timedelta(minutes=2)) == 1
    assert restarted.tick(start + timedelta(minutes=3)) == 0
    assert restarted.store.get_schedule(schedule.id).next_run_at is None
    assert len(restarted.store.list_trigger_history(schedule.id)) == 1


async def test_scheduled_job_reaches_agent_runtime_and_commits_result(tmp_path, monkeypatch):
    from ai import executor, response
    from ai.execution import loop

    store = JobStore(tmp_path / "suto.db")
    scheduler = Scheduler(store)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    schedule = scheduler.create(
        kind="once",
        expression=(start + timedelta(minutes=1)).isoformat(),
        prompt="Please reply in English: inspect project",
        workspace=tmp_path,
        now=start,
    )
    assert scheduler.tick(start + timedelta(minutes=1)) == 1
    job = store.claim_next_job()
    assert job is not None

    seen_requests = []
    seen_model_requests = []

    class FakeModel:
        async def generate(self, request):
            seen_model_requests.append(request)
            return ModelResponse("Inspection complete.", usage=ModelUsage(3, 2))

    original_run = AgentRuntime.run

    async def record_run(self, request, messages, **kwargs):
        seen_requests.append(request)
        return await original_run(self, request, messages, **kwargs)

    monkeypatch.setattr(executor.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(loop, "build_model_router", lambda session: FakeModel())
    monkeypatch.setattr(response, "detect_language_code", lambda text: "en")
    monkeypatch.setattr(AgentRuntime, "run", record_run)

    await JobRunner(store).run(job)

    assert len(seen_requests) == 1
    assert isinstance(seen_requests[0], AgentRequest)
    assert seen_requests[0].user_message == schedule.prompt
    assert seen_requests[0].metadata == {
        "job_id": job.id,
        "source": "schedule",
        "source_ref": schedule.id,
        "parent_id": None,
        "parent_run_id": None,
    }
    available = {tool["function"]["name"] for tool in seen_model_requests[0].available_tools}
    assert "read_workspace_file" in available
    assert "apply_workspace_patch" not in available
    assert "run_workspace_command" not in available
    completed = store.get_job(job.id)
    assert completed.status == JobStatus.COMPLETED
    assert completed.result == "Inspection complete."
    assert completed.total_tokens == 5


async def test_restarted_worker_runs_scheduled_job_through_provider_and_read_tool(tmp_path, monkeypatch):
    from ai import config, executor, response

    (tmp_path / "source.txt").write_text("verified input", encoding="utf-8")
    database = tmp_path / "suto.db"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    scheduler = Scheduler(JobStore(database))
    schedule = scheduler.create(
        kind="once",
        expression=(start + timedelta(minutes=1)).isoformat(),
        prompt="Read source.txt and report its contents.",
        workspace=tmp_path,
        now=start,
    )
    assert scheduler.tick(start + timedelta(minutes=1)) == 1

    requests = []
    finished = asyncio.Event()
    provider_responses = iter([
        {
            "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "read-1", "type": "function",
                "function": {"name": "read_workspace_file", "arguments": '{"path":"source.txt"}'},
            }]}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        },
        {
            "choices": [{"message": {"role": "assistant", "content": "verified input"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        },
    ])

    class FakeResponse:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def raise_for_status(self):
            pass

        async def json(self):
            return self.payload

    class FakeProviderSession(FakeClientSession):
        def post(self, url, **kwargs):
            requests.append(kwargs["json"])
            return FakeResponse(next(provider_responses))

    monkeypatch.setattr(executor.aiohttp, "ClientSession", FakeProviderSession)
    monkeypatch.setattr(config, "AI_PROVIDER", "openai-compatible")
    monkeypatch.setattr(config, "AI_BASE_URL", "https://provider.test/v1")
    monkeypatch.setattr(config, "AI_MODEL", "test-model")
    monkeypatch.setattr(config, "AI_API_KEY", "")
    monkeypatch.setattr(response, "detect_language_code", lambda text: "en")

    reopened = JobStore(database)
    original_complete = reopened.complete_job

    def record_completion(*args, **kwargs):
        result = original_complete(*args, **kwargs)
        finished.set()
        return result

    monkeypatch.setattr(reopened, "complete_job", record_completion)
    worker = AutomationWorker(reopened, JobRunner(reopened), poll_interval=0.01)
    worker_task = asyncio.create_task(worker.start())
    try:
        await asyncio.wait_for(finished.wait(), timeout=2)
    finally:
        await worker.stop()
        await worker_task

    job = reopened.list_jobs()[0]
    assert job.source_ref == schedule.id
    assert job.status == JobStatus.COMPLETED
    assert job.result == "verified input"
    assert job.total_tokens == 11
    assert len(requests) == 2
    first_tools = {item["function"]["name"] for item in requests[0]["tools"]}
    assert "read_workspace_file" in first_tools
    assert "apply_workspace_patch" not in first_tools
    assert "run_workspace_command" not in first_tools
    assert "verified input" in requests[1]["messages"][-1]["content"]
    assert requests[1]["messages"][-1]["tool_call_id"] == "read-1"
    tool_events = reopened.list_tool_events(job.id)
    assert len(tool_events) == 1
    assert tool_events[0].tool_name == "read_workspace_file"
    assert tool_events[0].status == "finished"
    assert tool_events[0].tool_call_id == f"{tool_events[0].run_id}:1:1"
    assert tool_events[0].run_id
    trace = reopened.list_run_events(tool_events[0].run_id)
    assert {event["job_id"] for event in trace} == {job.id}
    assert {event["event_type"] for event in trace} >= {
        "agent.preparing", "model.requested", "model.completed",
        "tool.requested", "permission.allowed", "tool.started",
        "tool.completed", "tool.observed", "agent.completed",
    }
    assert {event["tool_call_id"] for event in trace if event["event_type"].startswith("tool.")} == {tool_events[0].tool_call_id}
    assert any(event.run_id == tool_events[0].run_id for event in reopened.list_events(job.id))


async def test_cancelling_scheduled_job_stops_agent_runtime(tmp_path, monkeypatch):
    from ai import executor
    from ai.execution import loop

    store = JobStore(tmp_path / "suto.db")
    started = asyncio.Event()
    stopped = asyncio.Event()

    class WaitingModel:
        async def generate(self, request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    monkeypatch.setattr(executor.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(loop, "build_model_router", lambda session: WaitingModel())
    schedule = Scheduler(store).create(
        kind="interval",
        expression="60",
        prompt="wait for model",
        workspace=tmp_path,
        now=datetime.now(UTC) - timedelta(minutes=2),
    )
    worker = AutomationWorker(store, JobRunner(store), poll_interval=0.01)
    worker_task = asyncio.create_task(worker.start())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        job = store.list_jobs()[0]
        assert job.source_ref == schedule.id
        assert await worker.cancel(job.id)
        await asyncio.wait_for(stopped.wait(), timeout=2)
        assert store.get_job(job.id).status == JobStatus.CANCELLED
    finally:
        await worker.stop()
        await worker_task
