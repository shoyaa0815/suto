"""Pinned scheduled runs and atomic occurrence boundaries."""

import asyncio
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from threading import Event

import pytest

from ai import AIExecutionResult
from application.automation import JobService
from workflows.runtime.context import ApprovalRequired
from workflows.models import JobStatus
from workflows.runtime.runner import JobRunner
from workflows.runtime import scheduler as scheduler_module
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


def test_upgrade_rolls_back_snapshot_when_schedule_update_fails(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    schedule = saved_schedule(store, tmp_path)
    newer_version(store, tmp_path)
    with store._connect() as connection:
        original = connection.execute(
            "SELECT * FROM schedule_automation_snapshots WHERE schedule_id=?", (schedule.id,),
        ).fetchone()
        connection.execute("""CREATE TRIGGER reject_schedule_upgrade BEFORE UPDATE ON schedules
            BEGIN SELECT RAISE(ABORT, 'schedule update failed'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="schedule update failed"):
        store.upgrade_automation_schedule(schedule.id, "latest")
    reopened = JobStore(store.path)
    assert reopened.get_schedule(schedule.id) == schedule
    with reopened._connect() as connection:
        assert dict(connection.execute(
            "SELECT * FROM schedule_automation_snapshots WHERE schedule_id=?", (schedule.id,),
        ).fetchone()) == dict(original)
        connection.execute("DROP TRIGGER reject_schedule_upgrade")


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


def test_automation_retry_insert_failure_rolls_back_requeue(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    schedule = saved_schedule(store, tmp_path, retry_limit=1)
    assert Scheduler(store).tick(DUE) == 1
    job = store.claim_next_job()
    assert store.fail_job(job.id, "transient")
    with store._connect() as connection:
        connection.execute("""CREATE TRIGGER reject_retry BEFORE INSERT ON trigger_history
            WHEN NEW.attempt = 2 BEGIN SELECT RAISE(ABORT, 'retry insert failed'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="retry insert failed"):
        Scheduler(store).tick(DUE + timedelta(seconds=2))
    restarted = JobStore(store.path)
    assert restarted.get_job(job.id).status == JobStatus.FAILED
    assert restarted.get_job(job.id).retry_count == 0
    assert len(restarted.list_trigger_history(schedule.id)) == 1
    with restarted._connect() as connection:
        connection.execute("DROP TRIGGER reject_retry")
    assert Scheduler(restarted).tick(DUE + timedelta(seconds=2)) == 1
    assert {item.job_id for item in restarted.list_trigger_history(schedule.id)} == {job.id}
    assert len(restarted.list_jobs()) == 1


def test_missed_run_policy_preserves_pinned_automation(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    store.create_skill("review", "Original instructions.")
    store.create_automation(
        "daily", "Review {{topic}}.", tmp_path,
        {"topic": {"type": "string", "default": "changes"}},
        skill_names=["review"],
    )
    interval = Scheduler(store).create_automation(
        automation_name="daily", parameters={}, kind="interval", expression="60",
        timezone="UTC", now=START, missed_run_policy="run_once",
    )
    skipped = Scheduler(store).create_automation(
        automation_name="daily", parameters={}, kind="interval", expression="60",
        timezone="UTC", now=START, missed_run_policy="skip",
    )
    newer_version(store, tmp_path)
    late = START + timedelta(minutes=5)
    assert Scheduler(JobStore(store.path)).tick(late) == 1
    jobs = {job.source_ref: job for job in store.list_jobs()}
    assert set(jobs) == {interval.id}
    assert jobs[interval.id].prompt == "Review changes."
    assert store.get_job_automation_version_id(jobs[interval.id].id) == interval.automation.automation_version_id
    assert store.get_schedule(interval.id).next_run_at == (late + timedelta(minutes=1)).isoformat()
    assert store.get_schedule(skipped.id).next_run_at == (late + timedelta(minutes=1)).isoformat()
    assert store.list_trigger_history(skipped.id)[0].job_id is None


def test_skip_large_backlog_keeps_interval_cadence_without_replaying_it(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "suto.db")
    store.create_automation("daily", "Review.", tmp_path, {})
    schedule = Scheduler(store).create_automation(
        automation_name="daily", parameters={}, kind="interval",
        expression="1", timezone="UTC", now=START,
        missed_run_policy="skip",
    )
    real_next = scheduler_module.next_occurrence
    calls = 0

    def bounded_next(item, after):
        nonlocal calls
        calls += 1
        assert calls <= 2, "skip must not traverse every missed occurrence"
        return real_next(item, after)

    monkeypatch.setattr(scheduler_module, "next_occurrence", bounded_next)
    late = START + timedelta(days=2, milliseconds=500)
    assert Scheduler(store).tick(late) == 0
    assert store.get_schedule(schedule.id).next_run_at == (
        START + timedelta(days=2, seconds=1)
    ).isoformat()
    history = store.list_trigger_history(schedule.id)
    assert len(history) == 1
    assert history[0].job_id is None
    assert history[0].scheduled_for == (START + timedelta(seconds=1)).isoformat()


def test_skip_large_cron_backlog_selects_next_calendar_occurrence(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "suto.db")
    store.create_automation("daily", "Review.", tmp_path, {})
    schedule = Scheduler(store).create_automation(
        automation_name="daily", parameters={}, kind="cron",
        expression="0 0 * * *", timezone="UTC", now=START,
        missed_run_policy="skip",
    )
    real_next = scheduler_module.next_occurrence
    calls = 0

    def bounded_next(item, after):
        nonlocal calls
        calls += 1
        assert calls <= 2, "skip must not traverse every missed occurrence"
        return real_next(item, after)

    monkeypatch.setattr(scheduler_module, "next_occurrence", bounded_next)
    late = START + timedelta(days=366 * 5, hours=12)
    assert Scheduler(store).tick(late) == 0
    assert store.get_schedule(schedule.id).next_run_at == (
        late.replace(hour=0) + timedelta(days=1)
    ).isoformat()
    assert len(store.list_trigger_history(schedule.id)) == 1
    assert store.list_jobs() == []


def test_scheduled_job_keeps_approval_and_sandbox_guard(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    schedule = saved_schedule(store, tmp_path)
    assert Scheduler(store).tick(DUE) == 1
    job = store.claim_next_job()
    seen = []

    async def execute(prompt, execution_context, **kwargs):
        seen.append((execution_context.sandbox, execution_context.allowed_tools))
        assert execution_context.can_tool("apply_workspace_patch")
        assert not execution_context.can_tool("run_workspace_command")
        try:
            execution_context.require_approval(
                "write", {"path": "note.txt", "content": "review"},
                "write note", "review",
            )
        except ApprovalRequired as error:
            return AIExecutionResult(str(error), "waiting_approval", str(error), 0, 0, 0)
        return AIExecutionResult("done", "completed", None, 0, 0, 0)

    asyncio.run(JobRunner(store, execute=execute).run(job))
    assert store.get_job(job.id).status == JobStatus.WAITING_APPROVAL
    assert store.latest_approval(job.id).status.value == "pending"
    assert Scheduler(JobStore(store.path)).tick(DUE) == 0
    assert len(store.list_jobs()) == 1
    assert store.decide_approval(job.id, True)[0]
    asyncio.run(JobRunner(store, execute=execute).run(store.claim_next_job()))
    assert store.get_job(job.id).status == JobStatus.COMPLETED
    assert store.latest_approval(job.id).status.value == "consumed"
    assert [sandbox for sandbox, _ in seen] == ["process", "process"]
    assert {event.job_id for event in store.list_trigger_history(schedule.id)} == {job.id}


def test_concurrent_ticks_materialize_one_occurrence(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    schedule = saved_schedule(store, tmp_path)
    stores = [JobStore(store.path), JobStore(store.path)]
    ready = Event()

    def tick(other_store):
        ready.wait()
        return Scheduler(other_store).tick(DUE)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(tick, other_store) for other_store in stores]
        ready.set()
        assert sorted(result.result(timeout=15) for result in results) == [0, 1]
    jobs = store.list_jobs()
    assert len(jobs) == 1
    assert [event.job_id for event in store.list_trigger_history(schedule.id)] == [jobs[0].id]


def test_restart_after_worker_claim_keeps_same_scheduled_job(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    schedule = saved_schedule(store, tmp_path)
    assert Scheduler(store).tick(DUE) == 1
    claimed = store.claim_next_job()
    restarted = JobStore(store.path)
    assert restarted.recover_interrupted_jobs() == 1
    assert restarted.get_job(claimed.id).status == JobStatus.INTERRUPTED
    assert Scheduler(restarted).tick(DUE) == 0
    assert restarted.resume_job(claimed.id)
    resumed = restarted.claim_next_job()
    assert resumed.id == claimed.id
    assert resumed.attempt_count == 2
    assert len(restarted.list_jobs()) == 1
    assert [event.job_id for event in restarted.list_trigger_history(schedule.id)] == [claimed.id]


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


def _crash_at_fire_boundary(path, boundary):
    store = JobStore(path)
    if boundary == "after_commit":
        original_fire = store.fire_schedule

        def fire_then_crash(*args, **kwargs):
            original_fire(*args, **kwargs)
            os._exit(74)

        store.fire_schedule = fire_then_crash
    else:
        original_connect = store._connect

        @contextmanager
        def crashing_connect():
            with original_connect() as connection:
                connection.create_function("crash_now", 0, lambda: os._exit(73))
                yield connection

        store._connect = crashing_connect
        event, table = {
            "job": ("AFTER INSERT", "jobs"),
            "parameters": ("AFTER INSERT", "automation_run_parameters"),
            "history": ("AFTER INSERT", "trigger_history"),
            "schedule": ("AFTER UPDATE", "schedules"),
        }[boundary]
        with store._connect() as connection:
            connection.execute(f"""CREATE TRIGGER crash_fire {event} ON {table}
                BEGIN SELECT crash_now(); END""")
    Scheduler(store).tick(DUE)
    os._exit(70)


@pytest.mark.parametrize("boundary", ["job", "parameters", "history", "schedule", "after_commit"])
def test_crash_at_each_fire_boundary_keeps_one_logical_job(tmp_path, boundary):
    store = JobStore(tmp_path / "suto.db")
    schedule = saved_schedule(store, tmp_path)
    process = get_context("spawn").Process(
        target=_crash_at_fire_boundary, args=(store.path, boundary),
    )
    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("crash injection process did not exit")
    assert process.exitcode == (74 if boundary == "after_commit" else 73)
    restarted = JobStore(store.path)
    if boundary != "after_commit":
        with restarted._connect() as connection:
            connection.execute("DROP TRIGGER crash_fire")
        assert restarted.list_jobs() == []
        assert restarted.list_trigger_history(schedule.id) == []
        assert restarted.get_schedule(schedule.id).next_run_at == DUE.isoformat()
    assert Scheduler(restarted).tick(DUE) == (0 if boundary == "after_commit" else 1)
    assert Scheduler(JobStore(store.path)).tick(DUE) == 0
    jobs = restarted.list_jobs()
    history = restarted.list_trigger_history(schedule.id)
    assert len(jobs) == len(history) == 1
    assert history[0].job_id == jobs[0].id
    assert restarted.get_job_automation_version_id(jobs[0].id) == schedule.automation.automation_version_id
    with restarted._connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("column,value,error", [
    ("automation_name", "other", "version is invalid"),
    ("skill_version_ids", "[]", "skill references changed"),
    ("options", '{"sandbox":"bwrap"}', "sandbox is invalid"),
])
def test_corrupt_snapshot_never_queues_job(tmp_path, column, value, error):
    store = JobStore(tmp_path / "suto.db")
    schedule = saved_schedule(store, tmp_path)
    with store._connect() as connection:
        connection.execute("DROP TRIGGER schedule_automation_snapshot_immutable")
        connection.execute(
            f"UPDATE schedule_automation_snapshots SET {column}=? WHERE schedule_id=?",
            (value, schedule.id),
        )
    restarted = JobStore(store.path)
    with pytest.raises(ValueError, match=error):
        Scheduler(restarted).tick(DUE)
    assert restarted.list_jobs() == []
    assert restarted.list_trigger_history(schedule.id) == []
    assert restarted.get_schedule(schedule.id).next_run_at == DUE.isoformat()


def test_missing_snapshot_cannot_fall_back_to_blank_prompt(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    schedule = saved_schedule(store, tmp_path)
    with store._connect() as connection:
        connection.execute(
            "DELETE FROM schedule_automation_snapshots WHERE schedule_id=?", (schedule.id,),
        )
    restarted = JobStore(store.path)
    with pytest.raises(ValueError, match="snapshot is missing"):
        restarted.get_schedule(schedule.id)
    with pytest.raises(ValueError, match="snapshot is missing"):
        Scheduler(restarted).tick(DUE)
    with pytest.raises(ValueError, match="snapshot is missing"):
        restarted.fire_schedule(schedule.id, DUE.isoformat(), None)
    assert restarted.list_jobs() == []
    assert restarted.list_trigger_history(schedule.id) == []
