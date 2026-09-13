"""Path and content helpers shared by workspace tools."""

import hashlib
from pathlib import Path

from workflows.runtime.context import ExecutionContext


def safe_path(context: ExecutionContext, raw_path: str) -> Path:
    candidate = (context.workspace / raw_path).resolve()
    try:
        candidate.relative_to(context.workspace)
    except ValueError as error:
        raise PermissionError(f"path escapes workspace: {raw_path}") from error
    return candidate


def relative_path(context: ExecutionContext, path: Path) -> str:
    return path.relative_to(context.workspace).as_posix() or "."


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
