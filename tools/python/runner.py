"""Python runner for executing code snippets in an isolated process."""

import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class PythonResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False

    def to_text(self) -> str:
        parts = []
        if self.timed_out:
            parts.append("[Execution timed out]")
        if self.stdout:
            parts.append(f"STDOUT:\n{self.stdout}")
        if self.stderr:
            parts.append(f"STDERR:\n{self.stderr}")
        parts.append(f"Return code: {self.returncode}")
        return "\n\n".join(parts)


class PythonRunner:
    """Executes Python code in a dedicated subprocess with timeout."""

    def __init__(self, default_timeout: int = 15):
        self.default_timeout = default_timeout

    def run(self, code: str, timeout: int | None = None) -> str:
        """Run Python code and return formatted stdout/stderr."""
        timeout_sec = timeout if timeout is not None else self.default_timeout
        try:
            proc = subprocess.run(
                [sys.executable, "-c", code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_sec,
            )
            result = PythonResult(
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
            result = PythonResult(
                stdout=stdout,
                stderr=stderr,
                returncode=-1,
                timed_out=True,
            )
        except Exception as exc:
            return f"Error executing Python code: {exc}"

        return result.to_text()
