from automation.models import MissedRunPolicy, ScheduleKind
from automation.storage.store import JobStore
from clients.tui.backend import (
    _parse_run,
    _parse_schedule,
    _print_commands,
    _print_help,
    _print_job_status,
    _print_plan,
)
from clients.tui.progress import format_elapsed, print_progress, progress_style


def test_format_elapsed_uses_minutes_and_seconds():
    assert format_elapsed(125) == "02:05"


def test_print_help_lists_exit_commands(capsys):
    _print_help()

    output = capsys.readouterr().out
    assert "/help" in output
    assert "/setting" in output
    assert "/noti" in output
    assert "/noti del <reminder_id>" in output
    assert "/exit" in output
    assert "/quit" not in output
    assert "/plan" not in output
    assert "/resume" not in output
    assert "/approve" not in output
    assert "/reject" not in output


def test_developer_help_only_lists_public_commands(capsys):
    _print_help("developer")

    output = capsys.readouterr().out
    assert "/help" in output
    assert "/exit" in output
    assert "file-write" not in output


def test_job_status_labels_blocked_reason(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("loop forever")
    store.claim_next_job()
    store.block_job(job.id, "repeated tool call")

    _print_job_status(store, job.id)

    output = capsys.readouterr().out
    assert "Status: blocked" in output
    assert "Blocked reason: repeated tool call" in output


def test_job_status_shows_pending_approval_preview(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("update docs", allow_write=True)
    store.claim_next_job()
    store.request_or_consume_approval(
        job.id,
        "write",
        {"tool": "apply_workspace_patch", "path": "README.md", "hash": "new"},
        "replace workspace file README.md",
        "--- a/README.md\n+++ b/README.md\n-old\n+new\n",
    )

    _print_job_status(store, job.id)

    output = capsys.readouterr().out
    assert "Status: waiting_approval" in output
    assert "(write, pending)" in output
    assert "replace workspace file README.md" in output
    assert "+new" in output


def test_print_plan_shows_persistent_steps(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("inspect project")
    store.claim_next_job()
    store.create_plan(job.id, ["Inspect files", "Write report"])
    store.update_step(job.id, 1, "completed", "files inspected")

    _print_plan(store, job.id)

    output = capsys.readouterr().out
    assert f"Plan for {job.id}:" in output
    assert "1. [completed] Inspect files — files inspected" in output
    assert "2. [pending] Write report" in output


def test_print_commands_shows_audit_output(tmp_path, capsys):
    store = JobStore(tmp_path / "suto.db")
    job = store.create_job("run checks", allow_command=True)
    store.add_command_event(
        job.id,
        {
            "command": ["pytest", "-q"],
            "status": "completed",
            "exit_code": 0,
            "stdout": "2 passed",
            "stderr": "",
            "elapsed_seconds": 0.4,
        },
    )

    _print_commands(store, job.id)

    output = capsys.readouterr().out
    assert "pytest -q" in output
    assert "[completed] exit=0" in output
    assert "2 passed" in output


def test_parse_run_accepts_workspace(tmp_path):
    task, workspace, allow_write, allow_command = _parse_run(
        f'--workspace "{tmp_path}" "inspect this project"'
    )

    assert task == "inspect this project"
    assert workspace == tmp_path.resolve()
    assert allow_write is False
    assert allow_command is False


def test_parse_run_accepts_write_permission_and_workspace(tmp_path):
    task, workspace, allow_write, allow_command = _parse_run(
        f'--allow-write --allow-command --workspace "{tmp_path}" "update docs"'
    )

    assert task == "update docs"
    assert workspace == tmp_path.resolve()
    assert allow_write is True
    assert allow_command is True


def test_parse_run_rejects_missing_workspace(tmp_path):
    missing = tmp_path / "missing"

    try:
        _parse_run(f"--workspace {missing} inspect")
    except ValueError as error:
        assert "not a directory" in str(error)
    else:
        raise AssertionError("missing workspace was accepted")


def test_parse_schedule_accepts_cron_permissions_and_retry(tmp_path):
    options = _parse_schedule(
        f'--cron "0 9 * * 1-5" --timezone Asia/Bangkok '
        f'--workspace "{tmp_path}" --allow-write --allow-command '
        '--missed-run skip --retry 2 --retry-delay 30 "update report"'
    )

    assert options["kind"] == ScheduleKind.CRON
    assert options["expression"] == "0 9 * * 1-5"
    assert options["timezone"] == "Asia/Bangkok"
    assert options["workspace"] == tmp_path.resolve()
    assert options["allow_write"] is True
    assert options["allow_command"] is True
    assert options["missed_run_policy"] == MissedRunPolicy.SKIP
    assert options["retry_limit"] == 2
    assert options["retry_delay_seconds"] == 30
    assert options["prompt"] == "update report"


def test_print_progress_hides_heartbeat(capsys):
    print_progress(
        {
            "activity": "tool",
            "detail": "search_web (query=latest news)",
            "elapsed_seconds": 65,
            "activity_elapsed_seconds": 12,
            "prompt_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "heartbeat": True,
        }
    )

    assert capsys.readouterr().out == ""


def test_print_progress_is_short_and_has_no_timing(capsys):
    print_progress(
        {
            "activity": "tool",
            "detail": "search_web (query=latest news)",
            "elapsed_seconds": 65,
            "activity_elapsed_seconds": 12,
            "prompt_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "heartbeat": False,
        }
    )

    output = capsys.readouterr().out
    assert output == "search_web (query=latest news)\n"
    assert "01:05" not in output
    assert "00:12" not in output
    assert "tokens" not in output
    assert "→" not in output
    assert "✓" not in output


def test_print_progress_shows_tool_result_without_timing(capsys):
    print_progress(
        {
            "activity": "tool_done",
            "detail": "search_web (query=latest news): finished",
            "elapsed_seconds": 65,
            "activity_elapsed_seconds": 12,
            "prompt_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "heartbeat": False,
        }
    )

    assert capsys.readouterr().out == (
        "search_web (query=latest news): finished\n"
    )


def test_progress_uses_text_colors_for_tool_status():
    assert progress_style({"activity": "tool", "detail": "search_web"}) == "#60a5fa"
    assert progress_style(
        {"activity": "tool_done", "detail": "search_web: finished"}
    ) == "#4ade80"
    assert progress_style(
        {"activity": "tool_done", "detail": "search_web: failed"}
    ) == "#f87171"


def test_print_progress_hides_non_tool_activity(capsys):
    print_progress(
        {
            "activity": "model",
            "detail": "waiting for AI (loop 1/8)",
            "elapsed_seconds": 1,
            "activity_elapsed_seconds": 1,
            "prompt_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "heartbeat": False,
        }
    )

    assert capsys.readouterr().out == ""


def test_print_progress_can_label_a_discord_request(capsys):
    print_progress(
        {
            "activity": "finished",
            "detail": "completed",
            "elapsed_seconds": 5,
            "activity_elapsed_seconds": 0,
            "prompt_tokens": 80,
            "output_tokens": 20,
            "total_tokens": 100,
            "heartbeat": False,
        },
        prefix="discord:123:456",
    )

    assert capsys.readouterr().out == (
        "[discord:123:456] completed · 100 tokens\n"
    )
