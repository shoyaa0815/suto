"""External HTTP requests use durable services and the production worker/runtime path."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent import AgentRequest, AgentRuntime
from application.automation import ApprovalService, AutomationService, JobService, ScheduleService
from interfaces.api.server import create_app
from llm.types import ModelResponse, ModelUsage, ToolCall
from tests.interfaces.test_api import HEADERS, Model, setup
from workflows.models import ApprovalStatus, JobStatus, ScheduleKind
from workflows.runtime.runner import JobRunner
from workflows.runtime.scheduler import Scheduler
from workflows.runtime.worker import AutomationWorker
from workflows.storage.store import JobStore


def make_app(tmp_path, monkeypatch, model_factory=lambda: Model(ModelResponse("Done."))):
    monkeypatch.setenv("SUTO_DB_PATH", str(tmp_path / "suto.db"))
    return setup(tmp_path, monkeypatch, model_factory)


@asynccontextmanager
async def running_worker(store):
    attempts = asyncio.Queue()

    class ObservedRunner(JobRunner):
        async def run(self, job):
            try:
                await super().run(job)
            finally:
                attempts.put_nowait(job.id)

    # Separate store/worker ownership, just as in main.py worker; API has no worker.
    worker_store = JobStore(store.path)
    worker = AutomationWorker(worker_store, ObservedRunner(worker_store), poll_interval=0.01)
    task = asyncio.create_task(worker.start())
    try:
        yield attempts
    finally:
        await worker.stop()
        await task


async def attempt_finished(attempts, job_id):
    assert await asyncio.wait_for(attempts.get(), timeout=5) == job_id


async def submit(client, **fields):
    response = await client.post("/jobs", json={"prompt": "Report in English.", **fields}, headers=HEADERS)
    assert response.status == 202
    return await response.json()


async def test_external_job_service_worker_runtime_and_persisted_result(tmp_path, monkeypatch):
    (tmp_path / "source.txt").write_text("verified input", encoding="utf-8")
    model = Model(
        ModelResponse(None, [ToolCall("read_workspace_file", {"path": "source.txt"})]),
        ModelResponse("Verified input was read.", usage=ModelUsage(3, 2)),
    )
    app, store = make_app(tmp_path, monkeypatch, lambda: model)
    submitted = []
    requests = []
    original_submit = JobService.submit
    original_run = AgentRuntime.run

    def record_submit(self, *args, **kwargs):
        assert self.worker is None
        job = original_submit(self, *args, **kwargs)
        submitted.append(job.id)
        return job

    async def record_runtime(self, request, *args, **kwargs):
        assert isinstance(request, AgentRequest)
        assert request.session_id is None
        requests.append(request)
        return await original_run(self, request, *args, **kwargs)

    monkeypatch.setattr(JobService, "submit", record_submit)
    monkeypatch.setattr(AgentRuntime, "run", record_runtime)
    async with TestClient(TestServer(app)) as client:
        job = await submit(client, workspace=str(tmp_path))
        assert job["status"] == "queued" and job["source"] == "api"
        assert not job["allow_write"] and not job["allow_command"]
        assert JobStore(store.path).get_job(job["id"]).status == JobStatus.QUEUED
        assert (await (await client.get(f"/jobs/{job['id']}/result")).json())["result_summary"] is None
        async with running_worker(store) as attempts:
            await attempt_finished(attempts, job["id"])
        detail = await (await client.get(f"/jobs/{job['id']}")).json()
        assert detail["status"] == "completed"
        assert detail["result"] == "Verified input was read."
        assert detail["prompt_tokens"] == 3 and detail["output_tokens"] == 2
        assert detail["attempt_count"] == 1
        assert (await (await client.get("/jobs")).json())["jobs"] == [detail]
        assert submitted == [job["id"]]
        assert len(requests) == 1 and requests[0].metadata["job_id"] == job["id"]
        assert "verified input" in model.requests[1].messages[-1]["content"]

    reopened = JobStore(store.path)
    assert reopened.get_job(job["id"]).result == detail["result"]
    assert reopened.list_job_attempts(job["id"])[0].status == JobStatus.COMPLETED
    assert reopened.list_tool_events(job["id"])[0].tool_name == "read_workspace_file"
    assert reopened.notifications()[0]["job_id"] == job["id"]
    async with TestClient(TestServer(create_app(store=reopened))) as client:
        assert await (await client.get(f"/jobs/{job['id']}")).json() == detail
        result = await (await client.get(f"/jobs/{job['id']}/result")).json()
        assert result["status"] == "completed" and result["result_summary"] == detail["result"]
        assert result["attempt_id"] == detail["attempt_id"]
        assert result["error_code"] is None and result["safe_error_message"] is None
        assert result["automation_version_id"] is None and result["schedule_id"] is None


async def test_api_shutdown_preserves_queued_job_for_independent_worker(tmp_path, monkeypatch):
    app, store = make_app(tmp_path, monkeypatch)
    async with TestClient(TestServer(app)) as client:
        job = await submit(client, workspace=str(tmp_path))
    assert store.get_job(job["id"]).status == JobStatus.QUEUED
    async with running_worker(store) as attempts:
        await attempt_finished(attempts, job["id"])
    assert JobStore(store.path).get_job(job["id"]).status == JobStatus.COMPLETED


@pytest.mark.parametrize("payload", [
    {}, [], {"prompt": ""}, {"prompt": " "}, {"prompt": 1}, {"prompt": "x" * 8001},
    {"prompt": "task", "workspace": None}, {"prompt": "task", "workspace": ""},
    {"prompt": "task", "workspace": "\x00"}, {"prompt": "task", "allow_write": 1},
    {"prompt": "task", "allow_command": "false"}, {"prompt": "task", "mode": "developer"},
    {"prompt": "task", "allowed_tools": ["run_command"]},
    {"prompt": "task", "approval": True}, {"prompt": "task", "options": {"sandbox": "none"}},
    {"prompt": "task", "source": "cli"}, {"prompt": "api_key=sk-secret-value"},
])
async def test_invalid_job_request_never_persists(tmp_path, monkeypatch, payload):
    app, store = make_app(tmp_path, monkeypatch)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/jobs", json=payload, headers=HEADERS)
        assert response.status == 400
        assert (await response.json())["error_code"] == "INVALID_INPUT"
    assert JobStore(store.path).list_jobs() == []


async def test_json_limits_local_guard_and_structured_safe_errors(tmp_path, monkeypatch):
    app, store = make_app(tmp_path, monkeypatch)
    async with TestClient(TestServer(app)) as client:
        for raw in ("{", '{"prompt":"task","allow_write":NaN}', "[" * 1100):
            response = await client.post("/jobs", data=raw, headers={**HEADERS, "Content-Type": "application/json"})
            assert response.status == 400
            assert (await response.json())["error_code"] == "INVALID_INPUT"
        too_large = await client.post("/jobs", json={"prompt": "x" * 20000}, headers=HEADERS)
        assert too_large.status == 413
        assert (await too_large.json())["error_code"] == "INVALID_INPUT"
        for path, code in (("/jobs/missing", "JOB_NOT_FOUND"), ("/jobs/missing/result", "JOB_NOT_FOUND"),
                           ("/automations/missing", "AUTOMATION_NOT_FOUND"),
                           ("/schedules/missing", "SCHEDULE_NOT_FOUND")):
            response = await client.get(path)
            assert response.status == 404 and (await response.json())["error_code"] == code
        missing = await client.post("/jobs/missing/cancel", json={}, headers=HEADERS)
        assert missing.status == 404 and (await missing.json())["error_code"] == "JOB_NOT_FOUND"
        for kwargs in ({}, {"headers": {**HEADERS, "Origin": "http://evil.example"}},
                       {"headers": {**HEADERS, "Host": "evil.example"}}):
            response = await client.post("/jobs", json={"prompt": "task"}, **kwargs)
            assert response.status == 403
            assert (await response.json())["error_code"] == "PERMISSION_DENIED"
        invalid_workspace = await client.post("/jobs", json={"prompt": "task", "workspace": str(tmp_path / "missing")}, headers=HEADERS)
        assert invalid_workspace.status == 400
        assert (await invalid_workspace.json())["error_code"] == "WORKSPACE_INVALID"
        assert str(tmp_path) not in await invalid_workspace.text()
        store.configure(max_queued_jobs=1)
        await submit(client)
        quota = await client.post("/jobs", json={"prompt": "another"}, headers=HEADERS)
        assert quota.status == 429 and (await quota.json())["error_code"] == "QUOTA_EXCEEDED"
        assert quota.headers["Cache-Control"] == "no-store"
        assert quota.headers["X-Content-Type-Options"] == "nosniff"
    assert len(store.list_jobs()) == 1


async def test_unknown_service_failure_does_not_expose_internal_data(tmp_path, monkeypatch):
    app, store = make_app(tmp_path, monkeypatch)

    def fail(*args, **kwargs):
        raise RuntimeError("api_key=private-value /private/workspace")

    monkeypatch.setattr(JobService, "submit", fail)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/jobs", json={"prompt": "task"}, headers=HEADERS)
        assert response.status == 500
        assert (await response.json())["error_code"] == "INTERNAL_ERROR"
        assert "private" not in await response.text()
    assert store.list_jobs() == []


async def test_cancellation_queued_running_idempotent_and_terminal_conflict(tmp_path, monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class WaitingModel:
        async def generate(self, request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    app, store = make_app(tmp_path, monkeypatch, WaitingModel)
    async with TestClient(TestServer(app)) as client:
        queued = await submit(client)
        for _ in range(2):
            response = await client.post(f"/jobs/{queued['id']}/cancel", json={}, headers=HEADERS)
            assert response.status == 202 and (await response.json())["status"] == "cancelled"
        assert store.list_job_attempts(queued["id"]) == []
        running = await submit(client)
        async with running_worker(store) as attempts:
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert (await (await client.get(f"/jobs/{running['id']}")).json())["status"] == "running"
            malformed = await client.post(f"/jobs/{running['id']}/cancel", json={"force": True}, headers=HEADERS)
            assert malformed.status == 400 and store.get_job(running["id"]).status == JobStatus.RUNNING
            response = await client.post(f"/jobs/{running['id']}/cancel", json={}, headers=HEADERS)
            assert response.status == 202 and (await response.json())["error_code"] == "CANCELLED"
            await asyncio.wait_for(cancelled.wait(), timeout=5)
            await attempt_finished(attempts, running["id"])
        reopened = JobStore(store.path)
        assert reopened.get_job(running["id"]).status == JobStatus.CANCELLED
        assert reopened.list_job_attempts(running["id"])[0].status == JobStatus.CANCELLED
        completed = JobService(store).submit("already done", workspace=tmp_path)
        store.claim_next_job()
        assert store.complete_job(completed.id, "done", 0, 0)
        conflict = await client.post(f"/jobs/{completed.id}/cancel", json={}, headers=HEADERS)
        assert conflict.status == 409 and (await conflict.json())["error_code"] == "JOB_STATE_CONFLICT"
        assert store.get_job(completed.id).status == JobStatus.COMPLETED


async def test_automation_and_schedule_reads_run_parameters_and_version_pinning(tmp_path, monkeypatch):
    models = []

    def factory():
        model = Model(ModelResponse("Pinned automation completed."))
        models.append(model)
        return model

    app, store = make_app(tmp_path, monkeypatch, factory)
    store.create_skill("review", "Original skill instructions.")
    automation = store.create_automation("review", "Report on {{topic}} in English.", tmp_path,
                                         {"topic": {"type": "string", "required": True}}, skill_names=["review"])
    schedule = ScheduleService(store).create_automation(
        automation_name="review", parameters={"topic": "scheduled topic"}, kind=ScheduleKind.ONCE,
        expression="2030-01-01T00:00:00+00:00", timezone="Asia/Bangkok",
    )
    called = []
    original_run = AutomationService.run

    def record_run(self, *args, **kwargs):
        assert self.worker is None
        called.append(args)
        return original_run(self, *args, **kwargs)

    monkeypatch.setattr(AutomationService, "run", record_run)
    async with TestClient(TestServer(app)) as client:
        assert (await (await client.get("/automations")).json())["automations"][0]["id"] == automation.id
        detail = await (await client.get(f"/automations/{automation.id}")).json()
        assert detail == await (await client.get("/automations/review")).json()
        assert detail["skills"] == ["review"] and detail["version"]["version"] == 1
        schedule_detail = await (await client.get(f"/schedules/{schedule.id}")).json()
        assert schedule_detail["timezone"] == "Asia/Bangkok"
        assert schedule_detail["automation"]["automation_version_id"] == detail["version"]["id"]
        assert (await (await client.get("/schedules")).json())["schedules"] == [schedule_detail]
        for payload, code in (({}, "INVALID_PARAMETER"), ({"parameters": []}, "INVALID_PARAMETER"),
                              ({"parameters": None}, "INVALID_PARAMETER"),
                              ({"parameters": {"topic": 5}}, "INVALID_PARAMETER"),
                              ({"parameters": {"topic": "x", "unknown": True}}, "INVALID_PARAMETER"),
                              ({"parameters": {"topic": "api_key=hidden"}}, "INVALID_PARAMETER"),
                              ({"parameters": {"topic": "x"}, "allow_write": True}, "INVALID_INPUT"),
                              ({"parameters": {"topic": "x"}, "version": 99}, "INVALID_INPUT")):
            response = await client.post(f"/automations/{automation.id}/run", json=payload, headers=HEADERS)
            assert response.status == 400 and (await response.json())["error_code"] == code
        assert store.list_jobs() == []
        missing = await client.post("/automations/missing/run", json={}, headers=HEADERS)
        assert missing.status == 404 and (await missing.json())["error_code"] == "AUTOMATION_NOT_FOUND"
        response = await client.post(f"/automations/{automation.id}/run", json={"parameters": {"topic": "original topic"}}, headers=HEADERS)
        assert response.status == 202
        job = await response.json()
        assert called[-1] == (automation.id, {"topic": "original topic"})
        assert job["source_ref"] == detail["version"]["id"]
        store.revise_skill("review", "New skill instructions.")
        store.revise_automation("review", "Changed {{topic}}.", tmp_path,
                                {"topic": {"type": "string", "required": True}}, allow_write=True, skill_names=["review"])
        async with running_worker(store) as attempts:
            await attempt_finished(attempts, job["id"])
        result = await (await client.get(f"/jobs/{job['id']}/result")).json()
        assert result["status"] == "completed" and result["automation_version_id"] == detail["version"]["id"]
        assert "Original skill instructions." in str(models[0].requests[0].messages)
        assert "New skill instructions." not in str(models[0].requests[0].messages)
        persisted = JobStore(store.path).get_job(job["id"])
        assert persisted.prompt == "Report on original topic in English." and not persisted.allow_write
        assert store.get_automation_run_parameters(job["id"]) == {"topic": "original topic"}
        assert await (await client.get(f"/schedules/{schedule.id}")).json() == schedule_detail
        assert (await (await client.get(f"/automations/{automation.id}")).json())["current_version"] == 2
        assert Scheduler(store).tick(datetime(2030, 1, 1, tzinfo=UTC)) == 1
        scheduled = next(item for item in store.list_jobs() if item.source == "schedule")
        scheduled_result = await (await client.get(f"/jobs/{scheduled.id}/result")).json()
        assert scheduled_result["automation_version_id"] == detail["version"]["id"]
        assert scheduled_result["schedule_id"] == schedule.id and scheduled_result["trigger_id"] is not None


@pytest.mark.parametrize("tool", [
    ToolCall("apply_workspace_patch", {"path": "denied.txt", "content": "unsafe"}),
    ToolCall("run_workspace_command", {"command": ["git", "status"]}),
    ToolCall("mcp.fake.exec", {}),
])
async def test_default_job_permission_denies_mutation_command_and_mcp(tmp_path, monkeypatch, tool):
    app, store = make_app(tmp_path, monkeypatch, lambda: Model(ModelResponse(None, [tool])))
    async with TestClient(TestServer(app)) as client:
        job = await submit(client)
        async with running_worker(store) as attempts:
            await attempt_finished(attempts, job["id"])
        detail = await (await client.get(f"/jobs/{job['id']}")).json()
        assert detail["status"] == "blocked"
        assert not (tmp_path / "denied.txt").exists()
        assert store.list_change_events(job["id"]) == []
        assert store.list_command_events(job["id"]) == []
        assert store.list_pending_approvals() == []


@pytest.mark.parametrize("decision", ["approve", "deny", "cancel"])
async def test_job_write_requires_durable_exact_approval(tmp_path, monkeypatch, decision):
    patch = ToolCall("apply_workspace_patch", {"path": "approved.txt", "content": "approved content"})
    app, store = make_app(tmp_path, monkeypatch, lambda: Model(ModelResponse(None, [patch]), ModelResponse("Written.")))
    async with TestClient(TestServer(app)) as client:
        job = await submit(client, allow_write=True)
        async with running_worker(store) as attempts:
            await attempt_finished(attempts, job["id"])
            assert (await (await client.get(f"/jobs/{job['id']}")).json())["status"] == "waiting_approval"
            assert not (tmp_path / "approved.txt").exists()
            reopened = JobStore(store.path)
            approval = reopened.latest_approval(job["id"])
            assert approval.status == ApprovalStatus.PENDING
            if decision == "cancel":
                response = await client.post(f"/jobs/{job['id']}/cancel", json={}, headers=HEADERS)
                assert response.status == 202
                assert reopened.latest_approval(job["id"]).status == ApprovalStatus.INVALIDATED
                assert not ApprovalService(reopened).decide(approval.id, True)[2]
            else:
                assert ApprovalService(reopened).decide(approval.id, decision == "approve")[2]
                if decision == "approve":
                    await attempt_finished(attempts, job["id"])
                    assert (tmp_path / "approved.txt").read_text() == "approved content"
                    assert reopened.latest_approval(job["id"]).status == ApprovalStatus.CONSUMED
                    assert len(reopened.list_change_events(job["id"])) == 1
                    # Existing policy still blocks completion without post-write verification.
                    assert reopened.get_job(job["id"]).status == JobStatus.BLOCKED
                else:
                    assert reopened.get_job(job["id"]).error_code == "APPROVAL_DENIED"
                    assert reopened.latest_approval(job["id"]).status == ApprovalStatus.REJECTED
            if decision != "approve":
                assert not (tmp_path / "approved.txt").exists()
                assert reopened.list_change_events(job["id"]) == []
            result = await (await client.get(f"/jobs/{job['id']}/result")).json()
            assert result["status"] == reopened.get_job(job["id"]).status
