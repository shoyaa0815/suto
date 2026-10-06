"""Stable machine-readable codes for durable workflow failures."""

import asyncio
from enum import StrEnum
from typing import TypeVar


class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    WORKSPACE_INVALID = "WORKSPACE_INVALID"
    WORKSPACE_PERMISSION_DENIED = "WORKSPACE_PERMISSION_DENIED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    WORKER_UNAVAILABLE = "WORKER_UNAVAILABLE"
    AUTOMATION_NOT_FOUND = "AUTOMATION_NOT_FOUND"
    AUTOMATION_VERSION_NOT_FOUND = "AUTOMATION_VERSION_NOT_FOUND"
    INVALID_PARAMETER = "INVALID_PARAMETER"
    SKILL_NOT_FOUND = "SKILL_NOT_FOUND"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    COMMAND_DENIED = "COMMAND_DENIED"
    SANDBOX_VIOLATION = "SANDBOX_VIOLATION"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


SAFE_MESSAGES = {
    ErrorCode.INVALID_INPUT: "The request contains invalid input.",
    ErrorCode.WORKSPACE_INVALID: "The workspace is unavailable or has changed.",
    ErrorCode.WORKSPACE_PERMISSION_DENIED: "Access to the workspace was denied.",
    ErrorCode.QUOTA_EXCEEDED: "The job exceeded a quota or execution budget.",
    ErrorCode.WORKER_UNAVAILABLE: "The automation worker is unavailable.",
    ErrorCode.AUTOMATION_NOT_FOUND: "Automation not found.",
    ErrorCode.AUTOMATION_VERSION_NOT_FOUND: "Automation version not found.",
    ErrorCode.INVALID_PARAMETER: "Automation parameters are invalid.",
    ErrorCode.SKILL_NOT_FOUND: "An automation skill is unavailable.",
    ErrorCode.APPROVAL_DENIED: "The action was denied approval.",
    ErrorCode.APPROVAL_EXPIRED: "Approval expired; a new approval is required.",
    ErrorCode.COMMAND_DENIED: "The command was denied by job policy.",
    ErrorCode.SANDBOX_VIOLATION: "The action could not satisfy the required sandbox policy.",
    ErrorCode.CANCELLED: "The job was cancelled.",
    ErrorCode.INTERRUPTED: "The job was interrupted; safe resume is available.",
    ErrorCode.PROVIDER_ERROR: "The provider could not complete the request.",
    ErrorCode.RETRY_EXHAUSTED: "The job exhausted its retry allowance.",
    ErrorCode.INTERNAL_ERROR: "The job could not be completed.",
}


def normalize_error_code(code: object) -> ErrorCode:
    try:
        return ErrorCode(code)
    except (ValueError, TypeError):
        return ErrorCode.INTERNAL_ERROR


ExceptionT = TypeVar("ExceptionT", bound=BaseException)


def tag_error(error: ExceptionT, code: ErrorCode) -> ExceptionT:
    """Attach metadata at a workflow boundary without changing exception types."""
    error.error_code = code
    error.safe_message = SAFE_MESSAGES[code]
    return error


class WorkflowError(ValueError):
    """A backward-compatible ValueError carrying a safe workflow error code."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        self.error_code = ErrorCode(code)
        self.safe_message = SAFE_MESSAGES[self.error_code]
        super().__init__(message)


def error_code_for_exception(error: BaseException) -> ErrorCode:
    """Return the explicit semantic code, falling back safely for unknown errors."""
    if isinstance(error, asyncio.CancelledError):
        return ErrorCode.CANCELLED
    return normalize_error_code(getattr(error, "error_code", None))


def safe_error_message(error: BaseException) -> str:
    """Expose only messages explicitly marked safe; unknown errors stay generic."""
    return SAFE_MESSAGES[error_code_for_exception(error)]
