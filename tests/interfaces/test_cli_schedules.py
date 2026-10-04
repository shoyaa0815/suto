"""Public schedule commands are backed by durable scheduler state."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from interfaces.cli.commands import CommandContext, handle_command
from workflows.models import JobStatus, MissedRunPolicy, ScheduleKind
from workflows.runtime.scheduler import Scheduler
from workflows.storage.store import JobStore


class Worker:
    def __init__(self):
        self.wakes = 0

    def wake(self):
        self.wakes += 1


def context(store, worker=None):
    return CommandContext(
        store, SimpleNamespace(timezone="Asia/Bangkok"), "conversation", "agent",
        worker=worker,
    )


def test_create_once_uses_profile_timezone_and_persists_options(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    worker = Worker()
    future = (datetime.now(UTC) + timedelta(days=1)).astimezone(
        ZoneInfo("Asia/Bangkok")
    ).replace(microsecond=0)
    local = future.replace(tzinfo=None).isoformat()
    command = (f'/schedule create --at {local} --workspace "{tmp_path}" '
               '--allow-write --missed-run skip --retry 2 --retry-delay 5 "inspect files"')

    assert handle_command(context(store, worker), command).handled
    schedule = JobStore(store.path).list_schedules()[0]
    assert schedule.kind == ScheduleKind.ONCE
    assert schedule.timezone == "Asia/Bangkok"
    assert schedule.next_run_at == future.astimezone(UTC).isoformat()
    assert schedule.workspace == str(tmp_path.resolve())
    assert schedule.prompt == "inspect files"
    assert schedule.allow_write and not schedule.allow_command
    assert schedule.missed_run_policy == MissedRunPolicy.SKIP
    assert (schedule.retry_limit, schedule.retry_delay_seconds) == (2, 5)
    assert worker.wakes == 1
    assert f"Schedule created: {schedule.id}" in capsys.readouterr().out


def test_create_interval_cron_and_offset_once(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    ctx = context(store)
    handle_command(ctx, "/schedule create --every 60 check")
    handle_command(ctx, '/schedule create --cron "0 8 * * *" --timezone UTC check')
    handle_command(ctx, "/schedule create --at 2030-01-01T08:00+07:00 check")
    schedules = {item.kind: item for item in JobStore(store.path).list_schedules()}
    assert set(schedules) == {ScheduleKind.ONCE, ScheduleKind.INTERVAL, ScheduleKind.CRON}
    assert schedules[ScheduleKind.ONCE].next_run_at == "2030-01-01T01:00:00+00:00"
    assert schedules[ScheduleKind.CRON].timezone == "UTC"
    assert schedules[ScheduleKind.INTERVAL].expression == "60"


def test_list_show_pause_resume_and_history_use_persisted_state(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    worker = Worker()
    handle_command(context(store, worker), "/schedule create --every 60 inspect")
    schedule = store.list_schedules()[0]
    due = datetime.fromisoformat(schedule.next_run_at)
    capsys.readouterr()

    for action in ("list", f"show {schedule.id}", f"pause {schedule.id}"):
        handle_command(context(store, worker), f"/schedule {action}")
    assert not JobStore(store.path).get_schedule(schedule.id).enabled
    assert Scheduler(store).tick(due) == 0
    assert store.list_jobs() == []
    assert "State: paused" in capsys.readouterr().out

    handle_command(context(store, worker), f"/schedule resume {schedule.id}")
    assert worker.wakes == 2
    assert Scheduler(store).tick(due) == 1
    job = store.list_jobs()[0]
    handle_command(context(store, worker), f"/schedule pause {schedule.id}")
    assert store.get_job(job.id).status == JobStatus.QUEUED
    handle_command(context(store, worker), f"/schedule history {schedule.id}")
    output = capsys.readouterr().out
    assert "State: active" in output
    assert f"job={job.id}" in output
    assert "attempt=1" in output
    assert JobStore(store.path).list_trigger_history(schedule.id)[0].job_id == job.id


def test_invalid_schedule_commands_do_not_mutate_state(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    ctx = context(store)
    for command in (
        "/schedule create", "/schedule create --at 2030-01-01T00:00 --every 60 task",
        "/schedule create --every 0 task", "/schedule create --cron bad task",
        "/schedule create --at 2030-01-01T00:00 --timezone Unknown/Zone task",
        "/schedule create --every 60 --workspace missing task",
        "/schedule create --every 60 --retry 11 task",
        "/schedule create --every 60 --missed-run replay task",
        "/schedule show missing", "/schedule pause missing", "/schedule history missing",
    ):
        assert handle_command(ctx, command).handled
    assert store.list_schedules() == []
    assert "Cannot" in capsys.readouterr().out


def test_schedule_rejects_detectable_secret_before_persisting(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    handle_command(context(store), "/schedule create --every 60 inspect api_key=example-secret-value")
    assert JobStore(store.path).list_schedules() == []
    output = capsys.readouterr().out
    assert "detectable secret is not allowed in schedule prompt" in output
    assert "example-secret-value" not in output


def test_once_schedule_can_resume_pending_retry_after_pause(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    ctx = context(store)
    due = datetime.now(UTC) + timedelta(seconds=30)
    handle_command(
        ctx, f"/schedule create --at {due.isoformat()} --retry 1 --retry-delay 1 task",
    )
    schedule = store.list_schedules()[0]
    assert Scheduler(store).tick(due) == 1
    job = store.claim_next_job()
    assert store.fail_job(job.id, "temporary failure")
    assert store.get_schedule(schedule.id).next_run_at is None

    handle_command(ctx, f"/schedule pause {schedule.id}")
    assert "State: paused" in capsys.readouterr().out
    assert Scheduler(store).tick(datetime.now(UTC) + timedelta(seconds=2)) == 0
    handle_command(ctx, f"/schedule resume {schedule.id}")
    assert "State: exhausted" in capsys.readouterr().out
    assert Scheduler(store).tick(datetime.now(UTC) + timedelta(seconds=2)) == 1
    assert {event.job_id for event in store.list_trigger_history(schedule.id)} == {job.id}
