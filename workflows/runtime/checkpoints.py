"""Workspace checkpoint validation shared by resume admission and execution."""

import hashlib
from pathlib import Path

from workflows.models import Job
from workflows.errors import ErrorCode, WorkflowError
from workflows.storage.store import JobStore


def checkpoint_failure(store: JobStore, job: Job, *, resuming: bool = False) -> WorkflowError | None:
    if job.attempt_count < (1 if resuming else 2):
        return None
    workspace = Path(job.workspace).resolve()
    for change in store.latest_changes_by_path(job.id):
        target = (workspace / change.path).resolve()
        try:
            target.relative_to(workspace)
        except ValueError:
            return WorkflowError(ErrorCode.SANDBOX_VIOLATION,
                                 f"cannot resume: changed path escapes workspace: {change.path}")
        if not target.is_file():
            return WorkflowError(ErrorCode.WORKSPACE_INVALID,
                                 f"cannot resume: previously changed file is missing: {change.path}")
        try:
            current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError as error:
            code = ErrorCode.WORKSPACE_PERMISSION_DENIED if isinstance(error, PermissionError) else ErrorCode.WORKSPACE_INVALID
            return WorkflowError(code, f"cannot resume: previously changed file cannot be read: {change.path}")
        if current_hash != change.after_sha256:
            return WorkflowError(ErrorCode.WORKSPACE_INVALID,
                                 f"cannot resume: workspace file changed after checkpoint: {change.path}")
    return None


def checkpoint_error(store: JobStore, job: Job, *, resuming: bool = False) -> str | None:
    """Preserve the existing text-only checkpoint contract."""
    failure = checkpoint_failure(store, job, resuming=resuming)
    return str(failure) if failure is not None else None
