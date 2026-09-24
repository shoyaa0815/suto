import subprocess
from pathlib import Path
from unittest.mock import patch

from tools.terminal import TerminalRunner, run, terminal_run


def test_terminal_runner_runs_command(tmp_path):
    runner = TerminalRunner(default_cwd=tmp_path)
    output = runner.run("echo 'hello world'")
    assert "STDOUT:\nhello world" in output
    assert "Exit code: 0" in output


def test_terminal_runner_handles_nonexistent_cwd():
    runner = TerminalRunner()
    output = runner.run("echo 'hi'", cwd="/nonexistent/directory/path/that/does/not/exist")
    assert "Error: Working directory does not exist" in output


def test_terminal_runner_handles_timeout(tmp_path):
    runner = TerminalRunner(default_cwd=tmp_path, default_timeout=1)
    output = runner.run("python3 -c 'import time; time.sleep(5)'", timeout=1)
    assert "[Timed out]" in output
    assert "Exit code: -1" in output


def test_terminal_runner_handles_generic_exception():
    runner = TerminalRunner()
    with patch("subprocess.run", side_effect=OSError("Permission denied")):
        output = runner.run("ls")
        assert "Error executing command: Permission denied" in output


def test_terminal_convenience_functions():
    res1 = run("echo test_run")
    assert "test_run" in res1
    res2 = terminal_run("echo test_terminal_run")
    assert "test_terminal_run" in res2
