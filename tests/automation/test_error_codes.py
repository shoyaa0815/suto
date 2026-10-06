import asyncio
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai.providers.base import ProviderTransientError
from application.automation import AutomationService, JobService
from capabilities.developer.command import build_command_tools
from interfaces.cli.commands import CommandContext, handle_command
from llm.types import ModelResponse, ToolCall
from tests.support.ai_helpers import FakeClientSession
from workflows.errors import ErrorCode, error_code_for_exception, safe_error_message
from workflows.models import JobStatus
from workflows.runtime.context import COMMAND_TOOLS, ExecutionContext, ExecutionLimitExceeded
from workflows.runtime.scheduler import Scheduler
from workflows.runtime.worker import AutomationWorker
from workflows.storage.migrations import SCHEMA_VERSION
from workflows.storage.store import JobStore
from workflows.runtime.runner import JobRunner


def missing_version(store, workspace):
    schedule = Scheduler(store).create_automation(
        automation_name="sample", parameters={}, kind="once",
        expression="2030-01-01T00:00:00+00:00", timezone="UTC",
        now=datetime(2029, 1, 1, tzinfo=UTC),
    )
    store.upgrade_automation_schedule(schedule.id, 99)


@pytest.mark.parametrize("operation, code", [
    (lambda s, p: JobService(s).submit(""), ErrorCode.INVALID_INPUT),
    (lambda s, p: s.create_job("task", options={"unknown": True}), ErrorCode.INVALID_INPUT),
    (lambda s, p: JobService(s).submit("task", workspace=p / "missing"), ErrorCode.WORKSPACE_INVALID),
    (lambda s, p: AutomationService(s).run("missing"), ErrorCode.AUTOMATION_NOT_FOUND),
    (missing_version, ErrorCode.AUTOMATION_VERSION_NOT_FOUND),
    (lambda s, p: AutomationService(s).run("sample", {"count": "wrong"}), ErrorCode.INVALID_PARAMETER),
    (lambda s, p: s.create_automation("broken", "task", p, skill_names=["missing"]), ErrorCode.SKILL_NOT_FOUND),
])
def test_admission_failures_keep_value_error_contract_and_specific_codes(tmp_path, operation, code):
    store = JobStore(tmp_path / "jobs.db")
    store.create_automation("sample", "Task {{count}}", tmp_path,
                            {"count": {"type": "integer", "default": 1}})
    with pytest.raises(ValueError) as caught:
        operation(store, tmp_path)
    assert error_code_for_exception(caught.value) == code
    assert store.list_jobs() == []


@pytest.mark.parametrize("setting", ["max_queued_jobs", "submissions_per_minute"])
def test_admission_quota_preserves_sqlite_exception_and_does_not_create_job(tmp_path, setting):
    store = JobStore(tmp_path / "jobs.db")
    store.configure(**{setting: 1})
    store.create_job("first")
    with pytest.raises(sqlite3.IntegrityError) as caught:
        store.create_job("second")
    assert error_code_for_exception(caught.value) == ErrorCode.QUOTA_EXCEEDED
    assert len(JobStore(store.path).list_jobs()) == 1


async def test_worker_unavailable_preserves_runtime_error_contract(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs.db")
    worker = AutomationWorker(store, JobRunner(store))
    monkeypatch.setattr(worker._lock, "acquire", lambda: False)
    with pytest.raises(RuntimeError, match="another worker") as caught:
        await worker.start()
    assert error_code_for_exception(caught.value) == ErrorCode.WORKER_UNAVAILABLE


@pytest.mark.parametrize("name, code", [
    ("run_workspace_command", ErrorCode.COMMAND_DENIED),
    ("read_workspace_file", ErrorCode.WORKSPACE_PERMISSION_DENIED),
])
def test_context_permission_errors_retain_their_original_type(tmp_path, name, code):
    context = ExecutionContext("job", tmp_path, allowed_tools=frozenset())
    with pytest.raises(PermissionError, match="not allowed") as caught:
        context.require_tool(name)
    assert error_code_for_exception(caught.value) == code


def test_explicit_approval_denial_keeps_permission_error(tmp_path):
    context = ExecutionContext("job", tmp_path, approval_callback=lambda *args: False)
    with pytest.raises(PermissionError, match="approval denied") as caught:
        context.require_approval("write", {}, "write file", "preview")
    assert error_code_for_exception(caught.value) == ErrorCode.APPROVAL_DENIED


@pytest.mark.parametrize("code", [None, "UNKNOWN_CODE", 123, []])
def test_unknown_exception_falls_back_without_exposing_its_message(code):
    error = ValueError("private provider detail that must stay private")
    error.error_code = code
    assert error_code_for_exception(error) == ErrorCode.INTERNAL_ERROR
    assert safe_error_message(error) == "The job could not be completed."


def test_runner_persists_internal_fallback_without_exception_secrets(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("do work", workspace=str(tmp_path))
    claimed = store.claim_next_job()

    async def explode(*args, **kwargs):
        raise RuntimeError("api_key=provider-secret")

    asyncio.run(JobRunner(store, execute=explode).run(claimed))
    result = JobStore(store.path).get_job(job.id)
    assert result.status == JobStatus.FAILED
    assert result.error_code == ErrorCode.INTERNAL_ERROR
    assert result.error == "The job could not be completed."
    assert "provider-secret" not in result.error


@pytest.mark.parametrize("failure, expected, allow_command", [
    ("provider", ErrorCode.PROVIDER_ERROR, False),
    ("retry", ErrorCode.RETRY_EXHAUSTED, False),
    ("command_policy", ErrorCode.COMMAND_DENIED, True),
    ("command_unavailable", ErrorCode.COMMAND_DENIED, False),
    ("sandbox", ErrorCode.SANDBOX_VIOLATION, False),
    ("workspace_permission", ErrorCode.WORKSPACE_PERMISSION_DENIED, False),
    ("invalid_arguments", ErrorCode.INVALID_INPUT, False),
    ("internal_tool", ErrorCode.INTERNAL_ERROR, False),
])
async def test_model_and_tool_failures_reach_durable_results_with_safe_messages(
    tmp_path, monkeypatch, failure, expected, allow_command,
):
    store = JobStore(tmp_path / "jobs.db")
    target = tmp_path / "safe.txt"
    target.write_text("safe content")
    job = store.create_job("inspect workspace", workspace=str(tmp_path), allow_command=allow_command)
    claimed = store.claim_next_job()
    private = "private-provider-detail-123"
    original_read = Path.read_bytes

    def read(path):
        if path == target:
            if failure == "workspace_permission":
                raise PermissionError(private)
            if failure == "internal_tool":
                raise ValueError(private)
        return original_read(path)

    class Model:
        calls = 0

        async def generate(self, request):
            self.calls += 1
            if failure == "provider":
                raise RuntimeError(private)
            if failure == "retry":
                raise ProviderTransientError(private)
            if failure.startswith("command"):
                call = ToolCall("run_workspace_command", {"command": ["bash"]})
            else:
                path = "../outside" if failure == "sandbox" else 123 if failure == "invalid_arguments" else "safe.txt"
                call = ToolCall("read_workspace_file", {"path": path})
            return ModelResponse(None, tool_calls=[call])

    model = Model()
    monkeypatch.setattr("ai.execution.loop.build_model_router", lambda session: model)
    monkeypatch.setattr("ai.executor.aiohttp.ClientSession", FakeClientSession)
    monkeypatch.setattr(Path, "read_bytes", read)
    await JobRunner(store, retry_delays=(0, 0)).run(claimed)

    reopened = JobStore(store.path)
    result = JobService(reopened).result(job.id)
    assert result.error_code == expected
    assert result.finished_at is not None
    assert reopened.list_job_attempts(job.id)[0].error_code == expected
    public = str(asdict(result)) + str(reopened.logs(job.id)) + str(reopened.list_tool_events(job.id))
    assert private not in public
    if failure == "retry":
        assert model.calls == 3
        assert result.retry_count == 2
        assert reopened.notifications()[0]["kind"] == "retry_exhausted"


@pytest.mark.parametrize("command, code", [
    (["bash"], ErrorCode.COMMAND_DENIED),
    (["python", "-m", "pytest", "../outside"], ErrorCode.SANDBOX_VIOLATION),
])
async def test_command_validation_retains_permission_error_and_containment(tmp_path, command, code):
    context = ExecutionContext("job", tmp_path, allowed_tools=COMMAND_TOOLS,
                               approval_callback=lambda *args: True)
    command_tool = build_command_tools(context)["run_workspace_command"]
    with pytest.raises(PermissionError) as caught:
        await command_tool(command)
    assert error_code_for_exception(caught.value) == code


def test_execution_limit_is_quota_and_unknown_value_errors_stay_internal():
    assert error_code_for_exception(ExecutionLimitExceeded("private detail")) == ErrorCode.QUOTA_EXCEEDED
    assert error_code_for_exception(ValueError("provider returned quota exceeded")) == ErrorCode.INTERNAL_ERROR


def test_error_metadata_persists_across_store_restart_and_clears_on_resume(tmp_path):
    path = tmp_path / "jobs.db"
    store = JobStore(path)
    job = store.create_job("do work")
    store.claim_next_job()
    assert store.fail_job(
        job.id,
        "The provider could not complete the request.",
        error_code=ErrorCode.PROVIDER_ERROR,
    )

    reopened = JobStore(path)
    failed = reopened.get_job(job.id)
    assert failed.status == JobStatus.FAILED
    assert failed.error_code == "PROVIDER_ERROR"
    assert failed.error == "The provider could not complete the request."


def test_cancellation_and_interruption_store_codes(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    queued = store.create_job("cancel")
    assert store.cancel_job(queued.id)
    assert store.get_job(queued.id).error_code == ErrorCode.CANCELLED

    running = store.create_job("interrupt")
    store.claim_next_job()
    assert store.interrupt_job(running.id, "worker stopped; safe resume is available")
    assert store.get_job(running.id).error_code == ErrorCode.INTERRUPTED
    assert store.resume_job(running.id)
    assert store.get_job(running.id).error_code is None
    # Cancelling before the next claim must not rewrite the earlier attempt.
    assert store.cancel_job(running.id)
    assert store.list_job_attempts(running.id)[0].error_code == ErrorCode.INTERRUPTED
    assert not store.fail_job(running.id, "stale failure", error_code=ErrorCode.PROVIDER_ERROR)
    assert store.get_job(running.id).error_code == ErrorCode.CANCELLED


def test_approval_denial_and_expiry_keep_distinct_codes(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("approval")
    store.claim_next_job()
    _, approval = store.request_or_consume_approval(
        job.id, "write", {"path": "safe.txt"}, "write a file", "preview"
    )
    assert store.decide_approval(job.id, False, approval_id=approval.id)[0]
    assert store.get_job(job.id).error_code == ErrorCode.APPROVAL_DENIED

    expired = store.create_job("expired approval")
    store.claim_next_job()
    _, approval = store.request_or_consume_approval(
        expired.id, "write", {"path": "safe.txt"}, "write a file", "preview"
    )
    with store._connect() as connection:
        connection.execute(
            "UPDATE approval_requests SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (approval.id,),
        )
    decided, message = store.decide_approval(expired.id, True, approval_id=approval.id)
    assert not decided and "expired" in message
    assert store.get_job(expired.id).error_code == ErrorCode.APPROVAL_EXPIRED
    assert store.list_job_attempts(expired.id)[0].error_code == ErrorCode.APPROVAL_EXPIRED
    assert store.claim_next_job().id == expired.id
    assert store.get_job(expired.id).error_code is None


@pytest.mark.parametrize("retained", ["none", "job", "all"])
def test_version_22_migrates_job_error_code_without_inventing_legacy_values(tmp_path, retained):
    path = tmp_path / "legacy.db"
    store = JobStore(path)
    job = store.create_job("legacy result")
    store.claim_next_job()
    store.fail_job(job.id, "legacy private internal detail")
    before = store.get_job(job.id)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER job_attempt_error_metadata")
        if retained == "none":
            connection.execute("ALTER TABLE jobs DROP COLUMN error_code")
        if retained != "all":
            connection.execute("ALTER TABLE job_attempts DROP COLUMN error_code")
            connection.execute("ALTER TABLE job_attempts DROP COLUMN safe_error_message")
        connection.execute("DELETE FROM schema_migrations WHERE version=?", (SCHEMA_VERSION,))
        connection.execute("PRAGMA user_version=22")

    migrated = JobStore(path)
    legacy = migrated.get_job(job.id)
    assert legacy.error_code == (None if retained == "none" else ErrorCode.INTERNAL_ERROR)
    assert (legacy.prompt, legacy.error, legacy.finished_at, legacy.attempt_id) == (
        before.prompt, before.error, before.finished_at, before.attempt_id,
    )
    assert "legacy private internal detail" not in str(asdict(JobService(migrated).result(job.id)))
    with migrated._connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    backups = list((tmp_path / "backups").glob("*.v22.*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT error FROM jobs WHERE id=?", (job.id,)).fetchone()[0] == before.error


def test_restart_recovery_preserves_attempt_error_after_successful_resume(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("restart later", workspace=str(tmp_path))
    first = store.claim_next_job()
    restarted = JobStore(store.path)
    assert restarted.recover_interrupted_jobs() == 1
    assert restarted.get_job(job.id).error_code == ErrorCode.INTERRUPTED
    assert restarted.resume_job(job.id)
    second = restarted.claim_next_job()
    assert second.attempt_id != first.attempt_id and second.error_code is None
    assert restarted.complete_job(job.id, "result summary", 0, 0)
    result = JobService(JobStore(store.path)).result(job.id)
    assert result.status == JobStatus.COMPLETED
    assert result.error_code is None and result.safe_error_message is None
    assert result.result_summary == "result summary"
    assert restarted.list_job_attempts(job.id)[0].error_code == ErrorCode.INTERRUPTED


def test_error_metadata_transition_is_atomic_with_attempt_and_notification(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("atomic metadata")
    store.claim_next_job()
    with store._connect() as connection:
        connection.execute("""CREATE TRIGGER reject_metadata BEFORE UPDATE OF error_code ON job_attempts
                           BEGIN SELECT RAISE(ABORT, 'metadata update failed'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="metadata update failed"):
        store.fail_job(job.id, "safe failure", error_code=ErrorCode.PROVIDER_ERROR)
    assert store.get_job(job.id).status == JobStatus.RUNNING
    assert store.get_job(job.id).error_code is None
    assert store.list_job_attempts(job.id)[0].error_code is None
    assert store.notifications() == []


def test_attempt_message_is_safe_in_committed_storage(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("inspect")
    claimed = store.claim_next_job()
    store.fail_job(job.id, "private diagnostic detail", error_code=ErrorCode.PROVIDER_ERROR)
    with store._connect() as connection:
        row = connection.execute(
            "SELECT error_code, safe_error_message FROM job_attempts WHERE id=?", (claimed.attempt_id,),
        ).fetchone()
        assert tuple(row) == ("PROVIDER_ERROR", "The provider could not complete the request.")


def test_version_23_migration_failure_rolls_back_and_can_restart(tmp_path, monkeypatch):
    from workflows.storage import migrations

    path = tmp_path / "legacy.db"
    store = JobStore(path)
    job = store.create_job("preserve legacy data")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER job_attempt_error_metadata")
        connection.execute("ALTER TABLE jobs DROP COLUMN error_code")
        connection.execute("ALTER TABLE job_attempts DROP COLUMN error_code")
        connection.execute("ALTER TABLE job_attempts DROP COLUMN safe_error_message")
        connection.execute("DELETE FROM schema_migrations WHERE version=23")
        connection.execute("PRAGMA user_version=22")

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "STRUCTURED_JOB_ERRORS",
                      migrations.STRUCTURED_JOB_ERRORS + "INSERT INTO nonexistent_table VALUES (1);")
        with pytest.raises(sqlite3.OperationalError, match="nonexistent_table"):
            JobStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 22
        assert connection.execute("SELECT 1 FROM schema_migrations WHERE version=23").fetchone() is None
        assert "error_code" not in {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        assert "error_code" not in {row[1] for row in connection.execute("PRAGMA table_info(job_attempts)")}
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened = JobStore(path)
    assert reopened.get_job(job.id).prompt == "preserve legacy data"
    assert reopened.get_job(job.id).error_code is None


def test_structured_scheduled_result_exposes_all_fields_and_final_retry_code(tmp_path, capsys):
    store = JobStore(tmp_path / "jobs.db")
    store.create_automation("sample", "inspect", tmp_path)
    start = datetime(2029, 1, 1, tzinfo=UTC)
    due = datetime(2030, 1, 1, tzinfo=UTC)
    schedule = Scheduler(store).create_automation(
        automation_name="sample", parameters={}, kind="once", expression=due.isoformat(),
        timezone="UTC", now=start, retry_limit=1, retry_delay_seconds=1,
    )
    assert Scheduler(store).tick(due) == 1
    job = store.claim_next_job()
    assert store.fail_job(job.id, "safe provider failure", error_code=ErrorCode.PROVIDER_ERROR)
    trigger_id = store.get_job_trigger_id(job.id)
    retried = store.retry_trigger(trigger_id, now=due.isoformat())
    assert retried is not None
    assert store.get_job(job.id).error_code is None
    store.claim_next_job()
    assert store.fail_job(job.id, "safe provider failure", error_code=ErrorCode.PROVIDER_ERROR)
    result = JobService(JobStore(store.path)).result(job.id)
    assert set(asdict(result)) == {
        "job_id", "attempt_id", "status", "result_summary", "error_code", "safe_error_message",
        "created_at", "started_at", "finished_at", "automation_version_id", "schedule_id",
        "trigger_id", "retry_count",
    }
    assert result.automation_version_id == schedule.automation.automation_version_id
    assert result.schedule_id == schedule.id and result.trigger_id == retried.id
    assert result.retry_count == 1 and result.error_code == ErrorCode.RETRY_EXHAUSTED
    assert [a.error_code for a in store.list_job_attempts(job.id)] == [ErrorCode.PROVIDER_ERROR, ErrorCode.RETRY_EXHAUSTED]
    handle_command(CommandContext(store, None, "conversation", "agent"), f"/status {job.id}")
    output = capsys.readouterr().out
    assert "Error code: RETRY_EXHAUSTED" in output
    assert "Safe error: The job exhausted its retry allowance." in output
    assert f"Schedule: {schedule.id}" in output
    assert "Retry count: 1" in output
