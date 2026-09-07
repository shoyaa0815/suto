import asyncio
import sqlite3

import ai.client
import ai.executor
import pytest
from ai import AIExecutionResult
from automation.context import (
    ACTION_POLICIES,
    COMMAND_TOOLS,
    WRITE_WORKSPACE_TOOLS,
    ApprovalRequired,
    ExecutionContext,
)
from automation.models import ActionType, ApprovalStatus, JobStatus
from automation.redaction import REDACTED
from automation.runner import JobRunner
from automation.store import JobStore
from automation.worker import AutomationWorker
from tools.command import build_command_tools
from tools.workspace import build_workspace_tools


class _FakeClientSession:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


ACTION = {
    "tool": "apply_workspace_patch",
    "path": "app.py",
    "before_sha256": "before",
    "after_sha256": "after",
}


def test_action_policy_allows_reads_requires_approval_and_denies_destructive(tmp_path):
    context = ExecutionContext("job_test", tmp_path)

    assert ACTION_POLICIES == {
        ActionType.READ: "allow",
        ActionType.WRITE: "require_approval",
        ActionType.COMMAND: "require_approval",
        ActionType.DESTRUCTIVE: "deny",
    }
    context.require_approval("read", {}, "read file", "")
    with pytest.raises(PermissionError, match="not allowed"):
        context.require_approval("destructive", {}, "delete file", "delete app.py")


def _request(store: JobStore, job_id: str, action: dict = ACTION, ttl: int = 600):
    return store.request_or_consume_approval(
        job_id,
        "write",
        action,
        "replace workspace file app.py",
        "--- a/app.py\n+++ b/app.py\n-old\n+new\n",
        ttl_seconds=ttl,
    )


def test_approval_is_persistent_exact_and_single_use(tmp_path):
    database = tmp_path / "suto.db"
    store = JobStore(database)
    job = store.create_job("update app", allow_write=True)
    store.claim_next_job()

    authorized, requested = _request(store, job.id)

    assert authorized is False
    assert requested.status == ApprovalStatus.PENDING
    assert store.get_job(job.id).status == JobStatus.WAITING_APPROVAL
    assert JobStore(database).latest_approval(job.id) == requested

    decided, message = store.decide_approval(job.id, True, actor="cli:test-user")
    assert decided is True
    assert "job queued" in message
    assert store.get_job(job.id).status == JobStatus.QUEUED

    store.claim_next_job()
    authorized, consumed = _request(store, job.id)
    assert authorized is True
    assert consumed.id == requested.id
    assert consumed.status == ApprovalStatus.CONSUMED

    authorized, second = _request(store, job.id)
    assert authorized is False
    assert second.id != requested.id
    assert second.status == ApprovalStatus.PENDING
    assert [event.event_type for event in store.list_approval_events(job.id)] == [
        "requested",
        "approved",
        "consumed",
        "requested",
    ]


def test_changed_action_invalidates_unused_approval(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("update app", allow_write=True)
    store.claim_next_job()
    _, first = _request(store, job.id)
    store.decide_approval(job.id, True)
    store.claim_next_job()

    changed = dict(ACTION, after_sha256="different")
    authorized, second = _request(store, job.id, changed)

    assert authorized is False
    assert second.id != first.id
    approvals = {item.id: item for item in store.list_approvals(job.id)}
    assert approvals[first.id].status == ApprovalStatus.INVALIDATED
    assert approvals[second.id].status == ApprovalStatus.PENDING


def test_reject_blocks_job_and_records_actor(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("update app", allow_write=True)
    store.claim_next_job()
    _request(store, job.id)

    decided, message = store.decide_approval(job.id, False, actor="cli:owner")

    assert decided is True
    assert "job blocked" in message
    blocked = store.get_job(job.id)
    assert blocked.status == JobStatus.BLOCKED
    assert "cli:owner" in blocked.error
    approval = store.latest_approval(job.id)
    assert approval.status == ApprovalStatus.REJECTED
    assert approval.decided_by == "cli:owner"


def test_expired_approval_cannot_be_granted(tmp_path):
    database = tmp_path / "suto.db"
    store = JobStore(database)
    job = store.create_job("update app", allow_write=True)
    store.claim_next_job()
    _, approval = _request(store, job.id)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE approval_requests SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", approval.id),
        )

    decided, message = store.decide_approval(job.id, True)

    assert decided is False
    assert "expired" in message
    assert store.latest_approval(job.id).status == ApprovalStatus.EXPIRED
    assert store.get_job(job.id).status == JobStatus.QUEUED


def test_cancel_invalidates_pending_approval(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("update app", allow_write=True)
    store.claim_next_job()
    _request(store, job.id)

    assert store.cancel_job(job.id)
    assert store.get_job(job.id).status == JobStatus.CANCELLED
    assert store.latest_approval(job.id).status == ApprovalStatus.INVALIDATED


async def test_worker_can_cancel_job_waiting_for_approval(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("update app", allow_write=True)
    store.claim_next_job()
    _request(store, job.id)
    worker = AutomationWorker(store, JobRunner(store))

    assert await worker.cancel(job.id)
    assert store.get_job(job.id).status == JobStatus.CANCELLED


def test_state_changing_tools_fail_closed_without_approval_callback(tmp_path):
    context = ExecutionContext(
        "job_test",
        tmp_path,
        allowed_tools=WRITE_WORKSPACE_TOOLS,
    )
    tool = build_workspace_tools(context)["apply_workspace_patch"]

    with pytest.raises(PermissionError, match="approval is unavailable"):
        tool("created.txt", "hello\n")
    assert not (tmp_path / "created.txt").exists()


async def test_command_is_not_started_before_approval(tmp_path):
    events = []

    def require(*args):
        raise ApprovalRequired("approval_test", "run pytest -q")

    context = ExecutionContext(
        "job_test",
        tmp_path,
        allowed_tools=COMMAND_TOOLS,
        command_event_callback=events.append,
        approval_callback=require,
    )
    tool = build_command_tools(context, events.append)["run_workspace_command"]

    with pytest.raises(ApprovalRequired):
        await tool(["pytest", "-q"])
    assert events == []


def test_persistent_records_redact_secrets(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("use api_key=top-secret-value")
    assert REDACTED in job.prompt
    assert "top-secret-value" not in job.prompt

    store.add_tool_event(
        job.id,
        {
            "tool_name": "example",
            "arguments": {"authorization": "Bearer hidden-token"},
            "status": "failed",
            "error": "password=hunter2",
        },
    )
    tool_event = store.latest_tool_event(job.id)
    assert "hidden-token" not in tool_event.arguments
    assert "hunter2" not in tool_event.error

    store.add_command_event(
        job.id,
        {
            "command": ["pytest", "token=command-secret"],
            "status": "failed",
            "stdout": "OPENAI_API_KEY=sk-testsecret123",
            "stderr": "client_secret=stderr-secret",
        },
    )
    command = store.latest_command_event(job.id)
    combined = command.command + command.stdout + command.stderr
    assert "command-secret" not in combined
    assert "testsecret" not in combined
    assert "stderr-secret" not in combined


async def test_runner_waits_then_consumes_exact_approval(tmp_path):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job(
        "create note",
        workspace=str(tmp_path),
        allow_write=True,
    )
    claimed = store.claim_next_job()

    async def execute(prompt, execution_context, change_event_callback, **kwargs):
        tool = build_workspace_tools(
            execution_context,
            change_event_callback,
        )["apply_workspace_patch"]
        try:
            result = tool("note.txt", "approved content\n")
        except ApprovalRequired as error:
            return AIExecutionResult(str(error), "waiting_approval", str(error), 4, 1, 0)
        return AIExecutionResult(result, "completed", None, 4, 1, 0)

    runner = JobRunner(store, execute=execute)
    await runner.run(claimed)

    assert store.get_job(job.id).status == JobStatus.WAITING_APPROVAL
    assert not (tmp_path / "note.txt").exists()
    store.decide_approval(job.id, True)
    resumed = store.claim_next_job()
    await runner.run(resumed)

    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "approved content\n"
    assert store.get_job(job.id).status == JobStatus.BLOCKED
    assert store.latest_approval(job.id).status == ApprovalStatus.CONSUMED
    assert "not followed by a successful" in store.get_job(job.id).error


async def test_ai_executor_turns_tool_approval_into_waiting_status(
    monkeypatch,
    tmp_path,
):
    async def fake_chat(session, messages, tool_schemas, think=False):
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_approval",
                        "function": {
                            "name": "apply_workspace_patch",
                            "arguments": {
                                "path": "note.txt",
                                "content": "pending content\n",
                            },
                        },
                    }
                ],
            }
        }

    monkeypatch.setattr(ai.executor.aiohttp, "ClientSession", _FakeClientSession)
    monkeypatch.setattr(ai.client, "chat", fake_chat)
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job(
        "create note",
        workspace=str(tmp_path),
        allow_write=True,
    )
    claimed = store.claim_next_job()

    await JobRunner(store).run(claimed)

    assert store.get_job(job.id).status == JobStatus.WAITING_APPROVAL
    assert store.latest_approval(job.id).action_type.value == "write"
    assert not (tmp_path / "note.txt").exists()


async def test_waiting_approval_does_not_hold_worker(tmp_path):
    store = JobStore(tmp_path / "suto.db")

    async def execute(prompt, execution_context, **kwargs):
        if prompt == "needs approval":
            try:
                execution_context.approval_callback(
                    "command",
                    {"tool": "run_workspace_command", "command": ["pytest", "-q"]},
                    "run pytest -q",
                    "command: pytest -q",
                )
            except ApprovalRequired as error:
                return AIExecutionResult(
                    str(error),
                    "waiting_approval",
                    str(error),
                    1,
                    1,
                    0,
                )
        return AIExecutionResult("done", "completed", None, 1, 1, 0)

    worker = AutomationWorker(
        store,
        JobRunner(store, execute=execute),
        poll_interval=0.01,
    )
    first = worker.submit("needs approval", workspace=tmp_path, allow_command=True)
    second = worker.submit("read only", workspace=tmp_path)
    worker_task = asyncio.create_task(worker.start())

    async def both_settled():
        while (
            store.get_job(first.id).status != JobStatus.WAITING_APPROVAL
            or store.get_job(second.id).status != JobStatus.COMPLETED
        ):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(both_settled(), timeout=1)
    await worker.stop()
    await worker_task

    assert store.get_job(first.id).status == JobStatus.WAITING_APPROVAL
    assert store.get_job(second.id).status == JobStatus.COMPLETED
