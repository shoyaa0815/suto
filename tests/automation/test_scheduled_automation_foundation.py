"""Phase 2A storage boundaries; scheduled automation execution is still closed."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from workflows.runtime.scheduler import Scheduler
from workflows.storage import migrations
from workflows.storage.store import JobStore


def _create_versioned_automation(store, workspace):
    store.create_skill("review", "Review the first version.")
    store.create_automation(
        "daily-review", "Review {{topic}} for {{day}}.", workspace,
        {"topic": {"type": "string", "default": "changes"},
         "day": {"type": "integer", "required": True}},
        allow_write=True, skill_names=["review"],
    )


def test_snapshot_persists_exact_version_inputs_and_permissions(tmp_path):
    database = tmp_path / "suto.db"
    store = JobStore(database)
    _create_versioned_automation(store, tmp_path)
    first = store.get_current_automation_version("daily-review")
    first_skill_id = store.list_automation_skill_versions(first.id)[0][1].id
    due = datetime(2026, 1, 1, tzinfo=UTC).isoformat()

    schedule = store.create_automation_schedule(
        automation_name="DAILY-REVIEW", parameters={"day": 4},
        kind="once", expression=due, timezone="UTC", next_run_at=due,
        retry_limit=2, retry_delay_seconds=30,
    )
    store.revise_skill("review", "Review the newer version.")
    store.revise_automation(
        "daily-review", "Summarize {{day}}.", tmp_path,
        {"day": {"type": "integer", "required": True}},
        allow_command=True, skill_names=["review"],
    )

    reopened = JobStore(database).get_schedule(schedule.id)
    assert reopened == schedule
    assert reopened.automation.automation_name == "daily-review"
    assert reopened.automation.automation_version_id == first.id
    assert reopened.automation.automation_version == 1
    assert reopened.automation.parameters == {"day": 4, "topic": "changes"}
    assert reopened.automation.skill_version_ids == (first_skill_id,)
    assert reopened.automation.workspace == str(tmp_path.resolve())
    assert (reopened.automation.allow_write, reopened.automation.allow_command) == (True, False)
    assert reopened.automation.options == {"sandbox": "process"}
    assert (reopened.timezone, reopened.retry_limit, reopened.retry_delay_seconds) == ("UTC", 2, 30)
    assert store.list_schedules() == [reopened]

    # Phase 2A must not create a job even if the old scheduler sees a due row.
    assert Scheduler(store).tick(datetime(2026, 1, 2, tzinfo=UTC)) == 0
    assert store.fire_schedule(schedule.id, due, None) is None
    assert store.list_jobs() == []
    assert store.list_trigger_history(schedule.id) == []
    assert store.get_schedule(schedule.id).next_run_at == due


@pytest.mark.parametrize("parameters", [None, {"day": "4"}, {"day": 4, "extra": 1}])
def test_invalid_snapshot_parameters_leave_no_schedule(tmp_path, parameters):
    store = JobStore(tmp_path / "suto.db")
    _create_versioned_automation(store, tmp_path)
    with pytest.raises(ValueError):
        store.create_automation_schedule(
            automation_name="daily-review", parameters=parameters,
            kind="once", expression="2026-01-01T00:00:00+00:00",
            timezone="UTC", next_run_at="2026-01-01T00:00:00+00:00",
        )
    assert store.list_schedules() == []


def test_snapshot_insert_failure_rolls_back_schedule_row(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    _create_versioned_automation(store, tmp_path)
    with store._connect() as connection:
        connection.execute("""CREATE TRIGGER reject_snapshot BEFORE INSERT
            ON schedule_automation_snapshots BEGIN SELECT RAISE(ABORT, 'test failure'); END""")

    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        store.create_automation_schedule(
            automation_name="daily-review", parameters={"day": 4},
            kind="once", expression="2026-01-01T00:00:00+00:00",
            timezone="UTC", next_run_at="2026-01-01T00:00:00+00:00",
        )
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM schedule_automation_snapshots").fetchone()[0] == 0


def test_snapshot_rejects_secrets_and_preserves_version_reference(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    _create_versioned_automation(store, tmp_path)
    with pytest.raises(ValueError, match="detectable secret"):
        store.create_automation_schedule(
            automation_name="daily-review", parameters={"day": 4, "topic": "sk-abcdefghij"},
            kind="once", expression="2026-01-01T00:00:00+00:00",
            timezone="UTC", next_run_at="2026-01-01T00:00:00+00:00",
        )
    assert store.list_schedules() == []

    schedule = store.create_automation_schedule(
        automation_name="daily-review", parameters={"day": 4},
        kind="once", expression="2026-01-01T00:00:00+00:00",
        timezone="UTC", next_run_at="2026-01-01T00:00:00+00:00",
    )
    with store._connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE schedule_automation_snapshots SET automation_version=2 WHERE schedule_id=?",
                (schedule.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "DELETE FROM automation_versions WHERE id=?",
                (schedule.automation.automation_version_id,),
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.get_schedule(schedule.id) == schedule


def test_v17_migration_preserves_prompt_schedule_and_its_trigger(tmp_path):
    database = tmp_path / "suto.db"
    old_store = JobStore(database)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    legacy = Scheduler(old_store).create(
        kind="interval", expression="60", prompt="original prompt",
        workspace=tmp_path, allow_write=True, now=start,
    )
    with old_store._connect() as connection:
        connection.executescript("""DROP TRIGGER schedule_automation_snapshot_immutable;
            DROP TABLE schedule_automation_snapshots;
            DELETE FROM schema_migrations WHERE version=18;
            PRAGMA user_version=17;""")

    upgraded = JobStore(database)
    migrated = upgraded.get_schedule(legacy.id)
    assert migrated == legacy
    assert migrated.automation is None
    assert upgraded.list_schedules() == [legacy]
    with upgraded._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 18
        assert connection.execute("SELECT COUNT(*) FROM schedule_automation_snapshots").fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert Scheduler(upgraded).tick(start + timedelta(seconds=60)) == 1
    job = upgraded.list_jobs()[0]
    assert (job.prompt, job.workspace, job.allow_write) == (
        "original prompt", str(tmp_path.resolve()), True,
    )


def test_failed_v18_migration_rolls_back_and_can_retry(tmp_path, monkeypatch):
    database = tmp_path / "suto.db"
    old_store = JobStore(database)
    legacy = Scheduler(old_store).create(
        kind="once", expression="2026-01-01T00:00:00+00:00",
        prompt="keep me", workspace=tmp_path,
    )
    with old_store._connect() as connection:
        connection.executescript("""DROP TRIGGER schedule_automation_snapshot_immutable;
            DROP TABLE schedule_automation_snapshots;
            DELETE FROM schema_migrations WHERE version=18;
            PRAGMA user_version=17;""")

    with monkeypatch.context() as patch:
        patch.setattr(
            migrations, "SCHEDULED_AUTOMATION_SNAPSHOTS",
            migrations.SCHEDULED_AUTOMATION_SNAPSHOTS + "\nINVALID SQL;",
        )
        with pytest.raises(sqlite3.OperationalError):
            JobStore(database)
    with old_store._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 17
        assert connection.execute("SELECT 1 FROM schema_migrations WHERE version=18").fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schedule_automation_snapshots'"
        ).fetchone() is None
        assert connection.execute("SELECT prompt FROM schedules WHERE id=?", (legacy.id,)).fetchone()[0] == "keep me"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert JobStore(database).get_schedule(legacy.id).automation is None
