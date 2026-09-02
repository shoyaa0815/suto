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


@dataclass(frozen=True)
class ExecutionContext:
    job_id: str
    workspace: Path
    allowed_tools: frozenset[str] = READ_ONLY_WORKSPACE_TOOLS

    def __post_init__(self) -> None:
        resolved = self.workspace.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"workspace is not a directory: {resolved}")
        object.__setattr__(self, "workspace", resolved)

    def require_tool(self, name: str) -> None:
        if name not in self.allowed_tools:
            raise PermissionError(f"tool is not allowed for this job: {name}")
