import sqlite3

import pytest

from application.automation import JobService
from workflows.models import JobStatus
from workflows.storage.migrations import SCHEMA_VERSION
from workflows.storage.store import JobStore


def test_attempt_identity_follows_existing_job_lifecycle(tmp_path):
    path = tmp_path / "jobs.db"
    store = JobStore(path)
    job = store.create_job("inspect")
    assert job.attempt_id is None
    assert store.list_job_attempts(job.id) == []

    first = store.claim_next_job()
    assert first.id == job.id
    assert first.attempt_id
    assert first.attempt_count == 1
    assert store.list_job_attempts(job.id)[0].status == JobStatus.RUNNING

    assert store.interrupt_job(job.id, "restart")
    interrupted = store.list_job_attempts(job.id)[0]
    assert interrupted.id == first.attempt_id
    assert interrupted.status == JobStatus.INTERRUPTED
    assert interrupted.finished_at is not None

    reopened = JobStore(path)
    assert reopened.resume_job(job.id)
    assert reopened.get_job(job.id).attempt_id == first.attempt_id
    second = reopened.claim_next_job()
    assert second.id == first.id
    assert second.attempt_id != first.attempt_id
    assert second.attempt_count == 2
    assert reopened.complete_job(job.id, "done", 0, 0)

    attempts = JobStore(path).list_job_attempts(job.id)
    assert [(a.id, a.ordinal, a.status) for a in attempts] == [
        (first.attempt_id, 1, JobStatus.INTERRUPTED),
        (second.attempt_id, 2, JobStatus.COMPLETED),
    ]
    assert all(a.finished_at for a in attempts)
    assert JobService(reopened).list_recent()[0].attempt_id == second.attempt_id


def test_attempt_records_cancellation_and_approval_transition(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    queued = store.create_job("cancel before running")
    assert store.cancel_job(queued.id)
    assert store.list_job_attempts(queued.id) == []

    job = store.create_job("requires approval")
    claimed = store.claim_next_job()
    assert claimed.id == job.id
    with store._connect() as connection:
        connection.execute(
            "UPDATE jobs SET status=? WHERE id=?",
            (JobStatus.WAITING_APPROVAL, job.id),
        )
    waiting = store.list_job_attempts(job.id)[0]
    assert waiting.status == JobStatus.WAITING_APPROVAL
    assert waiting.finished_at is not None
    assert store.cancel_job(job.id)
    assert store.list_job_attempts(job.id)[0].status == JobStatus.CANCELLED


def test_cancelling_resumed_job_before_claim_preserves_previous_attempt(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("resume and cancel")
    first = store.claim_next_job()
    assert store.interrupt_job(job.id, "restart")
    assert store.resume_job(job.id)
    assert store.cancel_job(job.id)
    assert store.get_job(job.id).status == JobStatus.CANCELLED
    attempts = store.list_job_attempts(job.id)
    assert len(attempts) == 1
    assert attempts[0].id == first.attempt_id
    assert attempts[0].status == JobStatus.INTERRUPTED


def test_claim_and_attempt_insert_roll_back_together(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("atomic claim")
    with store._connect() as connection:
        connection.execute(
            """CREATE TRIGGER reject_attempt BEFORE INSERT ON job_attempts
               BEGIN SELECT RAISE(ABORT, 'attempt insert failed'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="attempt insert failed"):
        store.claim_next_job()
    unchanged = JobStore(store.path).get_job(job.id)
    assert unchanged.status == JobStatus.QUEUED
    assert unchanged.attempt_count == 0
    assert unchanged.attempt_id is None
    assert store.list_job_attempts(job.id) == []


def test_version_fourteen_migration_preserves_jobs_without_inventing_history(tmp_path):
    path = tmp_path / "legacy.db"
    store = JobStore(path)
    job = store.create_job("resume later")
    store.claim_next_job()
    store.interrupt_job(job.id, "restart")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER job_attempt_status")
        connection.execute("DROP TABLE job_attempts")
        connection.execute("ALTER TABLE jobs DROP COLUMN attempt_id")
        connection.execute("DELETE FROM schema_migrations WHERE version=?", (SCHEMA_VERSION,))
        connection.execute("PRAGMA user_version=14")

    migrated = JobStore(path)
    assert migrated.get_job(job.id).status == JobStatus.INTERRUPTED
    assert migrated.get_job(job.id).attempt_id is None
    assert migrated.list_job_attempts(job.id) == []
    assert migrated.resume_job(job.id)
    claimed = migrated.claim_next_job()
    assert claimed.attempt_count == 2
    assert [(a.ordinal, a.id) for a in migrated.list_job_attempts(job.id)] == [
        (2, claimed.attempt_id)
    ]
    backups = list((tmp_path / "backups").glob("*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
