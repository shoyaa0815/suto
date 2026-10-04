"""Crash and rollback boundaries for the public automation lifecycle."""

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context

import pytest

from application.automation import JobService
from workflows.models import JobStatus, StepStatus
from workflows.runtime.scheduler import Scheduler
from workflows.storage.store import JobStore


def _crash_at_database_write(path: str, operation: str, point: str, due: str = "") -> None:
    store = JobStore(path)
    if point == "scheduled_job_insert":
        insert = store._insert_scheduled_job

        def crash_after_insert(connection, schedule):
            insert(connection, schedule)
            os._exit(71)

        store._insert_scheduled_job = crash_after_insert
    else:
        original_connect = store._connect

        @contextmanager
        def connect_with_crash_function():
            with original_connect() as connection:
                connection.create_function("phase1f_crash", 0, lambda: os._exit(72))
                yield connection

        store._connect = connect_with_crash_function
        statements = {
            "trigger_insert": """CREATE TRIGGER phase1f_crash_trigger AFTER INSERT ON trigger_history
                BEGIN SELECT phase1f_crash(); END""",
            "worker_claim": """CREATE TRIGGER phase1f_crash_trigger AFTER UPDATE OF status ON jobs
                WHEN NEW.status = 'running' BEGIN SELECT phase1f_crash(); END""",
            "checkpoint_recovery": """CREATE TRIGGER phase1f_crash_trigger AFTER UPDATE OF status ON job_steps
                WHEN NEW.status = 'pending' BEGIN SELECT phase1f_crash(); END""",
        }
        with store._connect() as connection:
            connection.execute(statements[point])

    if operation == "schedule":
        Scheduler(store).tick(datetime.fromisoformat(due))
    elif operation == "claim":
        store.claim_next_job()
    else:
        store.recover_interrupted_jobs()
    os._exit(70)  # Reaching here means the intended crash point was missed.


def _run_crashing_process(path, operation, point, due=""):
    process = get_context("spawn").Process(
        target=_crash_at_database_write,
        args=(str(path), operation, point, due),
    )
    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("crash injection process did not exit")
    assert process.exitcode == (71 if point == "scheduled_job_insert" else 72)
    if point != "scheduled_job_insert":
        with JobStore(path)._connect() as connection:
            connection.execute("DROP TRIGGER phase1f_crash_trigger")


@pytest.mark.parametrize("point", ["scheduled_job_insert", "trigger_insert"])
def test_crash_during_schedule_fire_recovers_one_occurrence(tmp_path, point):
    path = tmp_path / "jobs.db"
    store = JobStore(path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    due = start + timedelta(minutes=1)
    schedule = Scheduler(store).create(
        kind="once", expression=due.isoformat(), prompt="run once", now=start,
    )

    _run_crashing_process(path, "schedule", point, due.isoformat())

    restarted = JobStore(path)
    assert restarted.list_jobs() == []
    assert restarted.list_trigger_history(schedule.id) == []
    assert restarted.get_schedule(schedule.id).next_run_at == due.isoformat()
    assert Scheduler(restarted).tick(due) == 1
    assert Scheduler(JobStore(path)).tick(due) == 0
    history = restarted.list_trigger_history(schedule.id)
    jobs = restarted.list_jobs()
    assert len(history) == len(jobs) == 1
    assert history[0].job_id == jobs[0].id


def test_persistence_rejects_second_job_for_same_occurrence(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    due = start + timedelta(minutes=1)
    schedule = Scheduler(store).create(
        kind="once", expression=due.isoformat(), prompt="one job", now=start,
    )
    assert Scheduler(store).tick(due) == 1
    first = store.list_trigger_history(schedule.id)[0]

    with pytest.raises(sqlite3.IntegrityError, match="occurrence already has a job"):
        with store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            second_job = store._insert_scheduled_job(
                connection,
                connection.execute("SELECT * FROM schedules WHERE id=?", (schedule.id,)).fetchone(),
            )
            connection.execute(
                """INSERT INTO trigger_history
                   (schedule_id, scheduled_for, idempotency_key, attempt,
                    status, job_id, detail, created_at)
                   VALUES (?, ?, ?, 2, 'created', ?, '', ?)""",
                (schedule.id, first.scheduled_for, "different-job", second_job, due.isoformat()),
            )

    reopened = JobStore(store.path)
    assert [job.id for job in reopened.list_jobs()] == [first.job_id]
    assert reopened.list_trigger_history(schedule.id) == [first]


def test_schema_upgrade_adds_occurrence_guard_to_existing_history(tmp_path):
    path = tmp_path / "jobs.db"
    store = JobStore(path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    due = start + timedelta(minutes=1)
    schedule = Scheduler(store).create(
        kind="once", expression=due.isoformat(), prompt="existing run", now=start,
    )
    assert Scheduler(store).tick(due) == 1
    original = store.list_trigger_history(schedule.id)[0]
    with store._connect() as connection:
        connection.execute("DROP TRIGGER schedule_occurrence_job_identity")
        connection.execute("DROP TRIGGER schedule_occurrence_job_identity_update")
        connection.execute("DELETE FROM schema_migrations WHERE version=17")
        connection.execute("PRAGMA user_version=16")

    upgraded = JobStore(path)
    assert upgraded.list_trigger_history(schedule.id) == [original]
    assert [job.id for job in upgraded.list_jobs()] == [original.job_id]
    with upgraded._connect() as connection:
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'schedule_occurrence_job_identity%'"
        )}
        assert names == {
            "schedule_occurrence_job_identity",
            "schedule_occurrence_job_identity_update",
        }


def test_worker_pickup_crash_rolls_back_claim_and_attempt(tmp_path):
    path = tmp_path / "jobs.db"
    store = JobStore(path)
    job = store.create_job("inspect")

    _run_crashing_process(path, "claim", "worker_claim")

    restarted = JobStore(path)
    assert restarted.get_job(job.id).status == JobStatus.QUEUED
    assert restarted.get_job(job.id).attempt_count == 0
    assert restarted.list_job_attempts(job.id) == []
    claimed = restarted.claim_next_job()
    assert claimed.id == job.id
    assert claimed.attempt_count == 1
    assert len(restarted.list_job_attempts(job.id)) == 1


def test_checkpoint_recovery_crash_rolls_back_then_resumes_safely(tmp_path):
    path = tmp_path / "jobs.db"
    target = tmp_path / "checkpoint.txt"
    target.write_text("durable change\n", encoding="utf-8")
    store = JobStore(path)
    job = store.create_job("finish", workspace=str(tmp_path), allow_write=True)
    first = store.claim_next_job()
    store.create_plan(job.id, ["finish work"])
    store.update_step(job.id, 1, StepStatus.IN_PROGRESS)
    store.add_change_event(job.id, {
        "path": target.name,
        "diff": "saved change",
        "after_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    })

    _run_crashing_process(path, "recover", "checkpoint_recovery")

    restarted = JobStore(path)
    assert restarted.get_job(job.id).status == JobStatus.RUNNING
    assert restarted.list_steps(job.id)[0].status == StepStatus.IN_PROGRESS
    assert restarted.list_job_attempts(job.id)[0].status == JobStatus.RUNNING
    assert restarted.recover_interrupted_jobs() == 1
    assert restarted.recover_interrupted_jobs() == 0
    assert restarted.get_job(job.id).status == JobStatus.INTERRUPTED
    assert restarted.list_steps(job.id)[0].status == StepStatus.PENDING
    assert restarted.list_job_attempts(job.id)[0].status == JobStatus.INTERRUPTED
    assert restarted.list_job_attempts(job.id)[0].id == first.attempt_id
    assert JobService(restarted).resume(job.id).status == JobStatus.QUEUED
    resumed = restarted.claim_next_job()
    assert resumed.id == job.id
    assert resumed.attempt_id != first.attempt_id
    assert [attempt.ordinal for attempt in restarted.list_job_attempts(job.id)] == [1, 2]


def test_automation_run_rolls_back_job_if_parameter_pin_fails(tmp_path):
    path = tmp_path / "jobs.db"
    store = JobStore(path)
    version = store.create_automation("report", "Report {{day}}", tmp_path,
                                      {"day": {"type": "integer", "required": True}})
    with store._connect() as connection:
        connection.execute("""CREATE TRIGGER reject_parameters BEFORE INSERT ON automation_run_parameters
            BEGIN SELECT RAISE(ABORT, 'parameter pin failed'); END""")

    with pytest.raises(sqlite3.IntegrityError, match="parameter pin failed"):
        store.create_automation_job("report", {"day": 4})
    restarted = JobStore(path)
    assert restarted.list_jobs() == []
    with restarted._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM automation_run_parameters").fetchone()[0] == 0
        connection.execute("DROP TRIGGER reject_parameters")
    job, pinned = restarted.create_automation_job("report", {"day": 4})
    assert pinned.automation_id == version.id
    assert restarted.get_automation_run_parameters(job.id) == {"day": 4}


def test_parent_cancel_and_child_cancel_commit_together(tmp_path):
    path = tmp_path / "jobs.db"
    store = JobStore(path)
    parent = store.create_job("parent")
    child = store.create_job("child")
    with store._connect() as connection:
        connection.execute("UPDATE jobs SET parent_id=? WHERE id=?", (parent.id, child.id))
        connection.execute("""CREATE TRIGGER reject_child_cancel BEFORE UPDATE OF status ON jobs
            WHEN NEW.parent_id IS NOT NULL AND NEW.status = 'cancelled'
            BEGIN SELECT RAISE(ABORT, 'child cancel failed'); END""")

    with pytest.raises(sqlite3.IntegrityError, match="child cancel failed"):
        store.cancel_job(parent.id)
    restarted = JobStore(path)
    assert restarted.get_job(parent.id).status == JobStatus.QUEUED
    assert restarted.get_job(child.id).status == JobStatus.QUEUED
    with restarted._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM notifications WHERE job_id IN (?, ?)",
            (parent.id, child.id),
        ).fetchone()[0] == 0
        connection.execute("DROP TRIGGER reject_child_cancel")

    assert restarted.cancel_job(parent.id)
    assert restarted.get_job(parent.id).status == JobStatus.CANCELLED
    assert restarted.get_job(child.id).status == JobStatus.CANCELLED
    assert not restarted.cancel_job(parent.id)
