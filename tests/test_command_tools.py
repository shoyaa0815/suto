import asyncio

import pytest

from automation.context import COMMAND_TOOLS, WRITE_WORKSPACE_TOOLS, ExecutionContext
from automation.store import JobStore
from tools.command import _prepare_command, build_command_tools


def _context(tmp_path, *, allow_write=False):
    allowed = COMMAND_TOOLS
    if allow_write:
        allowed = allowed | WRITE_WORKSPACE_TOOLS
    return ExecutionContext(
        "job_test",
        tmp_path,
        allowed_tools=allowed,
        approval_callback=lambda *args: None,
    )


def test_command_validation_accepts_checks_and_rejects_shell(tmp_path):
    context = _context(tmp_path)

    pytest_command = _prepare_command(context, ["pytest", "-q", "tests"])
    assert pytest_command[1:4] == ["-m", "pytest", "-p"]
    assert pytest_command[-2:] == ["-q", "tests"]

    git_command = _prepare_command(context, ["git", "diff", "--check"])
    assert "--no-ext-diff" in git_command

    with pytest.raises(PermissionError, match="not allowlisted"):
        _prepare_command(context, ["bash", "-c", "echo unsafe"])
    with pytest.raises(PermissionError, match="allowed only"):
        _prepare_command(context, ["python", "script.py"])
    with pytest.raises(PermissionError, match="escapes workspace"):
        _prepare_command(context, ["pytest", "../outside.py"])


def test_compileall_requires_write_permission(tmp_path):
    with pytest.raises(PermissionError, match="write permission"):
        _prepare_command(
            _context(tmp_path),
            ["python", "-m", "compileall", "."],
        )

    prepared = _prepare_command(
        _context(tmp_path, allow_write=True),
        ["python", "-m", "compileall", "-q", "."],
    )
    assert prepared[1:4] == ["-m", "compileall", "-q"]


async def test_command_runs_check_and_persists_audit(tmp_path):
    (tmp_path / "test_sample.py").write_text(
        "def test_ok():\n    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("run tests", allow_command=True)
    store.claim_next_job()
    context = ExecutionContext(
        job.id,
        tmp_path,
        allowed_tools=COMMAND_TOOLS,
        command_event_callback=lambda event: store.add_command_event(job.id, event),
        approval_callback=lambda *args: None,
    )
    tool = build_command_tools(
        context,
        context.command_event_callback,
    )["run_workspace_command"]

    result = await tool(["pytest", "-q", "test_sample.py"])

    assert "status: completed" in result
    assert "exit_code: 0" in result
    assert "1 passed" in result
    event = store.latest_command_event(job.id)
    assert event.status == "completed"
    assert event.exit_code == 0
    assert "1 passed" in event.stdout


async def test_command_timeout_kills_process_and_records_event(tmp_path):
    (tmp_path / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(10)\n",
        encoding="utf-8",
    )
    events = []
    tool = build_command_tools(
        _context(tmp_path),
        events.append,
    )["run_workspace_command"]

    result = await tool(["pytest", "-q", "test_slow.py"], timeout_seconds=1)

    assert "status: timed_out" in result
    assert events[0]["status"] == "timed_out"
    assert events[0]["exit_code"] is not None


async def test_command_cancellation_kills_process_and_records_event(tmp_path):
    (tmp_path / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(10)\n",
        encoding="utf-8",
    )
    events = []
    tool = build_command_tools(
        _context(tmp_path),
        events.append,
    )["run_workspace_command"]
    task = asyncio.create_task(tool(["pytest", "-q", "test_slow.py"]))
    await asyncio.sleep(0.2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events[0]["status"] == "cancelled"
