"""Validated, non-secret application settings stored in YAML."""

from __future__ import annotations

import os
import tempfile
from dataclasses import asdict, dataclass, field
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
class VoiceSettings:
    input_provider: str = "alsa"
    stt_provider: str = "none"
    tts_provider: str = "none"
    output_provider: str = "alsa"
    input_device: str | None = None
    output_device: str | None = None
    input_path: str | None = None
    output_path: str | None = None
    capture_seconds: int = 10


@dataclass(frozen=True)
class AppSettings:
    version: int
    profile: ProfileSettings
    voice: VoiceSettings = field(default_factory=VoiceSettings)


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


def _optional_text(value: object, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, label, 1, maximum)


def _voice(values: object) -> VoiceSettings:
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise ValueError("voice must be a mapping")
    unknown = set(values) - set(VoiceSettings.__dataclass_fields__)
    if unknown:
        raise ValueError(f"unknown voice setting: {sorted(unknown)[0]}")
    input_provider = values.get("input_provider", "alsa")
    output_provider = values.get("output_provider", "alsa")
    stt_provider = values.get("stt_provider", "none")
    tts_provider = values.get("tts_provider", "none")
    if (not isinstance(input_provider, str) or input_provider not in {"alsa", "file"}
            or not isinstance(output_provider, str) or output_provider not in {"alsa", "file"}):
        raise ValueError("voice audio provider must be alsa or file")
    if (not isinstance(stt_provider, str) or stt_provider not in {"none", "openai"}
            or not isinstance(tts_provider, str) or tts_provider not in {"none", "openai"}):
        raise ValueError("voice speech provider must be none or openai")
    capture_seconds = values.get("capture_seconds", 10)
    if type(capture_seconds) is not int or not 1 <= capture_seconds <= 30:
        raise ValueError("voice.capture_seconds must be 1-30")
    input_path = _optional_text(values.get("input_path"), "voice.input_path", 4096)
    output_path = _optional_text(values.get("output_path"), "voice.output_path", 4096)
    if input_provider == "file" and (input_path is None or not Path(input_path).is_absolute()):
        raise ValueError("voice.input_path must be absolute for file input")
    if output_provider == "file" and (output_path is None or not Path(output_path).is_absolute()):
        raise ValueError("voice.output_path must be absolute for file output")
    return VoiceSettings(
        input_provider=input_provider, stt_provider=stt_provider,
        tts_provider=tts_provider, output_provider=output_provider,
        input_device=_optional_text(values.get("input_device"), "voice.input_device", 128),
        output_device=_optional_text(values.get("output_device"), "voice.output_device", 128),
        input_path=input_path, output_path=output_path, capture_seconds=capture_seconds,
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
    return parse_settings(raw)


def parse_settings(raw: object) -> AppSettings:
    """Validate decoded configuration using the same rules as file loading."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("config must be a mapping")
    unknown = set(raw) - {"version", "profile", "voice"}
    if unknown:
        raise ValueError(f"unknown config section: {sorted(unknown)[0]}")
    version = raw.get("version", CONFIG_VERSION)
    if isinstance(version, bool) or version != CONFIG_VERSION:
        raise ValueError(f"config version must be {CONFIG_VERSION}")
    return AppSettings(version=version, profile=_profile(raw.get("profile")),
                       voice=_voice(raw.get("voice")))


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
    return AppSettings(version=CONFIG_VERSION, profile=_profile(values), voice=settings.voice)


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
