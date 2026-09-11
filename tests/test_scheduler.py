from datetime import UTC, datetime, timedelta

from automation.models import JobStatus, MissedRunPolicy, ScheduleKind, TriggerStatus
from automation.scheduler import CronExpression, Scheduler
from automation.store import JobStore


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
