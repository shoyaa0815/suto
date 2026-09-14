import pytest

import interfaces.home_terminal as home_terminal


def test_home_placeholder_runs_silently(capsys):
    assert home_terminal.run("home") is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_plain_terminal_rejects_other_modes():
    with pytest.raises(ValueError, match="only supports home mode"):
        home_terminal.run("agent")
