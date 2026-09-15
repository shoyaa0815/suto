import pytest

import application.configuration as configuration
from application.configuration import (
    load_settings,
    save_settings,
    update_profile_setting,
)


def test_yaml_profile_overrides_legacy_environment_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SUTO_TIMEZONE", "UTC")
    monkeypatch.setenv("SUTO_LOCALE", "en")
    monkeypatch.setenv("SUTO_USER_NAME", "Legacy")
    path = tmp_path / "config.yaml"
    path.write_text(
        "version: 1\nprofile:\n  timezone: Asia/Bangkok\n"
        "  locale: th\n  display_name: Shoya\n",
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.profile.timezone == "Asia/Bangkok"
    assert settings.profile.locale == "th"
    assert settings.profile.display_name == "Shoya"


def test_missing_yaml_uses_legacy_environment_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("SUTO_TIMEZONE", "Asia/Bangkok")
    monkeypatch.setenv("SUTO_LOCALE", "th")
    monkeypatch.setenv("SUTO_USER_NAME", "Owner")

    settings = load_settings(tmp_path / "missing.yaml")

    assert settings.profile.timezone == "Asia/Bangkok"
    assert settings.profile.locale == "th"
    assert settings.profile.display_name == "Owner"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("version: 2\n", "config version"),
        ("version: 1\nsecrets:\n  token: nope\n", "unknown config section"),
        (
            "version: 1\nprofile:\n  timezone: Mars/Olympus\n",
            "unknown timezone",
        ),
        ("- not\n- a mapping\n", "config must be a mapping"),
    ],
)
def test_invalid_or_secret_yaml_is_rejected(tmp_path, content, message):
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_settings(path)


def test_save_is_validated_and_reloads_complete_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    settings = load_settings(tmp_path / "missing.yaml")
    settings = update_profile_setting(settings, "timezone", "Asia/Bangkok")
    settings = update_profile_setting(settings, "display_name", "Suto Owner")

    save_settings(settings, path)

    reloaded = load_settings(path)
    assert reloaded == settings
    assert path.stat().st_mode & 0o777 == 0o600


def test_failed_atomic_replace_preserves_previous_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    original = load_settings(tmp_path / "missing.yaml")
    save_settings(original, path)
    previous = path.read_text(encoding="utf-8")
    changed = update_profile_setting(original, "timezone", "Asia/Bangkok")

    def fail_replace(source, destination):
        raise OSError("disk unavailable")

    monkeypatch.setattr(configuration.os, "replace", fail_replace)

    with pytest.raises(OSError, match="disk unavailable"):
        save_settings(changed, path)

    assert path.read_text(encoding="utf-8") == previous
    assert list(tmp_path.glob(".config.yaml.*")) == []
