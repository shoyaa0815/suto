import hashlib
import stat

import pytest

from automation.context import (
    READ_ONLY_WORKSPACE_TOOLS,
    WRITE_WORKSPACE_TOOLS,
    ExecutionContext,
    ExecutionLimitExceeded,
)
from tools.workspace import build_workspace_tools


def test_workspace_tools_list_read_and_search(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("Alpha\nimportant value\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text(
        "VALUE = 'important'\n",
        encoding="utf-8",
    )
    tools = build_workspace_tools(ExecutionContext("job_test", workspace))

    listing = tools["list_workspace_files"](recursive=True)
    assert "notes.txt" in listing
    assert "src/app.py" in listing

    content = tools["read_workspace_file"]("notes.txt")
    assert "[workspace file: notes.txt; sha256:" in content
    assert "important value" in content

    matches = tools["search_workspace"]("IMPORTANT")
    assert "notes.txt:2" in matches
    assert "src/app.py:1" in matches


def test_workspace_tools_block_parent_traversal(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    tools = build_workspace_tools(ExecutionContext("job_test", workspace))

    with pytest.raises(PermissionError, match="escapes workspace"):
        tools["read_workspace_file"]("../secret.txt")


def test_workspace_tools_block_external_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)
    tools = build_workspace_tools(ExecutionContext("job_test", workspace))

    with pytest.raises(PermissionError, match="escapes workspace"):
        tools["read_workspace_file"]("link.txt")


def test_execution_context_enforces_tool_permissions(tmp_path):
    context = ExecutionContext(
        "job_test",
        tmp_path,
        allowed_tools=frozenset({"list_workspace_files"}),
    )
    tools = build_workspace_tools(context)

    with pytest.raises(PermissionError, match="not allowed"):
        tools["read_workspace_file"]("notes.txt")


def _write_context(workspace):
    return ExecutionContext(
        "job_test",
        workspace,
        allowed_tools=READ_ONLY_WORKSPACE_TOOLS | WRITE_WORKSPACE_TOOLS,
        approval_callback=lambda *args: None,
    )


def test_workspace_patch_requires_explicit_write_permission(tmp_path):
    tools = build_workspace_tools(ExecutionContext("job_test", tmp_path))

    with pytest.raises(PermissionError, match="not allowed"):
        tools["apply_workspace_patch"]("new.txt", "hello")


def test_workspace_patch_updates_atomically_and_records_diff(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("old value\n", encoding="utf-8")
    target.chmod(0o640)
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    changes = []
    tools = build_workspace_tools(_write_context(tmp_path), changes.append)

    result = tools["apply_workspace_patch"](
        "notes.txt",
        "new value\n",
        expected_sha256=before,
    )

    assert target.read_text(encoding="utf-8") == "new value\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert "-old value" in result
    assert "+new value" in result
    assert changes[0]["path"] == "notes.txt"
    assert changes[0]["before_sha256"] == before
    assert not list(tmp_path.glob(".suto-write-*"))


def test_workspace_patch_rejects_stale_hash_without_modifying_file(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("current\n", encoding="utf-8")
    tools = build_workspace_tools(_write_context(tmp_path))

    result = tools["apply_workspace_patch"](
        "notes.txt",
        "replacement\n",
        expected_sha256="0" * 64,
    )

    assert "changed since it was read" in result
    assert target.read_text(encoding="utf-8") == "current\n"


def test_workspace_patch_creates_new_file_and_blocks_external_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)
    changes = []
    tools = build_workspace_tools(_write_context(workspace), changes.append)

    result = tools["apply_workspace_patch"]("created.txt", "hello\n")
    assert "applied workspace patch" in result
    assert (workspace / "created.txt").read_text(encoding="utf-8") == "hello\n"
    assert changes[0]["before_sha256"] is None

    with pytest.raises(PermissionError, match="escapes workspace"):
        tools["apply_workspace_patch"](
            "link.txt",
            "leak",
            expected_sha256=hashlib.sha256(b"secret").hexdigest(),
        )
    assert outside.read_text(encoding="utf-8") == "secret"


def test_workspace_patch_checks_change_budget_before_writing(tmp_path):
    def reject_change(path):
        raise ExecutionLimitExceeded("job file-change limit reached")

    context = ExecutionContext(
        "job_limited",
        tmp_path,
        allowed_tools=READ_ONLY_WORKSPACE_TOOLS | WRITE_WORKSPACE_TOOLS,
        change_guard_callback=reject_change,
    )
    tools = build_workspace_tools(context)

    with pytest.raises(ExecutionLimitExceeded, match="file-change limit"):
        tools["apply_workspace_patch"]("blocked.txt", "content")

    assert not (tmp_path / "blocked.txt").exists()
