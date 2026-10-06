from dataclasses import dataclass
from pathlib import Path

from application.runtime_configuration import load_runtime_settings
from permissions import Approval, PermissionEngine, PermissionPolicy

from ..models import ActionType
from ..errors import ErrorCode, WorkflowError, tag_error


READ_ONLY_WORKSPACE_TOOLS = frozenset(
    {
        "list_workspace_files",
        "read_workspace_file",
        "search_workspace",
    }
)
WRITE_WORKSPACE_TOOLS = frozenset({"apply_workspace_patch"})
ALL_WORKSPACE_TOOLS = READ_ONLY_WORKSPACE_TOOLS | WRITE_WORKSPACE_TOOLS
PLANNING_TOOLS = frozenset({"create_plan", "update_step", "revise_plan"})
COMMAND_TOOLS = frozenset({"run_workspace_command"})
ACTION_POLICIES = {
    ActionType.READ: "allow",
    ActionType.WRITE: "require_approval",
    ActionType.COMMAND: "require_approval",
    ActionType.DESTRUCTIVE: "deny",
}


class ApprovalRequired(RuntimeError):
    """Raised when an exact state-changing action needs user approval."""

    def __init__(self, approval_id: str, summary: str) -> None:
        self.approval_id = approval_id
        self.summary = summary
        super().__init__(f"approval required ({approval_id}): {summary}")


class ExecutionLimitExceeded(RuntimeError):
    error_code = ErrorCode.QUOTA_EXCEEDED


_runtime_limits = load_runtime_settings().limits


@dataclass(frozen=True)
class ExecutionLimits:
    max_elapsed_seconds: float = _runtime_limits.max_elapsed_seconds
    max_tokens: int = _runtime_limits.max_tokens
    max_tool_calls: int = _runtime_limits.max_tool_calls
    max_changed_files: int = _runtime_limits.max_changed_files
    repeated_tool_call_limit: int = _runtime_limits.repeated_tool_call_limit


@dataclass(frozen=True)
class ExecutionContext:
    job_id: str
    workspace: Path
    allowed_tools: frozenset[str] = READ_ONLY_WORKSPACE_TOOLS
    plan_store: object | None = None
    command_event_callback: object | None = None
    change_guard_callback: object | None = None
    approval_callback: object | None = None
    limits: ExecutionLimits = ExecutionLimits()
    sandbox: str = 'process'
    budget_check: object | None = None
    budget_seconds: object | None = None
    workspace_is_pinned: bool = False

    def __post_init__(self) -> None:
        if self.sandbox not in {"process", "bwrap"}:
            raise WorkflowError(ErrorCode.INVALID_INPUT, "sandbox must be process or bwrap")
        try:
            resolved = self.workspace.expanduser().resolve()
            if self.workspace_is_pinned and resolved != self.workspace:
                raise WorkflowError(ErrorCode.SANDBOX_VIOLATION, "job workspace identity has changed")
            if not resolved.is_dir():
                raise WorkflowError(ErrorCode.WORKSPACE_INVALID, f"workspace is not a directory: {resolved}")
        except PermissionError as error:
            raise tag_error(error, ErrorCode.WORKSPACE_PERMISSION_DENIED)
        object.__setattr__(self, "workspace", resolved)

    def can_tool(self, name: str) -> bool:
        engine = PermissionEngine(PermissionPolicy({tool: "allow" for tool in self.allowed_tools}))
        return engine.decide(name).allowed

    def require_workspace(self) -> None:
        if self.workspace.resolve() != self.workspace:
            raise tag_error(PermissionError("job workspace identity has changed"),
                            ErrorCode.SANDBOX_VIOLATION)

    def require_tool(self, name: str) -> None:
        if not self.can_tool(name):
            code = (
                ErrorCode.COMMAND_DENIED if name in COMMAND_TOOLS else
                ErrorCode.WORKSPACE_PERMISSION_DENIED if name in ALL_WORKSPACE_TOOLS else
                ErrorCode.INTERNAL_ERROR
            )
            raise tag_error(PermissionError(f"tool is not allowed for this job: {name}"), code)
        self.require_workspace()

    def require_approval(
        self,
        action_type: str,
        action: dict,
        summary: str,
        preview: str,
    ) -> None:
        self.require_workspace()
        kind = ActionType(action_type)
        engine = PermissionEngine(PermissionPolicy({item.value: rule for item, rule in ACTION_POLICIES.items()}))
        callback = self.approval_callback

        def approve(request):
            accepted = callback(request.action_type, request.action, request.summary, request.preview)
            if accepted is not True:
                raise tag_error(PermissionError(f"approval denied for {kind.value} action"),
                                ErrorCode.APPROVAL_DENIED)
            return True

        try:
            engine.require(
                kind.value,
                approval=Approval(kind.value, action, summary, preview),
                approval_callback=approve if callback is not None else None,
            )
            self.require_workspace()
        except PermissionError as error:
            if not hasattr(error, "error_code"):
                code = ErrorCode.COMMAND_DENIED if kind == ActionType.COMMAND else ErrorCode.WORKSPACE_PERMISSION_DENIED
                tag_error(error, code)
            raise
