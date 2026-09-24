"""Git tool module exposing schemas and operations."""

from typing import Any

from .operations import GitOperations

_default_git = GitOperations()


def diff(
    repo_path: str | None = None,
    cached: bool = False,
    file_path: str | None = None,
) -> str:
    """Inspect git diff."""
    return _default_git.diff(
        repo_path=repo_path,
        cached=cached,
        file_path=file_path,
    )


def status(repo_path: str | None = None) -> str:
    """Inspect git status."""
    return _default_git.status(repo_path=repo_path)


def log(repo_path: str | None = None, max_count: int = 10) -> str:
    """Inspect git log."""
    return _default_git.log(repo_path=repo_path, max_count=max_count)


def commit(
    message: str,
    repo_path: str | None = None,
    all: bool = False,
) -> str:
    """Create a git commit."""
    return _default_git.commit(message=message, repo_path=repo_path, all=all)


git_diff = diff
git_status = status
git_log = log
git_commit = commit

DIFF_SCHEMA = {
    "type": "function",
    "function": {
        "name": "git.diff",
        "description": "Show changes between the working tree and the index/commit.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Optional repository path (defaults to current directory).",
                },
                "cached": {
                    "type": "boolean",
                    "description": "Whether to view staged changes (--cached).",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional specific file path to diff.",
                },
            },
        },
    },
}

DIFF_ALIAS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "git_diff",
        "description": "Show changes between the working tree and the index/commit.",
        "parameters": DIFF_SCHEMA["function"]["parameters"],
    },
}

STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "git.status",
        "description": "Show the working tree status.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Optional repository path (defaults to current directory).",
                },
            },
        },
    },
}

STATUS_ALIAS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "git_status",
        "description": "Show the working tree status.",
        "parameters": STATUS_SCHEMA["function"]["parameters"],
    },
}

LOG_SCHEMA = {
    "type": "function",
    "function": {
        "name": "git.log",
        "description": "Show commit logs.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Optional repository path (defaults to current directory).",
                },
                "max_count": {
                    "type": "integer",
                    "description": "Maximum number of commits to show (default 10).",
                },
            },
        },
    },
}

LOG_ALIAS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "git_log",
        "description": "Show commit logs.",
        "parameters": LOG_SCHEMA["function"]["parameters"],
    },
}

COMMIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "git.commit",
        "description": "Record changes to the repository.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The commit message.",
                },
                "repo_path": {
                    "type": "string",
                    "description": "Optional repository path (defaults to current directory).",
                },
                "all": {
                    "type": "boolean",
                    "description": "Automatically stage modified/deleted files (-a).",
                },
            },
            "required": ["message"],
        },
    },
}

COMMIT_ALIAS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "git_commit",
        "description": "Record changes to the repository.",
        "parameters": COMMIT_SCHEMA["function"]["parameters"],
    },
}

PROMPT = (
    "Use git.diff (or git_diff), git.status (or git_status), git.log (or git_log), "
    "and git.commit (or git_commit) to inspect and manage git repositories."
)

GIT_SCHEMAS = [
    DIFF_SCHEMA,
    DIFF_ALIAS_SCHEMA,
    STATUS_SCHEMA,
    STATUS_ALIAS_SCHEMA,
    LOG_SCHEMA,
    LOG_ALIAS_SCHEMA,
    COMMIT_SCHEMA,
    COMMIT_ALIAS_SCHEMA,
]

GIT_TOOLS: dict[str, Any] = {
    "git.diff": diff,
    "git_diff": diff,
    "git.status": status,
    "git_status": status,
    "git.log": log,
    "git_log": log,
    "git.commit": commit,
    "git_commit": commit,
}

__all__ = [
    "COMMIT_ALIAS_SCHEMA",
    "COMMIT_SCHEMA",
    "DIFF_ALIAS_SCHEMA",
    "DIFF_SCHEMA",
    "GIT_SCHEMAS",
    "GIT_TOOLS",
    "GitOperations",
    "LOG_ALIAS_SCHEMA",
    "LOG_SCHEMA",
    "PROMPT",
    "STATUS_ALIAS_SCHEMA",
    "STATUS_SCHEMA",
    "commit",
    "diff",
    "git_commit",
    "git_diff",
    "git_log",
    "git_status",
    "log",
    "status",
]
