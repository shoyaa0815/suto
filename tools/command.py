import asyncio
import inspect
import os
import shlex
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from automation.context import ExecutionContext
from automation.sandbox import sandbox_command


COMMAND_TOOL_NAMES = frozenset({"run_workspace_command"})
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 300
MAX_OUTPUT_BYTES = 100_000
MAX_COMMAND_PARTS = 32
MAX_ARGUMENT_CHARS = 1_000
SAFE_PATH = f"{Path(sys.executable).parent}:/usr/local/bin:/usr/bin:/bin"

PROMPT = """- run_workspace_command is available only when the job explicitly
  grants command permission. Use it to inspect or verify work, never to install
  packages, start services, access the network, or modify files directly.
- Only the documented git, pytest, compileall, and ruff checks are accepted.
  Each exact command requires user approval before execution.
  A command failure is evidence: inspect its output, update the plan, and fix the
  cause instead of claiming success.
- After changing code, run the most relevant permitted test or check and record
  its result in the current plan step before finishing."""

SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_workspace_command",
        "description": (
            "Run an allowlisted verification command in the job workspace. "
            "Accepted commands include git status/diff, pytest, python -m "
            "pytest, python -m compileall, and ruff check."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_COMMAND_PARTS,
                    "description": (
                        "Command and arguments as separate array items; shell "
                        "syntax, redirects, and pipelines are not supported."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TIMEOUT_SECONDS,
                    "description": "Execution timeout. Defaults to 120 seconds.",
                },
            },
            "required": ["command"],
        },
    },
}


def _workspace_argument(context: ExecutionContext, value: str) -> str:
    path_part, separator, selector = value.partition("::")
    candidate = (context.workspace / path_part).resolve()
    try:
        relative = candidate.relative_to(context.workspace)
    except ValueError as error:
        raise PermissionError(f"command path escapes workspace: {value}") from error
    normalized = relative.as_posix() or "."
    return normalized + (separator + selector if separator else "")


def _validate_pytest_args(context: ExecutionContext, args: list[str]) -> list[str]:
    allowed_flags = {
        "-q",
        "-x",
        "--collect-only",
        "--disable-warnings",
        "--failed-first",
        "--new-first",
    }
    allowed_prefixes = ("--maxfail=", "--tb=")
    validated = []
    for argument in args:
        if argument in allowed_flags or argument.startswith(allowed_prefixes):
            validated.append(argument)
        elif argument.startswith("-"):
            raise PermissionError(f"pytest option is not allowed: {argument}")
        else:
            validated.append(_workspace_argument(context, argument))
    return validated


def _validate_compile_args(context: ExecutionContext, args: list[str]) -> list[str]:
    if "apply_workspace_patch" not in context.allowed_tools:
        raise PermissionError("compileall requires job write permission")
    validated = []
    for argument in args:
        if argument in {"-q", "-f"}:
            validated.append(argument)
        elif argument.startswith("-"):
            raise PermissionError(f"compileall option is not allowed: {argument}")
        else:
            validated.append(_workspace_argument(context, argument))
    return validated or ["."]


def _resolve_executable(name: str) -> str:
    executable = shutil.which(name, path=SAFE_PATH)
    if executable is None:
        raise RuntimeError(f"required executable is unavailable: {name}")
    return executable


def _prepare_command(context: ExecutionContext, command: list[str]) -> list[str]:
    if not isinstance(command, list) or not command:
        raise ValueError("command must be a non-empty array")
    if len(command) > MAX_COMMAND_PARTS:
        raise ValueError(f"command is limited to {MAX_COMMAND_PARTS} parts")
    parts = [str(part) for part in command]
    if any(not part or "\0" in part for part in parts):
        raise ValueError("command arguments cannot be empty or contain NUL")
    if any(len(part) > MAX_ARGUMENT_CHARS for part in parts):
        raise ValueError(
            f"each command argument is limited to {MAX_ARGUMENT_CHARS} characters"
        )

    program, args = parts[0], parts[1:]
    if program == "pytest":
        return [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            *_validate_pytest_args(context, args),
        ]
    if program in {"python", "python3"}:
        if len(args) < 2 or args[0] != "-m":
            raise PermissionError("python is allowed only with -m pytest or -m compileall")
        module, module_args = args[1], args[2:]
        if module == "pytest":
            return [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                *_validate_pytest_args(context, module_args),
            ]
        if module == "compileall":
            return [
                sys.executable,
                "-m",
                "compileall",
                *_validate_compile_args(context, module_args),
            ]
        raise PermissionError(f"python module is not allowed: {module}")
    if program == "git":
        if not args:
            raise PermissionError("git requires an allowed subcommand")
        subcommand, options = args[0], args[1:]
        git = _resolve_executable("git")
        prefix = [
            git,
            "-c",
            "core.pager=cat",
            "-c",
            "core.fsmonitor=false",
        ]
        if subcommand == "status":
            allowed = {
                "--short",
                "--porcelain",
                "--branch",
                "--untracked-files=no",
                "--untracked-files=normal",
                "--untracked-files=all",
            }
            if any(option not in allowed for option in options):
                raise PermissionError("git status contains an unsupported option")
            return [*prefix, "status", *options]
        if subcommand == "diff":
            allowed = {
                "--check",
                "--stat",
                "--name-only",
                "--name-status",
                "--cached",
                "--staged",
            }
            if any(option not in allowed for option in options):
                raise PermissionError("git diff contains an unsupported option")
            return [
                *prefix,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                *options,
            ]
        raise PermissionError(f"git subcommand is not allowed: {subcommand}")
    if program == "ruff":
        if not args or args[0] != "check":
            raise PermissionError("ruff is allowed only with the check subcommand")
        paths = []
        for argument in args[1:]:
            if argument.startswith("-"):
                raise PermissionError(f"ruff option is not allowed: {argument}")
            paths.append(_workspace_argument(context, argument))
        return [_resolve_executable("ruff"), "check", "--no-cache", *(paths or ["."])]
    raise PermissionError(f"command is not allowlisted: {program}")


async def _read_limited(stream: asyncio.StreamReader) -> tuple[str, bool]:
    content = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(16_384)
        if not chunk:
            break
        remaining = MAX_OUTPUT_BYTES - len(content)
        if remaining > 0:
            content.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
    return content.decode("utf-8", errors="replace"), truncated


async def _terminate(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.returncode is None:
            process.kill()
    if process.returncode is None:
        await process.wait()


def _format_result(event: dict) -> str:
    lines = [
        f"command: {shlex.join(event['command'])}",
        f"status: {event['status']}",
        f"exit_code: {event['exit_code']}",
        f"elapsed_seconds: {event['elapsed_seconds']:.3f}",
    ]
    if event["stdout"]:
        lines.extend(["stdout:", event["stdout"]])
    if event["stderr"]:
        lines.extend(["stderr:", event["stderr"]])
    return "\n".join(lines)


def build_command_tools(
    context: ExecutionContext,
    event_callback: Callable[[dict], object] | None = None,
) -> dict[str, object]:
    async def run_workspace_command(
        command: list[str],
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> str:
        context.require_tool("run_workspace_command")
        prepared = _prepare_command(context, command)
        timeout = min(max(int(timeout_seconds), 1), MAX_TIMEOUT_SECONDS)
        requested = [str(part) for part in command]
        command_preview = shlex.join(requested)
        context.require_approval(
            "command",
            {
                "tool": "run_workspace_command",
                "command": requested,
                "timeout_seconds": timeout,
                "sandbox": context.sandbox,
            },
            f"run workspace command: {command_preview}",
            (
                f"command: {command_preview}\n"
                f"workspace: {context.workspace}\n"
                f"timeout_seconds: {timeout}"
                f"\nsandbox: {context.sandbox}"
            ),
        )
        started = time.perf_counter()
        event = {
            "job_id": context.job_id,
            "command": command,
            "status": "running",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "elapsed_seconds": 0.0,
        }

        with tempfile.TemporaryDirectory(prefix="suto-command-") as temp_dir:
            environment = {
                "PATH": SAFE_PATH,
                "HOME": temp_dir,
                "TMPDIR": temp_dir,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            process = await asyncio.create_subprocess_exec(
                *sandbox_command(context, prepared, temp_dir, timeout),
                cwd=context.workspace,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            stdout_task = asyncio.create_task(_read_limited(process.stdout))
            stderr_task = asyncio.create_task(_read_limited(process.stderr))
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
                event["status"] = "completed" if process.returncode == 0 else "failed"
                # A command may leave children holding stdout/stderr open. Kill
                # the isolated process group after the leader exits so those
                # children cannot outlive the job or stall output collection.
                await _terminate(process)
            except TimeoutError:
                event["status"] = "timed_out"
                await _terminate(process)
            except asyncio.CancelledError:
                event["status"] = "cancelled"
                await _terminate(process)
                stdout, stdout_truncated = await stdout_task
                stderr, stderr_truncated = await stderr_task
                event["stdout"] = stdout + ("\n[output truncated]" if stdout_truncated else "")
                event["stderr"] = stderr + ("\n[output truncated]" if stderr_truncated else "")
                event["exit_code"] = process.returncode
                event["elapsed_seconds"] = time.perf_counter() - started
                if event_callback is not None:
                    try:
                        saved = event_callback(event)
                        if inspect.isawaitable(saved):
                            await saved
                    except Exception as error:
                        event["audit_error"] = type(error).__name__
                raise

            stdout, stdout_truncated = await stdout_task
            stderr, stderr_truncated = await stderr_task
            event["stdout"] = stdout + ("\n[output truncated]" if stdout_truncated else "")
            event["stderr"] = stderr + ("\n[output truncated]" if stderr_truncated else "")
            event["exit_code"] = process.returncode
            event["elapsed_seconds"] = time.perf_counter() - started

        audit_warning = ""
        if event_callback is not None:
            try:
                saved = event_callback(event)
                if inspect.isawaitable(saved):
                    await saved
            except Exception as error:
                audit_warning = f"\n[command audit failed: {type(error).__name__}]"
        return _format_result(event) + audit_warning

    return {"run_workspace_command": run_workspace_command}
