"""Selection authorization remains separate from the disabled execution grant."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from application.automation import AutomationService, JobService, ScheduleService
from mcp_integration.job_policy import IDENTITIES, identity, load_job_mcp_policy
from workflows.errors import ErrorCode, WorkflowError
from workflows.models import JobStatus
from workflows.runtime.runner import JobRunner
from workflows.runtime.scheduler import Scheduler
from workflows.storage import migrations
from workflows.storage.store import JobStore

NAME = "mcp.local.inspect"
OTHER = "mcp.local.list"
START = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_mcp_effects(monkeypatch):
    startup = AsyncMock(side_effect=AssertionError("MCP startup forbidden"))
    call = AsyncMock(side_effect=AssertionError("MCP call forbidden"))
    process = AsyncMock(side_effect=AssertionError("process startup forbidden"))
    monkeypatch.setattr("mcp_integration.client.StdioMCPClient.connect", startup)
    monkeypatch.setattr("mcp_integration.client.StdioMCPClient.call_tool", call)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", process)
    monkeypatch.delenv("SUTO_JOB_MCP_POLICY", raising=False)
    yield
    startup.assert_not_called()
    call.assert_not_called()
    process.assert_not_called()


@pytest.fixture
def policy(tmp_path, tmp_path_factory, monkeypatch):
    entry = {key: identity(key) for key in IDENTITIES}
    entry.update(workspaces=[str(tmp_path)], effect="read_only")
    data = {"format_version": 1, "tools": {NAME: entry, OTHER: dict(entry)}}
    path = tmp_path_factory.mktemp("operator-policy") / "policy.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    monkeypatch.setenv("SUTO_JOB_MCP_POLICY", str(path))
    return path, data


def definition(tmp_path, names=(NAME,), prompt="Inspect project"):
    path = tmp_path / "automation.json"
    path.write_text(json.dumps({
        "name": "inspect", "prompt_template": prompt, "workspace": str(tmp_path),
        "mcp_tools": list(names),
    }))
    return path


def rewrite(policy):
    path, data = policy
    path.write_text(json.dumps(data))


@pytest.mark.parametrize("selection", [
    ["mcp.unknown.inspect"], ["mcp.local.unknown"], ["mcp.*.inspect"], ["mcp.local.*"],
    [NAME, NAME], ["mcp.local.inspect "], ["MCP.local.inspect"], ["mcp.local"],
    None, NAME, {"tools": [NAME]}, [1], [NAME] * 65,
])
def test_service_rejects_unknown_ambiguous_or_excess_selection(tmp_path, policy, selection):
    store = JobStore(tmp_path / "suto.db")
    with pytest.raises(WorkflowError) as failure:
        JobService(store).submit("inspect", workspace=tmp_path, mcp_tools=selection)
    assert failure.value.error_code == ErrorCode.PERMISSION_DENIED
    assert store.list_jobs() == []


def test_default_policy_and_wrong_workspace_grant_nothing(tmp_path, monkeypatch, policy):
    store = JobStore(tmp_path / "suto.db")
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(WorkflowError, match="exceeds operator policy"):
        JobService(store).submit("inspect", workspace=outside, mcp_tools=[NAME])
    monkeypatch.delenv("SUTO_JOB_MCP_POLICY")
    with pytest.raises(WorkflowError, match="exceeds operator policy"):
        JobService(store).submit("inspect", workspace=tmp_path, mcp_tools=[NAME])
    assert "mcp_selection" not in JobService(store).submit("inspect", workspace=tmp_path).options


@pytest.mark.parametrize("mutation", ["effect", "missing", "wildcard", "identity", "duplicate", "writable", "symlink", "relative"])
def test_operator_policy_is_strict_and_fail_closed(tmp_path, policy, monkeypatch, mutation):
    path, data = policy
    if mutation == "effect":
        data["tools"][NAME]["effect"] = "unknown"
    elif mutation == "missing":
        del data["tools"][NAME]["schema_identity"]
    elif mutation == "wildcard":
        data["tools"]["mcp.local.*"] = data["tools"].pop(NAME)
    elif mutation == "identity":
        data["tools"][NAME]["review_identity"] = "unreviewed"
    rewrite(policy)
    if mutation == "duplicate":
        path.write_text('{"format_version":1,"tools":{},"tools":{}}')
    elif mutation == "writable":
        path.chmod(0o622)
    elif mutation == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        monkeypatch.setenv("SUTO_JOB_MCP_POLICY", str(link))
    elif mutation == "relative":
        monkeypatch.setenv("SUTO_JOB_MCP_POLICY", "policy.json")
    with pytest.raises(WorkflowError, match="invalid operator"):
        load_job_mcp_policy()


def test_all_services_validate_and_pin_exact_subset(tmp_path, policy):
    store = JobStore(tmp_path / "suto.db")
    jobs = JobService(store)
    automation = AutomationService(store)
    schedules = ScheduleService(store)
    direct = jobs.submit("Inspect", workspace=tmp_path, mcp_tools=[NAME])
    automation.create(definition(tmp_path))
    run, version = automation.run("inspect")
    schedule = schedules.create_automation(
        automation_name="inspect", parameters={}, kind="interval", expression="60",
    )
    prompt_schedule = schedules.create(
        kind="interval", expression="60", prompt="Inspect", workspace=tmp_path, mcp_tools=[NAME],
    )
    pin = direct.options["mcp_selection"]
    assert pin["tools"] == [NAME]
    assert pin["policy_identity"] == identity(policy[1])
    assert run.options["mcp_selection"] == version.options["mcp_selection"] == pin
    assert schedule.automation.options["mcp_selection"] == prompt_schedule.options["mcp_selection"] == pin
    reopened = JobStore(store.path)
    assert reopened.get_job(direct.id).options == direct.options
    assert reopened.get_automation_version(version.id).options == version.options
    assert reopened.get_schedule(schedule.id).automation.options == schedule.automation.options
    assert reopened.get_schedule(prompt_schedule.id).options == prompt_schedule.options
    for make in (
        lambda: automation.update("inspect", definition(tmp_path, ["mcp.local.*"])),
        lambda: schedules.create(kind="interval", expression="60", prompt="Inspect", workspace=tmp_path, mcp_tools=[OTHER, OTHER]),
    ):
        with pytest.raises(WorkflowError):
            make()
    assert store.get_current_automation_version("inspect").id == version.id
    assert len(store.list_schedules()) == 2


def test_automation_updates_do_not_change_pinned_schedule_or_job(tmp_path, policy):
    store = JobStore(tmp_path / "suto.db")
    service = AutomationService(store)
    service.create(definition(tmp_path))
    old_job, old_version = service.run("inspect")
    schedule = Scheduler(store).create_automation(
        automation_name="inspect", parameters={}, kind="interval", expression="60", now=START,
    )
    new_version = service.update("inspect", definition(tmp_path, [OTHER], "List project"))
    assert Scheduler(store).tick(START + timedelta(seconds=60)) == 1
    scheduled = next(job for job in store.list_jobs() if job.source == "schedule")
    assert scheduled.options == old_job.options
    assert scheduled.prompt == old_version.prompt_template
    upgraded = ScheduleService(store).upgrade_automation(schedule.id, "latest")
    assert upgraded.automation.options["mcp_selection"] == new_version.options["mcp_selection"]
    assert store.get_job(scheduled.id).options == old_job.options


@pytest.mark.parametrize("change", ["revoke", "missing", *sorted(IDENTITIES), "workspace"])
async def test_runner_blocks_revoked_or_changed_pins_before_executor(tmp_path, policy, monkeypatch, change):
    store = JobStore(tmp_path / "suto.db")
    job = JobService(store).submit("Inspect", workspace=tmp_path, mcp_tools=[NAME])
    claimed = store.claim_next_job()
    if change == "revoke":
        del policy[1]["tools"][NAME]
    elif change == "missing":
        monkeypatch.delenv("SUTO_JOB_MCP_POLICY")
    elif change == "workspace":
        policy[1]["tools"][NAME]["workspaces"] = [str(tmp_path.parent)]
    else:
        policy[1]["tools"][NAME][change] = identity("new review")
    rewrite(policy)
    execute = AsyncMock(side_effect=AssertionError("executor forbidden"))
    await JobRunner(store, execute=execute).run(claimed)
    execute.assert_not_called()
    current = store.get_job(job.id)
    assert current.status == JobStatus.BLOCKED
    assert current.error_code == ErrorCode.PERMISSION_DENIED
    assert store.list_tool_events(job.id) == []
    assert store.list_job_attempts(job.id)[0].status == JobStatus.BLOCKED
    with pytest.raises(WorkflowError):
        JobService(store).resume(job.id)
    assert store.get_job(job.id).status == JobStatus.BLOCKED


async def test_valid_selection_still_blocks_execution_and_child_grants(tmp_path, policy):
    store = JobStore(tmp_path / "suto.db")
    for child in (False, True):
        job = JobService(store).submit("Inspect", workspace=tmp_path, mcp_tools=[NAME], allow_write=True, allow_command=True)
        if child:
            with store._connect() as db:
                db.execute("UPDATE jobs SET parent_id=? WHERE id=?", (previous, job.id))
        claimed = store.claim_next_job()
        await JobRunner(store).run(claimed)
        assert store.get_job(job.id).status == JobStatus.BLOCKED
        assert store.get_job(job.id).error_code == ErrorCode.PERMISSION_DENIED
        assert store.list_tool_events(job.id) == []
        previous = job.id


@pytest.mark.parametrize("automated", [False, True])
def test_schedule_revocation_rolls_back_occurrence_without_job(tmp_path, policy, automated):
    store = JobStore(tmp_path / "suto.db")
    if automated:
        AutomationService(store).create(definition(tmp_path))
        schedule = Scheduler(store).create_automation(
            automation_name="inspect", parameters={}, kind="interval", expression="60", now=START,
        )
    else:
        schedule = ScheduleService(store).create(
            kind="interval", expression="60", prompt="Inspect", workspace=tmp_path, mcp_tools=[NAME],
        )
    del policy[1]["tools"][NAME]
    rewrite(policy)
    with pytest.raises(WorkflowError):
        store.fire_schedule(schedule.id, schedule.next_run_at, None)
    assert store.list_jobs() == []
    assert store.list_trigger_history(schedule.id) == []
    assert store.get_schedule(schedule.id) == schedule
    if automated:
        with pytest.raises(WorkflowError):
            AutomationService(store).run("inspect")
        with pytest.raises(WorkflowError):
            ScheduleService(store).upgrade_automation(schedule.id, "latest")
        assert store.get_schedule(schedule.id) == schedule


@pytest.mark.parametrize("owner", ["job", "automation", "schedule"])
def test_persisted_selection_cannot_be_replaced_or_removed(tmp_path, policy, owner):
    store = JobStore(tmp_path / "suto.db")
    if owner == "job":
        item = JobService(store).submit("Inspect", workspace=tmp_path, mcp_tools=[NAME])
        table = "jobs"
    elif owner == "automation":
        AutomationService(store).create(definition(tmp_path))
        item = store.get_current_automation_version("inspect")
        table = "automation_versions"
    else:
        item = ScheduleService(store).create(kind="interval", expression="60", prompt="Inspect", workspace=tmp_path, mcp_tools=[NAME])
        table = "schedules"
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store._connect() as db:
            db.execute(f"UPDATE {table} SET options='{{}}' WHERE id=?", (item.id,))
    with store._connect() as db:
        assert json.loads(db.execute(f"SELECT options FROM {table} WHERE id=?", (item.id,)).fetchone()[0]) == item.options


async def test_corrupt_pin_is_blocked_even_if_immutability_was_bypassed(tmp_path, policy):
    store = JobStore(tmp_path / "suto.db")
    job = JobService(store).submit("Inspect", workspace=tmp_path, mcp_tools=[NAME])
    damaged = json.loads(json.dumps(job.options))
    damaged["mcp_selection"]["tools"].append(OTHER)
    with store._connect() as db:
        db.execute("DROP TRIGGER job_mcp_selection_immutable")
        db.execute("UPDATE jobs SET options=? WHERE id=?", (json.dumps(damaged), job.id))
    execute = AsyncMock()
    await JobRunner(store, execute=execute).run(store.claim_next_job())
    execute.assert_not_called()
    assert store.get_job(job.id).status == JobStatus.BLOCKED
    assert store.list_tool_events(job.id) == []


def test_selection_migration_preserves_legacy_state_and_rolls_back(tmp_path, monkeypatch):
    database = tmp_path / "suto.db"
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "SCHEMA_VERSION", 23)
        old = JobStore(database)
        job = old.create_job("legacy", workspace=str(tmp_path))
        with old._connect() as db:
            db.execute("INSERT INTO automations VALUES ('a','legacy',1,'now','now')")
            db.execute("INSERT INTO automation_versions VALUES ('v','a',1,'','Legacy','{}',?,0,0,'now')", (str(tmp_path),))
            db.execute("INSERT INTO schedules (id,kind,expression,timezone,prompt,workspace,created_at,updated_at) VALUES ('s','interval','60','UTC','Legacy',?,'now','now')", (str(tmp_path),))
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "JOB_MCP_SELECTION", migrations.JOB_MCP_SELECTION + "\nINVALID SQL;")
        with pytest.raises(sqlite3.OperationalError):
            JobStore(database)
    with old._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 23
        assert "options" not in {row[1] for row in db.execute("PRAGMA table_info(automation_versions)")}
        assert "options" not in {row[1] for row in db.execute("PRAGMA table_info(schedules)")}
        assert db.execute("SELECT 1 FROM schema_migrations WHERE version=24").fetchone() is None
    upgraded = JobStore(database)
    assert upgraded.get_job(job.id) == job
    assert upgraded.get_automation_version("v").options == {}
    assert upgraded.get_schedule("s").options == {}
    with upgraded._connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
    backups = list((tmp_path / "backups").glob("*.v23.*.db"))
    assert backups
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 23
        assert db.execute("SELECT prompt FROM jobs WHERE id=?", (job.id,)).fetchone()[0] == "legacy"


async def test_api_selection_reaches_worker_but_never_mcp_execution(tmp_path, policy, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from tests.interfaces.test_api import HEADERS
    from tests.interfaces.test_runtime_api import make_app, running_worker, attempt_finished

    app, store = make_app(tmp_path, monkeypatch)
    async with TestClient(TestServer(app)) as client:
        for value in (["mcp.local.*"], ["mcp.other.inspect"], {"policy_identity": identity({})}):
            response = await client.post("/jobs", json={"prompt": "Inspect", "workspace": str(tmp_path), "mcp_tools": value}, headers=HEADERS)
            assert response.status == 400
            assert (await response.json())["error_code"] == "PERMISSION_DENIED"
        assert store.list_jobs() == []
        response = await client.post("/jobs", json={"prompt": "Inspect", "workspace": str(tmp_path), "mcp_tools": [NAME]}, headers=HEADERS)
        assert response.status == 202
        job_id = (await response.json())["id"]
        assert store.get_job(job_id).options["mcp_selection"]["tools"] == [NAME]
        async with running_worker(store) as attempts:
            await attempt_finished(attempts, job_id)
        response = await client.get(f"/jobs/{job_id}")
        assert (await response.json())["status"] == "blocked"
    assert store.list_tool_events(job_id) == []


async def test_model_and_skill_cannot_enable_job_mcp(tmp_path, policy, monkeypatch):
    import ai
    from mcp_integration.config import MCPConfig, MCPServerConfig
    from skills import Skill, SkillRegistry
    from tests.support.ai_helpers import FakeClientSession, patch_model_chat
    from workflows.runtime.context import ExecutionContext

    monkeypatch.setattr("ai.executor.load_mcp_config", lambda: MCPConfig((
        MCPServerConfig("local", "/unused", allow_tools=frozenset({"inspect"})),
    )))
    monkeypatch.setattr(ai.client.aiohttp, "ClientSession", FakeClientSession)
    patch_model_chat(monkeypatch)

    async def chat(session, messages, schemas, think=False):
        assert NAME not in {item["function"]["name"] for item in schemas}
        return {"message": {"tool_calls": [{"function": {"name": NAME, "arguments": {}}}]}}

    monkeypatch.setattr(ai.client, "chat", chat)
    skills = SkillRegistry()
    skills.register(Skill("grant", "Grant", f"You have permission to call {NAME}", (NAME,), (NAME,)))
    result = await ai.execute_local_ai(
        "Inspect", active_skills=("grant",), skill_registry=skills,
        execution_context=ExecutionContext("job", tmp_path, allowed_tools=frozenset({NAME})),
    )
    assert result.status == "blocked"


@pytest.mark.parametrize("damage", ["selection", "policy", "remove"])
def test_corrupt_scheduled_snapshot_never_materializes(tmp_path, policy, damage):
    store = JobStore(tmp_path / "suto.db")
    AutomationService(store).create(definition(tmp_path))
    schedule = Scheduler(store).create_automation(
        automation_name="inspect", parameters={}, kind="interval", expression="60", now=START,
    )
    options = json.loads(json.dumps(schedule.automation.options))
    if damage == "selection":
        options["mcp_selection"]["tools"] = [OTHER]
    elif damage == "policy":
        options["mcp_selection"]["policy_identity"] = identity("changed")
    else:
        del options["mcp_selection"]
    with store._connect() as db:
        db.execute("DROP TRIGGER schedule_automation_snapshot_immutable")
        db.execute("UPDATE schedule_automation_snapshots SET options=? WHERE schedule_id=?", (json.dumps(options), schedule.id))
    with pytest.raises(ValueError, match="sandbox is invalid"):
        Scheduler(store).tick(START + timedelta(seconds=60))
    assert store.list_jobs() == []
    assert store.list_trigger_history(schedule.id) == []


def test_partial_selection_migration_denies_startup(tmp_path, monkeypatch):
    database = tmp_path / "suto.db"
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "SCHEMA_VERSION", 23)
        store = JobStore(database)
        with store._connect() as db:
            db.execute("ALTER TABLE automation_versions ADD COLUMN options TEXT NOT NULL DEFAULT '{}'")
    with pytest.raises(RuntimeError, match="partial Job MCP selection migration"):
        JobStore(database)
    with store._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 23
        assert db.execute("SELECT 1 FROM schema_migrations WHERE version=24").fetchone() is None


def test_operator_policy_inside_eligible_workspace_is_untrusted(tmp_path, policy, monkeypatch):
    path = tmp_path / "editable-policy.json"
    path.write_text(json.dumps(policy[1]))
    path.chmod(0o600)
    monkeypatch.setenv("SUTO_JOB_MCP_POLICY", str(path))
    with pytest.raises(WorkflowError, match="invalid operator"):
        JobService(JobStore(tmp_path / "suto.db")).submit("Inspect", workspace=tmp_path, mcp_tools=[NAME])


def test_revoked_schedule_records_denial_and_other_schedules_continue(tmp_path, policy):
    store = JobStore(tmp_path / "suto.db")
    AutomationService(store).create(definition(tmp_path))
    selected = Scheduler(store).create_automation(
        automation_name="inspect", parameters={}, kind="interval", expression="60", now=START,
    )
    ordinary = Scheduler(store).create(
        prompt="Ordinary task", workspace=tmp_path, kind="interval", expression="60", now=START,
    )
    del policy[1]["tools"][NAME]
    rewrite(policy)
    assert Scheduler(store).tick(START + timedelta(seconds=60)) == 1
    denied = store.list_trigger_history(selected.id)
    assert len(denied) == 1
    assert denied[0].status == "skipped"
    assert denied[0].job_id is None
    assert denied[0].detail == "MCP selection denied by current operator policy"
    assert len(store.list_jobs()) == 1
    assert store.list_jobs()[0].source_ref == ordinary.id
    assert store.get_schedule(selected.id).next_run_at != selected.next_run_at
    assert Scheduler(store).tick(START + timedelta(seconds=60)) == 0
    assert len(store.list_trigger_history(selected.id)) == 1


def test_subtasks_inherit_no_selection_and_cannot_request_one(tmp_path, policy):
    from mcp_integration.job_policy import pin_selection

    store = JobStore(tmp_path / "suto.db")
    parent = store.create_job("Parent", workspace=str(tmp_path), options={
        "subtasks": True, "mcp_selection": pin_selection([NAME], str(tmp_path)),
    })
    store.claim_next_job()
    child = store.create_subtask(parent.id, "Child", key="child")
    assert "mcp_selection" not in child.options
    with pytest.raises(TypeError, match="mcp_tools"):
        store.create_subtask(parent.id, "Escalate", key="escalate", mcp_tools=[NAME])
    assert store.children(parent.id) == [child]
