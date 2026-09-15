import pytest

import main as main_module
from main import _parse_args


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["chat", "cli"], ("cli", "chat")),
        (["agent", "cli"], ("cli", "agent")),
        (["home"], ("home_terminal", "home")),
        (["settings"], ("settings_terminal", "settings")),
        (["chat", "discord"], ("discord", "chat")),
        (["agent", "discord"], ("discord", "agent")),
        (["chat", "line"], ("line", "chat")),
        (["agent", "line"], ("line", "agent")),
    ],
)
def test_parse_args_accepts_interface_and_mode(args, expected):
    assert _parse_args(args) == expected


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["discord"],
        ["secret", "discord"],
        ["developer", "cli"],
        ["home", "cli"],
        ["home", "discord"],
        ["home", "line"],
        ["chat"],
        ["chat", "unknown"],
        ["chat", "tui"],
        ["chat", "discord", "extra"],
    ],
)
def test_parse_args_rejects_invalid_arguments(args):
    with pytest.raises(SystemExit, match="usage:"):
        _parse_args(args)


def test_main_dispatches_home_to_plain_terminal(monkeypatch):
    import interfaces.home_terminal as home_terminal

    calls = []
    monkeypatch.setattr(main_module.sys, "argv", ["main.py", "home"])
    monkeypatch.setattr(home_terminal, "run", calls.append)

    main_module.main()

    assert calls == ["home"]


def test_main_dispatches_settings_to_plain_terminal(monkeypatch):
    import interfaces.settings_terminal as settings_terminal

    calls = []
    monkeypatch.setattr(main_module.sys, "argv", ["main.py", "settings"])
    monkeypatch.setattr(settings_terminal, "run", calls.append)

    main_module.main()

    assert calls == ["settings"]
