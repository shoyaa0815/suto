"""Filesystem operations with path safety boundaries."""

import os
from pathlib import Path


class FilesystemOperations:
    """Safe filesystem reader and writer with path containment checks."""

    def __init__(self, root_dir: str | Path | None = None):
        self.root_dir = Path(root_dir).resolve() if root_dir else None

    def _resolve_safe_path(self, path_str: str) -> Path:
        resolved = Path(path_str).resolve()
        if self.root_dir is not None:
            try:
                resolved.relative_to(self.root_dir)
            except ValueError:
                raise PermissionError(
                    f"Path '{path_str}' escapes allowed root directory '{self.root_dir}'"
                )
        return resolved

    def read(
        self,
        path: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> str:
        """Read text contents from a file."""
        try:
            target = self._resolve_safe_path(path)
            if not target.exists():
                return f"Error: File not found: {path}"
            if not target.is_file():
                return f"Error: Path is not a file: {path}"

            with open(target, "r", encoding="utf-8", errors="replace") as f:
                if offset > 0:
                    f.seek(offset)
                content = f.read(limit) if limit is not None else f.read()
            return content
        except Exception as exc:
            return f"Error reading file '{path}': {exc}"

    def write(
        self,
        path: str,
        content: str,
        append: bool = False,
    ) -> str:
        """Write or append text content to a file."""
        try:
            target = self._resolve_safe_path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with open(target, mode, encoding="utf-8") as f:
                f.write(content)
            action = "Appended to" if append else "Wrote"
            return f"Successfully {action.lower()} {len(content)} characters to {path}"
        except Exception as exc:
            return f"Error writing file '{path}': {exc}"

    def list(
        self,
        path: str = ".",
        recursive: bool = False,
        max_entries: int = 100,
    ) -> str:
        """List files and directories."""
        try:
            target = self._resolve_safe_path(path)
            if not target.exists():
                return f"Error: Directory not found: {path}"
            if not target.is_dir():
                return f"Error: Path is not a directory: {path}"

            entries: list[str] = []
            if recursive:
                for root, dirs, files in os.walk(target):
                    rel_root = Path(root).relative_to(target)
                    for d in sorted(dirs):
                        entries.append(str(rel_root / d) + "/")
                        if len(entries) >= max_entries:
                            break
                    if len(entries) >= max_entries:
                        break
                    for f in sorted(files):
                        entries.append(str(rel_root / f))
                        if len(entries) >= max_entries:
                            break
                    if len(entries) >= max_entries:
                        break
            else:
                for item in sorted(target.iterdir()):
                    name = item.name + ("/" if item.is_dir() else "")
                    entries.append(name)
                    if len(entries) >= max_entries:
                        break

            output = "\n".join(entries)
            if len(entries) >= max_entries:
                output += f"\n... (limited to {max_entries} entries)"
            return output if output else "(empty directory)"
        except Exception as exc:
            return f"Error listing directory '{path}': {exc}"
