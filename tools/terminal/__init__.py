"""Terminal execution tool module exposing schemas and runner."""

from typing import Any

from .runner import TerminalResult, TerminalRunner

_default_runner = TerminalRunner()


def run(
    command: str,
    cwd: str | None = None,
    timeout: int | None = None,
) -> str:
    """Execute a terminal command."""
    return _default_runner.run(command, cwd=cwd, timeout=timeout)


terminal_run = run

_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "The shell command to execute in the terminal.",
        },
        "cwd": {
            "type": "string",
            "description": "Optional working directory path for execution.",
        },
        "timeout": {
            "type": "integer",
            "description": "Maximum execution time in seconds (default 30).",
        },
    },
    "required": ["command"],
}

SCHEMA = {
    "type": "function",
    "function": {
        "name": "terminal.run",
        "description": "Execute a shell command and return stdout, stderr, and exit code.",
        "parameters": _PARAMETERS,
    },
}

ALIAS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "terminal_run",
        "description": "Execute a shell command and return stdout, stderr, and exit code.",
        "parameters": _PARAMETERS,
    },
}

PROMPT = (
    "Use terminal.run (or terminal_run) when you need to run non-interactive "
    "shell commands, build tasks, or checks in the environment."
)

TERMINAL_SCHEMAS = [SCHEMA, ALIAS_SCHEMA]
TERMINAL_TOOLS: dict[str, Any] = {
    "terminal.run": run,
    "terminal_run": run,
}

__all__ = [
    "ALIAS_SCHEMA",
    "PROMPT",
    "SCHEMA",
    "TERMINAL_SCHEMAS",
    "TERMINAL_TOOLS",
    "TerminalResult",
    "TerminalRunner",
    "run",
    "terminal_run",
]
