"""Compatibility adapter from job configuration to generic sandbox policy."""

from sandbox import Sandbox, SandboxPolicy
from workflows.errors import ErrorCode, tag_error


def sandbox_command(context, command: list[str], temp_dir: str, timeout: int) -> list[str]:
    policy = SandboxPolicy(
        context.sandbox,
        context.workspace,
        writable=context.can_tool("apply_workspace_patch"),
    )
    try:
        return Sandbox(policy).command(command, temp_dir, timeout)
    except PermissionError as error:
        raise tag_error(error, ErrorCode.SANDBOX_VIOLATION)
