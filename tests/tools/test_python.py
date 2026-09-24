from unittest.mock import patch
import subprocess

from tools.python import PythonRunner, python_run, run


def test_python_runner_evaluates_code():
    runner = PythonRunner()
    out = runner.run("print(10 + 25)")
    assert "STDOUT:\n35" in out
    assert "Return code: 0" in out


def test_python_runner_captures_stderr():
    runner = PythonRunner()
    out = runner.run("import sys; sys.stderr.write('oops\\n')")
    assert "STDERR:\noops" in out
    assert "Return code: 0" in out


def test_python_runner_captures_exceptions():
    runner = PythonRunner()
    out = runner.run("raise ValueError('something went wrong')")
    assert "ValueError: something went wrong" in out
    assert "Return code: 1" in out


def test_python_runner_timeout():
    runner = PythonRunner(default_timeout=1)
    out = runner.run("import time; time.sleep(5)", timeout=1)
    assert "[Execution timed out]" in out
    assert "Return code: -1" in out


def test_python_runner_handles_generic_exception():
    runner = PythonRunner()
    with patch("subprocess.run", side_effect=OSError("Exec error")):
        out = runner.run("print(1)")
        assert "Error executing Python code: Exec error" in out


def test_python_convenience_functions():
    assert "STDOUT:\n42" in run("print(42)")
    assert "STDOUT:\n100" in python_run("print(100)")
