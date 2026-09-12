import difflib
import hashlib
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

from automation.runtime.context import ExecutionContext

MAX_LIST_ENTRIES = 200
MAX_READ_BYTES = 100_000
MAX_SEARCH_FILES = 1_000
MAX_SEARCH_RESULTS = 50
MAX_SEARCH_FILE_BYTES = 1_000_000
MAX_WRITE_BYTES = 100_000
MAX_DIFF_CHARS = 50_000

PROMPT = """- Workspace tools are available only for automation jobs.
- Use list_workspace_files to inspect the workspace before assuming its layout.
- Use search_workspace to locate relevant text, then read_workspace_file for
  the exact files needed.
- apply_workspace_patch is available only for jobs explicitly created with
  write permission. Read an existing file first and pass its sha256 as
  expected_sha256. The exact diff requires user approval before the tool
  replaces the complete file content atomically.
- Never claim to have run a command unless run_workspace_command returned its
  result. File deletion is unavailable.
- All paths must be relative to the workspace. Never try to escape it with
  parent paths or external symlinks."""

LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_workspace_files",
        "description": "List files and directories inside the job workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative directory path. Defaults to the workspace root.",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "List descendants recursively. Defaults to false.",
                },
            },
        },
    },
}

READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_workspace_file",
        "description": "Read one UTF-8 text file inside the job workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to the workspace.",
                },
            },
            "required": ["path"],
        },
    },
}

SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_workspace",
        "description": "Search text files inside the job workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to find, matched case-insensitively.",
                },
                "path": {
                    "type": "string",
                    "description": "Relative file or directory to search. Defaults to root.",
                },
            },
            "required": ["query"],
        },
    },
}

PATCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "apply_workspace_patch",
        "description": (
            "Atomically replace a text file or create a new text file in the "
            "workspace. Requires explicit job write permission."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to the workspace.",
                },
                "content": {
                    "type": "string",
                    "description": "Complete new UTF-8 file content.",
                },
                "expected_sha256": {
                    "type": "string",
                    "description": (
                        "Current sha256 returned by read_workspace_file. "
                        "Required when modifying an existing file."
                    ),
                },
            },
            "required": ["path", "content"],
        },
    },
}


def _safe_path(context: ExecutionContext, raw_path: str) -> Path:
    candidate = (context.workspace / raw_path).resolve()
    try:
        candidate.relative_to(context.workspace)
    except ValueError as error:
        raise PermissionError(f"path escapes workspace: {raw_path}") from error
    return candidate


def _relative(context: ExecutionContext, path: Path) -> str:
    return path.relative_to(context.workspace).as_posix() or "."


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_workspace_tools(
    context: ExecutionContext,
    change_callback: Callable[[dict], object] | None = None,
) -> dict[str, object]:
    def list_workspace_files(
        path: str = ".",
        recursive: bool = False,
    ) -> str:
        context.require_tool("list_workspace_files")
        target = _safe_path(context, path)
        if not target.exists():
            return f"workspace path does not exist: {path}"
        if target.is_file():
            return _relative(context, target)
        if not target.is_dir():
            return f"workspace path is not a directory: {path}"

        candidates = target.rglob("*") if recursive else target.iterdir()
        entries = []
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                resolved.relative_to(context.workspace)
            except (OSError, ValueError):
                continue
            label = _relative(context, candidate)
            entries.append(label + ("/" if candidate.is_dir() else ""))
            if len(entries) == MAX_LIST_ENTRIES:
                break
        entries.sort()
        if not entries:
            return "workspace directory is empty"
        suffix = "\n[results truncated]" if len(entries) == MAX_LIST_ENTRIES else ""
        return "\n".join(entries) + suffix

    def read_workspace_file(path: str) -> str:
        context.require_tool("read_workspace_file")
        target = _safe_path(context, path)
        if not target.exists():
            return f"workspace file does not exist: {path}"
        if not target.is_file():
            return f"workspace path is not a file: {path}"
        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            return (
                f"workspace file is too large: {path} "
                f"({size} bytes, limit {MAX_READ_BYTES})"
            )
        data = target.read_bytes()
        if b"\0" in data:
            return f"workspace file is binary: {path}"
        text = data.decode("utf-8", errors="replace")
        return (
            f"[workspace file: {_relative(context, target)}; "
            f"sha256: {_sha256(data)}]\n{text}"
        )

    def search_workspace(query: str, path: str = ".") -> str:
        context.require_tool("search_workspace")
        needle = query.strip().casefold()
        if not needle:
            return "search query is empty"
        target = _safe_path(context, path)
        if not target.exists():
            return f"workspace path does not exist: {path}"

        candidates = [target] if target.is_file() else target.rglob("*")
        matches = []
        files_checked = 0
        for candidate in candidates:
            if files_checked == MAX_SEARCH_FILES:
                break
            try:
                resolved = candidate.resolve()
                resolved.relative_to(context.workspace)
                if not resolved.is_file():
                    continue
                size = resolved.stat().st_size
                if size > MAX_SEARCH_FILE_BYTES:
                    continue
                data = resolved.read_bytes()
            except (OSError, ValueError):
                continue
            files_checked += 1
            if b"\0" in data:
                continue
            text = data.decode("utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if needle in line.casefold():
                    compact = line.strip()
                    if len(compact) > 240:
                        compact = compact[:237] + "..."
                    matches.append(
                        f"{_relative(context, resolved)}:{line_number}: {compact}"
                    )
                    if len(matches) == MAX_SEARCH_RESULTS:
                        return "\n".join(matches) + "\n[results truncated]"
        return "\n".join(matches) if matches else "no matches found"

    def apply_workspace_patch(
        path: str,
        content: str,
        expected_sha256: str | None = None,
    ) -> str:
        context.require_tool("apply_workspace_patch")
        target = _safe_path(context, path)
        relative_path = _relative(context, target)
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

        before_sha256 = _sha256(old_data) if existed else None
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
            return f"no changes for workspace file: {relative_path}"

        if context.change_guard_callback is not None:
            context.change_guard_callback(relative_path)

        old_text = old_data.decode("utf-8", errors="replace")
        diff = "".join(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{relative_path}" if existed else "/dev/null",
                tofile=f"b/{relative_path}",
            )
        )
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n[diff truncated]\n"

        after_sha256 = _sha256(new_data)
        context.require_approval(
            "write",
            {
                "tool": "apply_workspace_patch",
                "path": relative_path,
                "before_sha256": before_sha256,
                "after_sha256": after_sha256,
            },
            f"replace workspace file {relative_path}",
            diff or f"replace {relative_path} (content changed without a line diff)",
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
                        "path": relative_path,
                        "diff": diff,
                        "before_sha256": before_sha256,
                        "after_sha256": after_sha256,
                    }
                )
            except Exception as error:
                audit_warning = f"\n[change audit failed: {type(error).__name__}]"
        return (
            f"applied workspace patch: {relative_path}\n"
            f"before_sha256={before_sha256 or 'new file'}\n"
            f"after_sha256={after_sha256}\n{diff}{audit_warning}"
        )

    return {
        "list_workspace_files": list_workspace_files,
        "read_workspace_file": read_workspace_file,
        "search_workspace": search_workspace,
        "apply_workspace_patch": apply_workspace_patch,
    }
