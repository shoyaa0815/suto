"""Public schedule commands are backed by durable scheduler state."""

import json
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


def _saved_review(store, tmp_path):
    store.create_skill("review-skill", "Review changes.")
    store.create_automation(
        "review", "Review {{repo}} at {{depth}} depth.", tmp_path,
        {"repo": {"type": "string", "required": True},
         "depth": {"type": "string", "default": "quick"}},
        allow_write=True, skill_names=["review-skill"],
    )


def test_schedule_automation_cli_pins_inputs_and_inspects_after_update(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    worker = Worker()
    _saved_review(store, tmp_path)
    first = store.get_current_automation_version("review")
    first_skill = store.list_automation_skill_versions(first.id)[0][1].id
    ctx = context(store, worker)

    assert handle_command(ctx, "/schedule automation review --at 2030-01-01T08:00 --retry 2 repo=suto").handled
    schedule = JobStore(store.path).list_schedules()[0]
    assert schedule.next_run_at == "2030-01-01T01:00:00+00:00"
    assert schedule.automation.automation_version_id == first.id
    assert schedule.automation.parameters == {"repo": "suto", "depth": "quick"}
    assert schedule.automation.skill_version_ids == (first_skill,)
    assert schedule.automation.workspace == str(tmp_path.resolve())
    assert (schedule.automation.allow_write, schedule.automation.allow_command) == (True, False)
    assert (schedule.retry_limit, schedule.timezone) == (2, "Asia/Bangkok")
    assert worker.wakes == 1

    update = tmp_path / "updated.json"
    update.write_text(json.dumps({
        "name": "review", "prompt_template": "Inspect {{repo}}.",
        "parameter_schema": {"repo": {"type": "string", "required": True}},
        "workspace": str(tmp_path), "allow_command": True, "skills": ["review-skill"],
    }), encoding="utf-8")
    store.revise_skill("review-skill", "New review steps.")
    assert handle_command(ctx, f"/automation update review {update}").handled
    assert store.get_current_automation_version("review").version == 2
    assert handle_command(ctx, "/schedule list").handled
    assert handle_command(ctx, f"/schedule show {schedule.id}").handled
    output = capsys.readouterr().out
    assert "State: active" in output
    assert "automation=review@v1" in output
    assert "Automation: review" in output and "Version: 1" in output
    assert 'Parameters: {"depth": "quick", "repo": "suto"}' in output
    assert f"Skill version IDs: {first_skill}" in output
    assert JobStore(store.path).get_schedule(schedule.id) == schedule
    assert Scheduler(store).tick(datetime(2030, 1, 2, tzinfo=UTC)) == 1
    assert len(store.list_jobs()) == 1
    assert store.list_jobs()[0].prompt == "Review suto at quick depth."
    assert len(store.list_trigger_history(schedule.id)) == 1


def test_schedule_upgrade_cli_requires_explicit_valid_version_and_parameters(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    _saved_review(store, tmp_path)
    ctx = context(store)
    handle_command(ctx, "/schedule automation review --at 2030-01-01T08:00 repo=suto")
    schedule = store.list_schedules()[0]
    store.revise_automation(
        "review", "Inspect {{repo}} {{count}} times.", tmp_path,
        {"repo": {"type": "string", "required": True},
         "count": {"type": "integer", "required": True}},
        allow_command=True,
    )
    for command in (
        f"/schedule upgrade {schedule.id} --automation-version latest",
        f"/schedule upgrade {schedule.id} --automation-version 9 repo=suto count=2",
        f"/schedule upgrade {schedule.id} --automation-version latest repo=suto count=bad",
    ):
        assert handle_command(ctx, command).handled
        assert store.get_schedule(schedule.id) == schedule
    assert handle_command(
        ctx, f"/schedule upgrade {schedule.id} --automation-version 2 repo=suto count=2",
    ).handled
    upgraded = store.get_schedule(schedule.id)
    assert upgraded.automation.automation_version == 2
    assert upgraded.automation.parameters == {"repo": "suto", "count": 2}
    assert (upgraded.allow_write, upgraded.allow_command) == (False, True)
    assert upgraded.next_run_at == schedule.next_run_at
    assert "Schedule upgraded:" in capsys.readouterr().out


def test_schedule_automation_cli_rejects_invalid_inputs_without_rows(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    _saved_review(store, tmp_path)
    ctx = context(store)
    for command in (
        "/schedule automation missing --every 60 repo=suto",
        "/schedule automation review --every 60",
        "/schedule automation review --every 60 repo=1",
        "/schedule automation review --every 60 repo=suto unknown=1",
        "/schedule automation review --every 60 repo=suto repo=other",
        "/schedule automation review --every 60 repo",
        "/schedule automation review --every 0 repo=suto",
        "/schedule automation review --cron bad repo=suto",
        "/schedule automation review --every 60 --timezone No/Such_Zone repo=suto",
        "/schedule automation review --every 60 --retry 11 repo=suto",
        "/schedule automation review --every 60 --allow-command repo=suto",
        "/schedule automation review --every 60 --workspace . repo=suto",
        "/schedule automation review --every 60 repo=sk-123456789abc",
    ):
        assert handle_command(ctx, command).handled
        assert store.list_schedules() == []
    output = capsys.readouterr().out
    assert "Cannot create automation schedule" in output
    assert "sk-123456789abc" not in output
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM schedule_automation_snapshots").fetchone()[0] == 0


def test_schedule_automation_cli_rolls_back_on_snapshot_failure(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    _saved_review(store, tmp_path)
    with store._connect() as connection:
        connection.execute("""CREATE TRIGGER reject_snapshot BEFORE INSERT
            ON schedule_automation_snapshots BEGIN SELECT RAISE(ABORT, 'test failure'); END""")

    assert handle_command(
        context(store), "/schedule automation review --every 60 repo=suto",
    ).handled
    assert store.list_schedules() == []
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM schedule_automation_snapshots").fetchone()[0] == 0
    assert "Cannot create schedule: storage error." in capsys.readouterr().out


def test_schedule_automation_cli_validates_workspace_and_skill_reference(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store.create_skill("review-skill", "Review changes.")
    store.create_automation(
        "review", "Review files.", workspace, skill_names=["review-skill"],
    )
    workspace.rmdir()
    command = "/schedule automation review --every 60"
    assert handle_command(context(store), command).handled
    assert "workspace is not a directory" in capsys.readouterr().out
    assert store.list_schedules() == []

    workspace.mkdir()
    with store._connect() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "UPDATE automation_version_skills SET skill_version_id = 'missing'",
        )
    assert handle_command(context(store), command).handled
    assert "automation skill reference is invalid" in capsys.readouterr().out
    assert store.list_schedules() == []
