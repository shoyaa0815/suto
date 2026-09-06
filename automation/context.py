from dataclasses import dataclass
from pathlib import Path


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


class ExecutionLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionLimits:
    max_elapsed_seconds: float = 900
    max_tokens: int = 100_000
    max_tool_calls: int = 40
    max_changed_files: int = 10
    repeated_tool_call_limit: int = 3


@dataclass(frozen=True)
class ExecutionContext:
    job_id: str
    workspace: Path
    allowed_tools: frozenset[str] = READ_ONLY_WORKSPACE_TOOLS
    plan_store: object | None = None
    command_event_callback: object | None = None
    change_guard_callback: object | None = None
    limits: ExecutionLimits = ExecutionLimits()

    def __post_init__(self) -> None:
        resolved = self.workspace.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"workspace is not a directory: {resolved}")
        object.__setattr__(self, "workspace", resolved)

    def require_tool(self, name: str) -> None:
        if name not in self.allowed_tools:
            raise PermissionError(f"tool is not allowed for this job: {name}")
