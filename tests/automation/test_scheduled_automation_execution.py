"""Phase 2C: pinned scheduled runs and atomic occurrence boundaries."""

import asyncio
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context

import pytest

from ai import AIExecutionResult
from application.automation import JobService
from workflows.models import JobStatus
from workflows.runtime.runner import JobRunner
from workflows.runtime.scheduler import Scheduler
from workflows.storage.store import JobStore


START = datetime(2030, 1, 1, tzinfo=UTC)
DUE = START + timedelta(minutes=1)


def saved_schedule(store, workspace, *, retry_limit=0):
    store.create_skill("review", "Original instructions.")
    store.create_automation(
        "daily", "Review {{topic}}.", workspace,
        {"topic": {"type": "string", "default": "changes"}},
        allow_write=True, skill_names=["review"],
    )
    return Scheduler(store).create_automation(
        automation_name="daily", parameters={}, kind="once",
        expression=DUE.isoformat(), timezone="UTC", now=START,
        retry_limit=retry_limit, retry_delay_seconds=1,
    )


def newer_version(store, workspace):
    store.revise_skill("review", "New instructions.")
    return store.revise_automation(
        "daily", "Inspect {{topic}}.", workspace,
        {"topic": {"type": "string", "required": True}},
        allow_command=True, skill_names=["review"],
    )


def test_due_job_uses_snapshot_after_update_and_runner_uses_old_skill(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    schedule = saved_schedule(store, tmp_path)
    old_version = schedule.automation.automation_version_id
    old_skill = schedule.automation.skill_version_ids[0]
    newer_version(store, tmp_path)

    restarted = JobStore(store.path)
    assert Scheduler(restarted).tick(DUE) == 1
    assert Scheduler(store).tick(DUE) == 0
    job = restarted.list_jobs()[0]
    assert (job.source, job.source_ref, job.prompt) == (
        "schedule", schedule.id, "Review changes.",
    )
    assert (job.workspace, job.allow_write, job.allow_command) == (
        str(tmp_path.resolve()), True, False,
    )
    assert job.options == {"sandbox": "process"}
    assert restarted.get_job_automation_version_id(job.id) == old_version
    assert restarted.get_automation_run_parameters(job.id) == {"topic": "changes"}
    assert JobService(restarted).automation_version(job) == 1
    assert restarted.list_automation_skill_versions(old_version)[0][1].id == old_skill
    seen = {}

    async def execute(prompt, **kwargs):
        seen.update(prompt=prompt, **kwargs)
        return AIExecutionResult("done", "completed", None, 1, 1, 0)

    claimed = restarted.claim_next_job()
    asyncio.run(JobRunner(restarted, execute=execute).run(claimed))
    assert restarted.get_job(job.id).status == JobStatus.COMPLETED
    assert seen["prompt"] == "Review changes."
    assert "Original instructions." in seen["skill_instructions"]
    assert "New instructions." not in seen["skill_instructions"]
    assert "apply_workspace_patch" in seen["execution_context"].allowed_tools
    assert "run_command" not in seen["execution_context"].allowed_tools


def test_explicit_upgrade_validates_and_preserves_existing_job(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    schedule = saved_schedule(store, tmp_path)
    assert Scheduler(store).tick(DUE) == 1
    old_job = store.list_jobs()[0]
    version = newer_version(store, tmp_path)
    assert store.get_schedule(schedule.id).automation.automation_version == 1
    with pytest.raises(ValueError, match="automation version not found"):
        store.upgrade_automation_schedule(schedule.id, 9)
    assert store.get_schedule(schedule.id).automation.automation_version == 1

    upgraded = store.upgrade_automation_schedule(schedule.id, "latest")
    assert upgraded.automation.automation_version_id == version.id
    assert upgraded.automation.skill_version_ids != schedule.automation.skill_version_ids
    assert (upgraded.allow_write, upgraded.allow_command) == (False, True)
    assert (upgraded.next_run_at, upgraded.last_run_at) == (None, DUE.isoformat())
    assert store.get_job_automation_version_id(old_job.id) == schedule.automation.automation_version_id
    assert store.get_job(old_job.id).prompt == "Review changes."
    with pytest.raises(ValueError, match="newer automation version"):
        store.upgrade_automation_schedule(schedule.id, "latest")


def test_upgrade_changes_only_future_occurrences(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    store.create_skill("review", "Original instructions.")
    store.create_automation(
        "daily", "Review {{topic}}.", tmp_path,
        {"topic": {"type": "string", "default": "changes"}},
        skill_names=["review"],
    )
    schedule = Scheduler(store).create_automation(
        automation_name="daily", parameters={}, kind="interval",
        expression="60", timezone="UTC", now=START,
    )
    assert Scheduler(store).tick(DUE) == 1
    first = store.claim_next_job()
    assert store.fail_job(first.id, "finished")
    next_version = newer_version(store, tmp_path)
    upgraded = store.upgrade_automation_schedule(schedule.id, "latest")
    assert upgraded.next_run_at == (DUE + timedelta(minutes=1)).isoformat()
    assert Scheduler(JobStore(store.path)).tick(DUE + timedelta(minutes=1)) == 1
    jobs = {job.id: job for job in store.list_jobs()}
    assert len(jobs) == 2
    assert jobs[first.id].prompt == "Review changes."
    second = next(job for job in jobs.values() if job.id != first.id)
    assert second.prompt == "Inspect changes."
    assert store.get_job_automation_version_id(second.id) == next_version.id
    assert store.get_job_automation_version_id(first.id) == schedule.automation.automation_version_id


def test_upgrade_rejects_incompatible_parameters_and_rolls_back_insert_failure(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    schedule = saved_schedule(store, tmp_path)
    store.revise_automation(
        "daily", "Inspect {{count}}.", tmp_path,
        {"count": {"type": "integer", "required": True}},
    )
    with pytest.raises(ValueError):
        store.upgrade_automation_schedule(schedule.id, "latest")
    assert store.get_schedule(schedule.id) == schedule
    with store._connect() as connection:
        connection.execute("""CREATE TRIGGER reject_upgrade BEFORE INSERT
            ON schedule_automation_snapshots BEGIN SELECT RAISE(ABORT, 'upgrade failed'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="upgrade failed"):
        store.upgrade_automation_schedule(schedule.id, 2, {"count": 3})
    assert JobStore(store.path).get_schedule(schedule.id) == schedule
    with store._connect() as connection:
        connection.execute("DROP TRIGGER reject_upgrade")
    assert store.upgrade_automation_schedule(schedule.id, 2, {"count": 3}).automation.parameters == {"count": 3}


def test_retry_and_restart_reuse_one_logical_job_after_upgrade(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    schedule = saved_schedule(store, tmp_path, retry_limit=1)
    assert Scheduler(store).tick(DUE) == 1
    first = store.claim_next_job()
    assert store.fail_job(first.id, "transient")
    newer_version(store, tmp_path)
    store.upgrade_automation_schedule(schedule.id, "latest")
    restarted = JobStore(store.path)
    assert Scheduler(restarted).tick(DUE + timedelta(seconds=2)) == 1
    assert Scheduler(JobStore(store.path)).tick(DUE + timedelta(seconds=2)) == 0
    events = restarted.list_trigger_history(schedule.id)
    assert len(events) == 2
    assert {event.job_id for event in events} == {first.id}
    assert restarted.get_job_automation_version_id(first.id) == schedule.automation.automation_version_id
    assert len(restarted.list_jobs()) == 1


@pytest.mark.parametrize("table", ["automation_run_parameters", "trigger_history"])
def test_trigger_partial_failure_rolls_back_job_and_schedule(tmp_path, table):
    store = JobStore(tmp_path / "suto.db")
    schedule = saved_schedule(store, tmp_path)
    with store._connect() as connection:
        connection.execute(f"""CREATE TRIGGER reject_fire BEFORE INSERT ON {table}
            BEGIN SELECT RAISE(ABORT, 'fire failed'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="fire failed"):
        Scheduler(store).tick(DUE)
    assert store.list_jobs() == []
    assert store.list_trigger_history(schedule.id) == []
    assert store.get_schedule(schedule.id).next_run_at == DUE.isoformat()
    with store._connect() as connection:
        connection.execute("DROP TRIGGER reject_fire")
    assert Scheduler(JobStore(store.path)).tick(DUE) == 1


def _crash_on_trigger_insert(path):
    store = JobStore(path)
    original_connect = store._connect

    @contextmanager
    def crashing_connect():
        with original_connect() as connection:
            connection.create_function("crash_now", 0, lambda: os._exit(73))
            yield connection

    store._connect = crashing_connect
    with store._connect() as connection:
        connection.execute("""CREATE TRIGGER crash_fire AFTER INSERT ON trigger_history
            BEGIN SELECT crash_now(); END""")
    Scheduler(store).tick(DUE)
    os._exit(70)


def test_crash_before_trigger_commit_recovers_one_automation_job(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    schedule = saved_schedule(store, tmp_path)
    process = get_context("spawn").Process(target=_crash_on_trigger_insert, args=(store.path,))
    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("crash injection process did not exit")
    assert process.exitcode == 73
    restarted = JobStore(store.path)
    with restarted._connect() as connection:
        connection.execute("DROP TRIGGER crash_fire")
    assert restarted.list_jobs() == []
    assert restarted.list_trigger_history(schedule.id) == []
    assert restarted.get_schedule(schedule.id).next_run_at == DUE.isoformat()
    assert Scheduler(restarted).tick(DUE) == 1
    assert Scheduler(JobStore(store.path)).tick(DUE) == 0
    assert len(restarted.list_jobs()) == 1
