"""Validated, non-secret application settings stored in YAML."""

from __future__ import annotations

import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


CONFIG_VERSION = 1
DEFAULT_CONFIG_PATH = Path("config.yaml")
EDITABLE_PROFILE_KEYS = frozenset({"display_name", "locale", "timezone"})


@dataclass(frozen=True)
class ProfileSettings:
    timezone: str
    locale: str
    display_name: str


@dataclass(frozen=True)
class AppSettings:
    version: int
    profile: ProfileSettings


def _text(value: object, label: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    normalized = value.strip()
    if not minimum <= len(normalized) <= maximum:
        raise ValueError(f"{label} must contain {minimum}-{maximum} characters")
    return normalized


def _timezone(value: object) -> str:
    timezone = _text(value, "profile.timezone", 1, 100)
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown timezone: {timezone}") from error
    return timezone


def _profile(values: object) -> ProfileSettings:
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise ValueError("profile must be a mapping")
    unknown = set(values) - EDITABLE_PROFILE_KEYS
    if unknown:
        raise ValueError(f"unknown profile setting: {sorted(unknown)[0]}")
    return ProfileSettings(
        timezone=_timezone(values.get("timezone", os.environ.get("SUTO_TIMEZONE", "UTC"))),
        locale=_text(
            values.get("locale", os.environ.get("SUTO_LOCALE", "th")),
            "profile.locale",
            2,
            16,
        ),
        display_name=_text(
            values.get("display_name", os.environ.get("SUTO_USER_NAME", "User")),
            "profile.display_name",
            1,
            100,
        ),
    )


def load_settings(path: str | Path = DEFAULT_CONFIG_PATH) -> AppSettings:
    """Load YAML settings, falling back to legacy environment defaults."""
    config_path = Path(path)
    if config_path.exists():
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ValueError(f"cannot read {config_path}: {error}") from error
        except yaml.YAMLError as error:
            raise ValueError(f"invalid YAML in {config_path}") from error
    else:
        raw = {}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("config must be a mapping")
    unknown = set(raw) - {"version", "profile"}
    if unknown:
        raise ValueError(f"unknown config section: {sorted(unknown)[0]}")
    version = raw.get("version", CONFIG_VERSION)
    if isinstance(version, bool) or version != CONFIG_VERSION:
        raise ValueError(f"config version must be {CONFIG_VERSION}")
    return AppSettings(version=version, profile=_profile(raw.get("profile")))


def render_settings(settings: AppSettings) -> str:
    return yaml.safe_dump(
        asdict(settings),
        allow_unicode=True,
        sort_keys=False,
    )


def update_profile_setting(
    settings: AppSettings,
    key: str,
    value: str,
) -> AppSettings:
    normalized = key.strip().casefold().removeprefix("profile.")
    if normalized not in EDITABLE_PROFILE_KEYS:
        raise ValueError(f"setting is not editable: {key}")
    values = asdict(settings.profile)
    values[normalized] = value
    return AppSettings(version=CONFIG_VERSION, profile=_profile(values))


def save_settings(
    settings: AppSettings,
    path: str | Path = DEFAULT_CONFIG_PATH,
) -> Path:
    """Atomically replace the non-secret YAML settings file."""
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(
            prefix=f".{config_path.name}.",
            dir=config_path.parent,
            text=True,
        )
        temporary = Path(name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(render_settings(settings))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config_path)
        temporary = None
        directory_fd = os.open(config_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return config_path
