"""Python execution tool module exposing schemas and runner."""

from typing import Any

from .runner import PythonResult, PythonRunner

_default_runner = PythonRunner()


def run(code: str, timeout: int | None = None) -> str:
    """Execute Python code in an isolated subprocess."""
    return _default_runner.run(code, timeout=timeout)


python_run = run

_PARAMETERS = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "The Python code snippet to execute.",
        },
        "timeout": {
            "type": "integer",
            "description": "Maximum execution time in seconds (default 15).",
        },
    },
    "required": ["code"],
}

SCHEMA = {
    "type": "function",
    "function": {
        "name": "python.run",
        "description": "Execute a Python code snippet and return stdout, stderr, and exit status.",
        "parameters": _PARAMETERS,
    },
}

ALIAS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "python_run",
        "description": "Execute a Python code snippet and return stdout, stderr, and exit status.",
        "parameters": _PARAMETERS,
    },
}

PROMPT = (
    "Use python.run (or python_run) when you need to calculate, parse, simulate, "
    "or execute Python code."
)

PYTHON_SCHEMAS = [SCHEMA, ALIAS_SCHEMA]
PYTHON_TOOLS: dict[str, Any] = {
    "python.run": run,
    "python_run": run,
}

__all__ = [
    "ALIAS_SCHEMA",
    "PROMPT",
    "PYTHON_SCHEMAS",
    "PYTHON_TOOLS",
    "PythonResult",
    "PythonRunner",
    "python_run",
    "run",
]
