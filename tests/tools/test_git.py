import subprocess
from pathlib import Path
from unittest.mock import patch

from tools.git import (
    GitOperations,
    commit,
    diff,
    git_commit,
    git_diff,
    git_log,
    git_status,
    log,
    status,
)


def _init_git_repo(path: Path):
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "TestUser"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


def test_git_operations_in_repo(tmp_path):
    _init_git_repo(tmp_path)
    git = GitOperations(default_repo_path=tmp_path)

    # Status before files
    st = git.status()
    assert st == "(empty output)"

    # Create file
    f = tmp_path / "hello.txt"
    f.write_text("initial content", encoding="utf-8")

    st = git.status()
    assert "?? hello.txt" in st

    # Add and commit
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True, capture_output=True)
    res_commit = git.commit("initial commit")
    assert "initial commit" in res_commit

    # Modify file and diff
    f.write_text("updated content", encoding="utf-8")
    d = git.diff()
    assert "+updated content" in d
    assert "-initial content" in d

    # Log
    l = git.log()
    assert "initial commit" in l


def test_git_nonexistent_directory():
    git = GitOperations(default_repo_path="/nonexistent/path/xyz")
    res = git.status()
    assert "Error: Directory does not exist" in res


def test_git_not_found():
    git = GitOperations()
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        res = git.status()
        assert "executable not found" in res


def test_git_timeout():
    git = GitOperations()
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=30)):
        res = git.diff()
        assert "timed out" in res


def test_git_convenience_functions(tmp_path):
    _init_git_repo(tmp_path)
    assert status(str(tmp_path)) == "(empty output)"
    assert git_status(str(tmp_path)) == "(empty output)"
    assert diff(str(tmp_path)) == "(empty output)"
    assert git_diff(str(tmp_path)) == "(empty output)"
    assert log(str(tmp_path)) == "(empty output)" or "fatal" in log(str(tmp_path)) or "does not have any commits" in log(str(tmp_path))
    assert git_log(str(tmp_path)) == "(empty output)" or "fatal" in git_log(str(tmp_path)) or "does not have any commits" in git_log(str(tmp_path))
