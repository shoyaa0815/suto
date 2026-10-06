import pytest

import main as main_module
from main import _parse_args


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], ("cli", "agent")),
        (["setting"], ("setting", "agent")),
        (["api"], ("api", "agent")),
        (["voice"], ("voice", "agent")),
        (["worker"], ("worker", "agent")),
    ],
)
def test_parse_args_accepts_public_entrypoints(args, expected):
    assert _parse_args(args) == expected


@pytest.mark.parametrize(
    "args",
    [["chat", "cli"], ["agent", "cli"], ["home"], ["settings"], ["web"], ["setting", "extra"], ["worker", "extra"]],
)
def test_parse_args_rejects_removed_entrypoints(args):
    with pytest.raises(SystemExit, match=r"usage: python3 main.py \[setting\|api\|voice\|worker\]"):
        _parse_args(args)


def test_main_starts_the_cli_in_personal_assistant_mode(monkeypatch):
    import interfaces.cli as cli

    calls = []
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])
    monkeypatch.setattr(cli, "run", calls.append)

    main_module.main()

    assert calls == ["agent"]


def test_main_dispatches_settings_interface(monkeypatch):
    import interfaces.web as web

    calls = []
    monkeypatch.setattr(main_module.sys, "argv", ["main.py", "setting"])
    monkeypatch.setattr(web, "run", calls.append)

    main_module.main()

    assert calls == ["agent"]


def test_main_dispatches_api(monkeypatch):
    import interfaces.api as api

    calls = []
    monkeypatch.setattr(main_module.sys, "argv", ["main.py", "api"])
    monkeypatch.setattr(api, "run", calls.append)

    main_module.main()

    assert calls == ["agent"]


def test_main_dispatches_voice(monkeypatch):
    import interfaces.voice as voice

    calls = []
    monkeypatch.setattr(main_module.sys, "argv", ["main.py", "voice"])
    monkeypatch.setattr(voice, "run", calls.append)
    main_module.main()
    assert calls == ["agent"]


def test_main_dispatches_worker(monkeypatch):
    from application import worker

    calls = []
    monkeypatch.setattr(main_module.sys, "argv", ["main.py", "worker"])
    monkeypatch.setattr(worker, "run", calls.append)
    monkeypatch.setenv("SUTO_MCP_CONFIG", "")
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


def test_setting_mcp_startup_diagnostic_does_not_expose_config_path(monkeypatch, capsys):
    import interfaces.web as web

    monkeypatch.setattr(main_module.sys, "argv", ["main.py", "setting"])
    monkeypatch.setattr(web, "run", lambda mode: None)
    monkeypatch.setenv("SUTO_MCP_CONFIG", "/private/mcp.yaml")
    main_module.main()
    warning = capsys.readouterr().err
    assert "trusted local executables" in warning
    assert "/private/mcp.yaml" not in warning
