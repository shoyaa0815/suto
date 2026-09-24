"""Terminal execution runner with timeout and output capture."""

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TerminalResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False

    def to_text(self) -> str:
        parts = []
        if self.timed_out:
            parts.append("[Timed out]")
        if self.stdout:
            parts.append(f"STDOUT:\n{self.stdout}")
        if self.stderr:
            parts.append(f"STDERR:\n{self.stderr}")
        parts.append(f"Exit code: {self.returncode}")
        return "\n\n".join(parts)


class TerminalRunner:
    """Executes terminal commands with isolation and timeout handling."""

    def __init__(
        self,
        default_cwd: str | Path | None = None,
        default_timeout: int = 30,
    ):
        self.default_cwd = (
            Path(default_cwd).resolve() if default_cwd else Path.cwd()
        )
        self.default_timeout = default_timeout

    def run(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int | None = None,
    ) -> str:
        """Run a command and return formatted execution output."""
        exec_cwd = Path(cwd).resolve() if cwd else self.default_cwd
        if not exec_cwd.is_dir():
            return f"Error: Working directory does not exist: {exec_cwd}"

        timeout_sec = timeout if timeout is not None else self.default_timeout
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(exec_cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_sec,
            )
            result = TerminalResult(
                stdout=proc.stdout,
                stderr=proc.stderr,
                returncode=proc.returncode,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (
                exc.stdout
                if isinstance(exc.stdout, str)
                else (exc.stdout.decode() if exc.stdout else "")
            )
            stderr = (
                exc.stderr
                if isinstance(exc.stderr, str)
                else (exc.stderr.decode() if exc.stderr else "")
            )
            result = TerminalResult(
                stdout=stdout,
                stderr=stderr,
                returncode=-1,
                timed_out=True,
            )
        except Exception as exc:
            return f"Error executing command: {exc}"

        return result.to_text()
