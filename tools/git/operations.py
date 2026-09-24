"""Git operations interface."""

import subprocess
from pathlib import Path


class GitOperations:
    """Git CLI wrapper providing status, diff, log, and commit operations."""

    def __init__(self, default_repo_path: str | Path | None = None):
        self.default_repo_path = (
            Path(default_repo_path).resolve()
            if default_repo_path
            else Path.cwd()
        )

    def _run_git(
        self,
        args: list[str],
        repo_path: str | None = None,
    ) -> str:
        cwd = Path(repo_path).resolve() if repo_path else self.default_repo_path
        if not cwd.is_dir():
            return f"Error: Directory does not exist: {cwd}"
        try:
            res = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            if res.returncode != 0:
                err = res.stderr.strip() or res.stdout.strip()
                return f"Git error (exit {res.returncode}): {err}"
            return res.stdout if res.stdout else "(empty output)"
        except FileNotFoundError:
            return "Error: 'git' executable not found on host"
        except subprocess.TimeoutExpired:
            return "Error: git command timed out"
        except Exception as exc:
            return f"Error executing git command: {exc}"

    def diff(
        self,
        repo_path: str | None = None,
        cached: bool = False,
        file_path: str | None = None,
    ) -> str:
        """Inspect git diff."""
        args = ["diff"]
        if cached:
            args.append("--cached")
        if file_path:
            args.extend(["--", file_path])
        return self._run_git(args, repo_path=repo_path)

    def status(self, repo_path: str | None = None) -> str:
        """Inspect git status in short format."""
        return self._run_git(["status", "--short"], repo_path=repo_path)

    def log(
        self,
        repo_path: str | None = None,
        max_count: int = 10,
    ) -> str:
        """Inspect git commit history."""
        return self._run_git(
            ["log", f"-n{max_count}", "--oneline"],
            repo_path=repo_path,
        )

    def commit(
        self,
        message: str,
        repo_path: str | None = None,
        all: bool = False,
    ) -> str:
        """Create a git commit."""
        args = ["commit"]
        if all:
            args.append("-a")
        args.extend(["-m", message])
        return self._run_git(args, repo_path=repo_path)
