"""Approved and atomic workspace write tools."""

import difflib
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

from workflows.runtime.context import ExecutionContext

from .paths import relative_path, safe_path, sha256

MAX_WRITE_BYTES = 100_000
MAX_DIFF_CHARS = 50_000


def build_workspace_write_tools(
    context: ExecutionContext,
    change_callback: Callable[[dict], object] | None = None,
) -> dict[str, object]:
    def apply_workspace_patch(
        path: str,
        content: str,
        expected_sha256: str | None = None,
    ) -> str:
        context.require_tool("apply_workspace_patch")
        target = safe_path(context, path)
        workspace_path = relative_path(context, target)
        if target.exists() and not target.is_file():
            return f"workspace path is not a file: {path}"
        if not target.parent.is_dir():
            return f"workspace parent directory does not exist: {path}"

        new_data = content.encode("utf-8")
        if len(new_data) > MAX_WRITE_BYTES:
            return (
                f"new workspace file is too large: {path} "
                f"({len(new_data)} bytes, limit {MAX_WRITE_BYTES})"
            )

        existed = target.exists()
        old_data = target.read_bytes() if existed else b""
        if b"\0" in old_data:
            return f"workspace file is binary: {path}"
        if len(old_data) > MAX_WRITE_BYTES:
            return (
                f"workspace file is too large to modify: {path} "
                f"({len(old_data)} bytes, limit {MAX_WRITE_BYTES})"
            )

        before_sha256 = sha256(old_data) if existed else None
        if existed and not expected_sha256:
            return (
                "expected_sha256 is required when modifying an existing file; "
                "read the file again before applying the patch"
            )
        if existed and expected_sha256.casefold() != before_sha256:
            return (
                f"workspace file changed since it was read: {path}; "
                f"expected {expected_sha256}, current {before_sha256}"
            )
        if not existed and expected_sha256:
            return f"expected_sha256 must be omitted when creating a new file: {path}"
        if new_data == old_data and existed:
            return f"no changes for workspace file: {workspace_path}"

        if context.change_guard_callback is not None:
            context.change_guard_callback(workspace_path)

        old_text = old_data.decode("utf-8", errors="replace")
        diff = "".join(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{workspace_path}" if existed else "/dev/null",
                tofile=f"b/{workspace_path}",
            )
        )
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n[diff truncated]\n"

        after_sha256 = sha256(new_data)
        context.require_approval(
            "write",
            {
                "tool": "apply_workspace_patch",
                "path": workspace_path,
                "before_sha256": before_sha256,
                "after_sha256": after_sha256,
            },
            f"replace workspace file {workspace_path}",
            diff or f"replace {workspace_path} (content changed without a line diff)",
        )

        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=".suto-write-",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as temporary_file:
                temporary_file.write(new_data)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            if existed:
                temporary_path.chmod(stat.S_IMODE(target.stat().st_mode))
            os.replace(temporary_path, target)
        finally:
            temporary_path.unlink(missing_ok=True)

        audit_warning = ""
        if change_callback is not None:
            try:
                change_callback(
                    {
                        "job_id": context.job_id,
                        "path": workspace_path,
                        "diff": diff,
                        "before_sha256": before_sha256,
                        "after_sha256": after_sha256,
                    }
                )
            except Exception as error:
                audit_warning = f"\n[change audit failed: {type(error).__name__}]"
        return (
            f"applied workspace patch: {workspace_path}\n"
            f"before_sha256={before_sha256 or 'new file'}\n"
            f"after_sha256={after_sha256}\n{diff}{audit_warning}"
        )

    return {"apply_workspace_patch": apply_workspace_patch}
