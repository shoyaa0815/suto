"""Runtime configuration loading and compatibility at the host boundary."""

from dataclasses import asdict
import traceback

import pytest
import yaml

from application.configuration import load_settings, save_settings, update_profile_setting
from application.runtime_configuration import RuntimeSettings, load_runtime_settings
from interfaces.web.settings import SettingsEditor


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (
        "SUTO_CONFIG_PATH", "SUTO_TIMEZONE", "SUTO_WORKSPACE", "AI_PROVIDER", "AI_MODEL",
        "AI_BASE_URL", "AI_TEMPERATURE", "AI_TIMEOUT_SECONDS", "OLLAMA_MODEL", "OLLAMA_URL",
        "OLLAMA_TEMPERATURE", "OLLAMA_TIMEOUT_SECONDS", "MAX_TOOL_ROUNDS", "MAX_AGENT_TOOL_ROUNDS",
        "MAX_LANGUAGE_CORRECTIONS", "PROGRESS_INTERVAL_SECONDS", "APPROVAL_TTL_SECONDS",
        "MAX_JOB_SECONDS", "MAX_JOB_TOKENS", "MAX_TOOL_CALLS", "MAX_CHANGED_FILES", "REPEATED_TOOL_CALL_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)


def write_config(tmp_path, raw):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_missing_empty_and_partial_runtime_use_defaults(tmp_path):
    assert load_runtime_settings() == RuntimeSettings()
    path = write_config(tmp_path, None)
    assert load_runtime_settings(path) == RuntimeSettings()
    path = write_config(tmp_path, {"runtime": {"model": "local-model"}})
    assert load_runtime_settings(path) == RuntimeSettings(model="local-model")


def test_legacy_environment_and_profile_timezone_remain_supported(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("OLLAMA_MODEL", "legacy-model")
    monkeypatch.setenv("SUTO_TIMEZONE", "UTC")
    monkeypatch.setenv("SUTO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OLLAMA_TEMPERATURE", "0.7")
    monkeypatch.setenv("OLLAMA_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("MAX_JOB_TOKENS", "1234")
    settings = load_runtime_settings(write_config(tmp_path, {"profile": {"timezone": "Asia/Bangkok"}}))
    assert settings.provider == "openai-compatible"
    assert settings.model == "legacy-model"
    assert settings.base_url == "http://localhost:1234/v1"
    assert settings.timezone == "Asia/Bangkok"
    assert settings.workspace == str(tmp_path)
    assert settings.options.temperature == 0.7
    assert settings.options.timeout_seconds == 60
    assert settings.limits.max_tokens == 1234
    monkeypatch.setenv("AI_MODEL", "primary-model")
    monkeypatch.setenv("AI_BASE_URL", "https://provider.test/v1")
    assert load_runtime_settings().model == "primary-model"
    assert load_runtime_settings().base_url == "https://provider.test/v1"


def test_yaml_overrides_environment_without_validating_personal_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_MODEL", "environment-model")
    monkeypatch.setenv("AI_TIMEOUT_SECONDS", "malformed-but-overridden")
    monkeypatch.setenv("SUTO_TIMEZONE", "Invalid/Zone")
    path = write_config(tmp_path, {
        "runtime": {"provider": "openai", "model": "yaml-model", "timezone": "UTC",
                    "workspace": str(tmp_path), "options": {"timeout_seconds": 20}},
        "profile": {"timezone": "Invalid/Zone", "locale": []},
        "voice": {"input_provider": "file"},
    })
    settings = load_runtime_settings(path)
    assert settings.model == "yaml-model"
    assert settings.base_url == "https://api.openai.com/v1"
    assert settings.timezone == "UTC"
    assert settings.options.timeout_seconds == 20
    with pytest.raises(ValueError):
        load_settings(path)


def test_selected_config_path_is_shared_and_missing_selection_fails(tmp_path, monkeypatch):
    path = tmp_path / "host.yaml"
    path.write_text("runtime:\n  model: selected\nprofile:\n  timezone: Asia/Bangkok\n")
    monkeypatch.setenv("SUTO_CONFIG_PATH", str(path))
    assert load_runtime_settings().model == "selected"
    assert load_settings().runtime.model == "selected"
    assert load_runtime_settings().timezone == "Asia/Bangkok"
    monkeypatch.setenv("SUTO_CONFIG_PATH", str(tmp_path / "absent.yaml"))
    for loader in (load_runtime_settings, load_settings):
        with pytest.raises(ValueError, match="SUTO_CONFIG_PATH"):
            loader()


@pytest.mark.parametrize("runtime", [
    None, [], "text", {"unknown": 1}, {"api_key": "private-token"},
    {"provider": "unsupported"}, {"provider": []}, {"model": ""}, {"model": 5},
    {"timezone": "Invalid/Zone"}, {"timezone": "/UTC"}, {"timezone": True},
    {"workspace": ""}, {"workspace": []}, {"workspace": "a\x00b"},
    {"base_url": "file:///tmp/provider"}, {"base_url": "https://user:private-token@provider.test"},
    {"base_url": "https://provider.test?key=private-token"}, {"base_url": "http://provider.test:bad"},
    {"options": None}, {"options": {"sandbox": "process"}}, {"options": {"temperature": float("nan")}},
    {"options": {"temperature": 2.1}}, {"options": {"timeout_seconds": True}},
    {"options": {"timeout_seconds": "20"}}, {"options": {"max_agent_tool_rounds": 0}},
    {"options": {"progress_interval_seconds": -1}}, {"limits": []},
    {"limits": {"max_tokens": 0}}, {"limits": {"max_tool_calls": 1.5}},
    {"limits": {"max_elapsed_seconds": float("inf")}}, {"limits": {"repeated_tool_call_limit": 1}},
])
def test_malformed_runtime_fails_without_echoing_secrets(tmp_path, runtime):
    path = write_config(tmp_path, {"runtime": runtime})
    with pytest.raises(ValueError) as error:
        load_runtime_settings(path)
    assert "private-token" not in str(error.value)


@pytest.mark.parametrize("content", ["runtime: [", "- runtime", "version: 2", "secret: value", "1: bad\nsecret: value"])
def test_bad_configuration_document_fails_closed(tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content)
    with pytest.raises(ValueError):
        load_runtime_settings(path)


def test_yaml_parser_traceback_does_not_expose_private_content(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("runtime: [private-token\n")
    with pytest.raises(ValueError) as error:
        load_runtime_settings(path)
    assert "private-token" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("name,value", [
    ("AI_PROVIDER", "unsupported"), ("AI_TIMEOUT_SECONDS", "1.5"),
    ("AI_TEMPERATURE", "nan"), ("MAX_TOOL_ROUNDS", "0"), ("MAX_JOB_TOKENS", "bad"),
    ("SUTO_TIMEZONE", "Invalid/Zone"),
])
def test_malformed_environment_fails_closed(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        load_runtime_settings()


def test_profile_updates_and_dashboard_preserve_runtime_and_keep_secrets_out(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "private-token")
    path = write_config(tmp_path, {
        "runtime": {"model": "configured-model", "timezone": "UTC",
                    "options": {"timeout_seconds": 45}, "limits": {"max_tokens": 500}},
    })
    original = load_settings(path).runtime
    save_settings(update_profile_setting(load_settings(path), "timezone", "Asia/Bangkok"), path)
    assert load_settings(path).runtime == original
    editor = SettingsEditor(path)
    snapshot = editor.profile_snapshot()
    editor.update_profile({**snapshot["profile"], "display_name": "New name"}, snapshot["revision"], save=True)
    assert load_runtime_settings(path) == original
    assert asdict(load_settings(path).runtime) == asdict(original)
    assert "private-token" not in editor.snapshot()["yaml"]
    assert "api_key" not in path.read_text()


def test_legacy_profile_save_does_not_materialize_runtime_defaults(tmp_path):
    path = write_config(tmp_path, {"profile": {"timezone": "Asia/Bangkok"}})
    save_settings(update_profile_setting(load_settings(path), "display_name", "Owner"), path)
    assert "runtime" not in yaml.safe_load(path.read_text())
    assert load_runtime_settings(path).timezone == "Asia/Bangkok"
