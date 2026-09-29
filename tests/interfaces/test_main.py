import pytest

import main as main_module
from main import _parse_args


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], ("cli", "agent")),
        (["settings"], ("settings_web", "settings")),
        (["api"], ("api", "agent")),
        (["tui"], ("tui", "agent")),
    ],
)
def test_parse_args_accepts_public_entrypoints(args, expected):
    assert _parse_args(args) == expected


@pytest.mark.parametrize(
    "args",
    [["chat", "cli"], ["agent", "cli"], ["home"], ["web"], ["settings", "extra"]],
)
def test_parse_args_rejects_removed_entrypoints(args):
    with pytest.raises(SystemExit, match=r"usage: python3 main.py \[settings\|api\|tui\]"):
        _parse_args(args)


def test_main_starts_the_cli_in_personal_assistant_mode(monkeypatch):
    import interfaces.cli as cli

    calls = []
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])
    monkeypatch.setattr(cli, "run", calls.append)

    main_module.main()

    assert calls == ["agent"]


def test_main_dispatches_settings_to_local_web_editor(monkeypatch):
    import interfaces.web as web

    calls = []
    monkeypatch.setattr(main_module.sys, "argv", ["main.py", "settings"])
    monkeypatch.setattr(web, "run", calls.append)

    main_module.main()

    assert calls == ["settings"]


def test_main_dispatches_api(monkeypatch):
    import interfaces.api as api

    calls = []
    monkeypatch.setattr(main_module.sys, "argv", ["main.py", "api"])
    monkeypatch.setattr(api, "run", calls.append)

    main_module.main()

    assert calls == ["agent"]


def test_main_dispatches_tui(monkeypatch):
    import interfaces.tui as tui

    calls = []
    monkeypatch.setattr(main_module.sys, "argv", ["main.py", "tui"])
    monkeypatch.setattr(tui, "run", calls.append)

    main_module.main()

    assert calls == ["agent"]


def test_mcp_startup_diagnostic_names_trust_boundary_without_secrets(monkeypatch, capsys):
    import interfaces.cli as cli

    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])
    monkeypatch.setattr(cli, "run", lambda mode: None)
    monkeypatch.setenv("SUTO_MCP_CONFIG", "/private/mcp.yaml")
    main_module.main()
    warning = capsys.readouterr().err
    assert "trusted local executables" in warning
    assert "/private/mcp.yaml" not in warning
