"""Read-only workspace inspection tools."""

from workflows.runtime.context import ExecutionContext

from .paths import relative_path, safe_path, sha256

MAX_LIST_ENTRIES = 200
MAX_READ_BYTES = 100_000
MAX_SEARCH_FILES = 1_000
MAX_SEARCH_RESULTS = 50
MAX_SEARCH_FILE_BYTES = 1_000_000


def build_workspace_read_tools(context: ExecutionContext) -> dict[str, object]:
    def list_workspace_files(
        path: str = ".",
        recursive: bool = False,
    ) -> str:
        context.require_tool("list_workspace_files")
        target = safe_path(context, path)
        if not target.exists():
            return f"workspace path does not exist: {path}"
        if target.is_file():
            return relative_path(context, target)
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
            label = relative_path(context, candidate)
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
        target = safe_path(context, path)
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
            f"[workspace file: {relative_path(context, target)}; "
            f"sha256: {sha256(data)}]\n{text}"
        )

    def search_workspace(query: str, path: str = ".") -> str:
        context.require_tool("search_workspace")
        needle = query.strip().casefold()
        if not needle:
            return "search query is empty"
        target = safe_path(context, path)
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
                        f"{relative_path(context, resolved)}:{line_number}: {compact}"
                    )
                    if len(matches) == MAX_SEARCH_RESULTS:
                        return "\n".join(matches) + "\n[results truncated]"
        return "\n".join(matches) if matches else "no matches found"

    return {
        "list_workspace_files": list_workspace_files,
        "read_workspace_file": read_workspace_file,
        "search_workspace": search_workspace,
    }
