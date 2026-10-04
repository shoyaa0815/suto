import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ai import AIExecutionResult
from workflows.runtime.runner import JobRunner
from workflows.runtime.scheduler import Scheduler
from workflows.storage.store import JobStore


def test_result_inbox_persists_and_acknowledgement_is_idempotent(tmp_path):
    path = tmp_path / "jobs.db"
    store = JobStore(path)
    completed = store.create_job("complete")
    store.claim_next_job()
    assert store.complete_job(completed.id, "done", 0, 0)
    assert not store.complete_job(completed.id, "again", 0, 0)

    cancelled = store.create_job("cancel")
    assert store.cancel_job(cancelled.id)
    assert not store.cancel_job(cancelled.id)

    items = JobStore(path).notifications()
    assert [(item["job_id"], item["kind"]) for item in items] == [
        (cancelled.id, "cancelled"), (completed.id, "completed")
    ]
    assert all(item["read_at"] is None for item in items)

    assert store.acknowledge_notification(items[0]["id"])
    assert not store.acknowledge_notification(items[0]["id"])
    reopened = JobStore(path)
    assert [item["id"] for item in reopened.notifications()] == [items[1]["id"]]
    history = reopened.notifications(unread_only=False)
    assert history[0]["read_at"] is not None
    assert [item["id"] for item in history] == [item["id"] for item in items]


def test_approval_required_is_one_item_per_actual_transition(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("approve")
    store.claim_next_job()
    with store._connect() as db:
        db.execute("UPDATE jobs SET status='waiting_approval' WHERE id=?", (job.id,))
    first = store.notifications()[0]
    assert (first["status"], first["kind"]) == ("waiting_approval", "approval_required")
    assert store.acknowledge_notification(first["id"])
    with store._connect() as db:
        db.execute("UPDATE jobs SET status='waiting_approval' WHERE id=?", (job.id,))
    assert store.notifications() == []
    with store._connect() as db:
        db.execute("UPDATE jobs SET status='queued' WHERE id=?", (job.id,))
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (job.id,))
        db.execute("UPDATE jobs SET status='waiting_approval' WHERE id=?", (job.id,))
    second = store.notifications()[0]
    assert second["id"] != first["id"]
    assert second["kind"] == "approval_required"


@pytest.mark.asyncio
async def test_runner_retry_exhausted_is_durable(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("retry")
    claimed = store.claim_next_job()

    async def transient(*args, **kwargs):
        return AIExecutionResult("timeout", "timed_out", "timeout", 0, 0, 0)

    await JobRunner(store, execute=transient, retry_delays=(0,)).run(claimed)
    assert store.get_job(job.id).status == "failed"
    assert store.get_job(job.id).retry_count == 1
    assert JobStore(store.path).notifications()[0]["kind"] == "retry_exhausted"


def test_scheduled_retry_only_exhausts_on_final_failure(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    start = datetime.now(UTC).replace(microsecond=0)
    scheduler = Scheduler(store)
    schedule = scheduler.create(
        kind="once", expression=(start + timedelta(seconds=30)).isoformat(),
        prompt="retry", retry_limit=1, retry_delay_seconds=1, now=start,
    )
    assert scheduler.tick(start + timedelta(seconds=30)) == 1
    job = store.claim_next_job()
    assert store.fail_job(job.id, "first failure")
    assert store.notifications()[0]["kind"] == "failed"
    assert store.acknowledge_notification(store.notifications()[0]["id"])
    assert scheduler.tick(start + timedelta(seconds=40)) == 1
    assert store.claim_next_job().id == job.id
    assert store.fail_job(job.id, "second failure")
    assert JobStore(store.path).notifications()[0]["kind"] == "retry_exhausted"
    assert store.list_trigger_history(schedule.id)[0].attempt == 2


def test_notification_insert_failure_rolls_back_job_transition(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("rollback")
    store.claim_next_job()
    with store._connect() as db:
        db.execute("""CREATE TRIGGER reject_notification BEFORE INSERT ON notifications
                      BEGIN SELECT RAISE(ABORT, 'notification rejected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="notification rejected"):
        store.fail_job(job.id, "failure")
    assert JobStore(store.path).get_job(job.id).status == "running"
    assert store.notifications(unread_only=False) == []


def test_upgrade_backfills_existing_notifications(tmp_path, monkeypatch):
    import workflows.storage.migrations as migrations

    path = tmp_path / "jobs.db"
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "SCHEMA_VERSION", 18)
        old = JobStore(path)
        job = old.create_job("legacy")
        old.claim_next_job()
        old.fail_job(job.id, "failure")
        legacy_id = old.notifications()[0]["id"]
        assert old.acknowledge_notification(legacy_id)

    upgraded = JobStore(path)
    item = upgraded.notifications(unread_only=False)[0]
    assert (item["id"], item["status"], item["kind"]) == (
        legacy_id, "failed", "failed"
    )
    assert item["read_at"] is not None
    assert upgraded.notifications() == []
