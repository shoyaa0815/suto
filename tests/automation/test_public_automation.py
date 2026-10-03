"""Public automation definitions and runs pin their exact inputs."""

import json
import sqlite3

import pytest

from application.automation import AutomationService
from interfaces.cli.commands import CommandContext, handle_command
from workflows.storage.store import JobStore


def definition(tmp_path, **overrides):
    data = {
        "name": "review",
        "prompt_template": "Review {{repo}} at {{depth}}.",
        "parameter_schema": {
            "repo": {"type": "string", "required": True},
            "depth": {"type": "string", "default": "quick"},
        },
        "workspace": str(tmp_path),
        "skills": ["review-skill"],
    }
    data.update(overrides)
    path = tmp_path / "definition.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_public_commands_pin_version_parameters_and_history(tmp_path, capsys):
    store = JobStore(tmp_path / "jobs.db")
    store.create_skill("review-skill", "Inspect changes.")
    source = definition(tmp_path)
    context = CommandContext(store, None, "conversation", "agent")

    assert handle_command(context, f"/automation create {source}").handled
    assert handle_command(context, "/automation list").handled
    assert handle_command(context, "/automation show review").handled
    assert handle_command(context, "/automation run review repo=suto").handled
    first = store.list_jobs()[0]
    first_version = store.get_automation_version(first.source_ref)
    assert first_version.version == 1
    assert first.prompt == "Review suto at quick."
    assert store.get_automation_run_parameters(first.id) == {"repo": "suto", "depth": "quick"}
    assert first.workspace == str(tmp_path.resolve())
    assert not first.allow_write and not first.allow_command

    definition(tmp_path, prompt_template="Inspect {{repo}} at {{depth}}.", allow_write=True)
    assert handle_command(context, f"/automation update review {source}").handled
    assert handle_command(context, "/automation run review repo=other depth=full").handled
    second = store.list_jobs()[0]
    assert second.source_ref != first.source_ref
    assert store.get_automation_version(second.source_ref).version == 2
    assert first.prompt == store.get_job(first.id).prompt
    assert store.get_automation_run_parameters(first.id) == {"repo": "suto", "depth": "quick"}
    assert first.allow_write is False and second.allow_write is True
    assert handle_command(context, "/automation history review").handled
    output = capsys.readouterr().out
    assert first.id in output and second.id in output
    assert "Version: 1" in output
    assert "version=1" in output and "version=2" in output


@pytest.mark.parametrize("parameters,match", [
    ({}, "missing required"),
    ({"repo": "suto", "unknown": 1}, "unknown parameters"),
    ({"repo": 1}, "must be string"),
    ({"repo": "sk-123456789abc"}, "detectable secret"),
    ({"repo": {"api_key": "hidden"}}, "must be string"),
])
def test_run_rejects_invalid_parameters_without_job(tmp_path, parameters, match):
    store = JobStore(tmp_path / "jobs.db")
    store.create_skill("review-skill", "Inspect changes.")
    service = AutomationService(store)
    service.create(definition(tmp_path))
    with pytest.raises(ValueError, match=match):
        service.run("review", parameters)
    assert store.list_jobs() == []


def test_definition_rejects_missing_skill_secrets_and_invalid_permissions(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    service = AutomationService(store)
    with pytest.raises(ValueError, match="skill not found"):
        service.create(definition(tmp_path))
    assert store.list_automations() == []
    store.create_skill("review-skill", "Inspect changes.")
    for change, match in [
        ({"description": "Bearer abcdefghijklmnop"}, "detectable secret"),
        ({"parameter_schema": {"repo": {"type": "string", "default": "sk-123456789abc"}}}, "detectable secret"),
        ({"allow_write": "yes"}, "must be boolean"),
        ({"workspace": str(tmp_path / "missing")}, "workspace is not a directory"),
    ]:
        with pytest.raises(ValueError, match=match):
            service.create(definition(tmp_path, **change))
        assert store.list_automations() == []


def test_update_validation_leaves_current_version_and_history_intact(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    store.create_skill("review-skill", "Inspect changes.")
    service = AutomationService(store)
    service.create(definition(tmp_path))
    job, first = service.run("review", {"repo": "suto"})
    source = definition(tmp_path, skills=["missing-skill"])
    with pytest.raises(ValueError, match="skill not found"):
        service.update("review", source)
    assert store.get_automation("review").current_version == 1
    assert store.get_current_automation_version("review") == first
    assert store.list_automation_jobs(first.automation_id) == [job]
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM automation_versions").fetchone()[0] == 1


def test_run_rejects_sensitive_field_and_removed_workspace(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    store.create_automation("secret-check", "Check {{payload}}", tmp_path,
                            {"payload": {"type": "object", "required": True}})
    service = AutomationService(store)
    with pytest.raises(ValueError, match="sensitive value"):
        service.run("secret-check", {"payload": {"api_key": "hidden"}})
    assert store.list_jobs() == []
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store.create_automation("workspace-check", "Check files", workspace)
    workspace.rmdir()
    with pytest.raises(ValueError, match="workspace is not a directory"):
        service.run("workspace-check")
    assert store.list_jobs() == []


def test_run_rolls_back_job_when_snapshot_insert_fails(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    store.create_automation("review", "Review {{repo}}", tmp_path,
                            {"repo": {"type": "string", "required": True}})
    with store._connect() as connection:
        connection.execute("""CREATE TRIGGER reject_snapshot BEFORE INSERT ON automation_run_parameters
                              BEGIN SELECT RAISE(ABORT, 'snapshot rejected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="snapshot rejected"):
        AutomationService(store).run("review", {"repo": "suto"})
    assert store.list_jobs() == []
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM job_stats").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM structured_logs").fetchone()[0] == 0


def test_run_quota_failure_leaves_no_snapshot_or_job(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    store.create_automation("review", "Review files", tmp_path)
    with store._connect() as connection:
        connection.execute("UPDATE runtime_settings SET value=0 WHERE key='max_queued_jobs'")
    with pytest.raises(sqlite3.IntegrityError, match="quota"):
        AutomationService(store).run("review")
    assert store.list_jobs() == []
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM automation_run_parameters").fetchone()[0] == 0
