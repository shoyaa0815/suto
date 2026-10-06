"""Path and content helpers shared by workspace tools."""

import hashlib
from functools import wraps
from pathlib import Path

from workflows.runtime.context import ExecutionContext
from workflows.errors import ErrorCode, tag_error


def workspace_boundary(handler):
    """Retain exception contracts while identifying workspace access failures."""
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except PermissionError as error:
            if not hasattr(error, "error_code"):
                tag_error(error, ErrorCode.WORKSPACE_PERMISSION_DENIED)
            raise
    return wrapped


def safe_path(context: ExecutionContext, raw_path: str) -> Path:
    candidate = (context.workspace / raw_path).resolve()
    try:
        candidate.relative_to(context.workspace)
    except ValueError as error:
        raise tag_error(PermissionError(f"path escapes workspace: {raw_path}"),
                        ErrorCode.SANDBOX_VIOLATION) from error
    return candidate


def relative_path(context: ExecutionContext, path: Path) -> str:
    return path.relative_to(context.workspace).as_posix() or "."


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
