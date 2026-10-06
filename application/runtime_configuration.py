"""Non-secret host runtime configuration, independent of interactive settings."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class RuntimeOptions:
    temperature: float = 0.2
    timeout_seconds: int = 300
    max_tool_rounds: int = 6
    max_agent_tool_rounds: int = 20
    max_language_corrections: int = 2
    progress_interval_seconds: float = 10
    approval_ttl_seconds: int = 600


@dataclass(frozen=True)
class RuntimeLimits:
    max_elapsed_seconds: float = 900
    max_tokens: int = 100_000
    max_tool_calls: int = 40
    max_changed_files: int = 10
    repeated_tool_call_limit: int = 3


@dataclass(frozen=True)
class RuntimeSettings:
    provider: str = "ollama"
    model: str = "qwen3.5:9b"
    base_url: str = "http://localhost:11434"
    timezone: str = "UTC"
    workspace: str = "."
    options: RuntimeOptions = field(default_factory=RuntimeOptions)
    limits: RuntimeLimits = field(default_factory=RuntimeLimits)


def _mapping(value: object, label: str, keys: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    if value.keys() - keys:
        raise ValueError(f"unknown {label} setting")
    return value


def _text(value: object, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _value(values: dict, key: str, default: object, *environment: str) -> object:
    if key in values:
        return values[key]
    for name in environment:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _number(values: dict, key: str, default: int | float, environment: tuple[str, ...],
            *, integer: bool = True, minimum: float = 1, maximum: float | None = None) -> int | float:
    value = _value(values, key, default, *environment)
    # YAML is typed; only environment values may be converted from text.
    types = {int} if integer else {int, float}
    if key in values and type(value) not in types:
        raise ValueError(f"runtime.{key} must be {'an integer' if integer else 'a number'}")
    try:
        number = int(value) if integer else float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"runtime.{key} must be {'an integer' if integer else 'a number'}") from None
    if (isinstance(number, float) and not math.isfinite(number)
            or number < minimum or maximum is not None and number > maximum):
        raise ValueError(f"runtime.{key} is outside the supported range")
    return number


def parse_runtime_settings(values: object, *, legacy_timezone: object = None) -> RuntimeSettings:
    values = _mapping(values, "runtime", set(RuntimeSettings.__dataclass_fields__))
    provider = _text(_value(values, "provider", "ollama", "AI_PROVIDER"), "runtime.provider", 64)
    provider = provider.casefold().replace("_", "-")
    if provider not in {"ollama", "openai", "openai-compatible"}:
        raise ValueError("runtime.provider must be ollama, openai, or openai-compatible")
    model = _text(_value(values, "model", "qwen3.5:9b", "AI_MODEL", "OLLAMA_MODEL"), "runtime.model", 256)
    default_url = "https://api.openai.com/v1" if provider == "openai" else "http://localhost:11434"
    base_url = _text(_value(values, "base_url", default_url, "AI_BASE_URL", "OLLAMA_URL"), "runtime.base_url")
    try:
        url = urlsplit(base_url)
        valid = (url.scheme in {"http", "https"} and url.hostname and url.port != 0
                 and not url.username and not url.password and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("runtime.base_url must be an HTTP(S) URL without credentials, query, or fragment")
    timezone = _text(
        values.get("timezone", legacy_timezone if legacy_timezone is not None
                   else _value({}, "timezone", "UTC", "SUTO_TIMEZONE")), "runtime.timezone", 100,
    )
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("runtime.timezone must be a known timezone") from None
    workspace = _text(_value(values, "workspace", ".", "SUTO_WORKSPACE"), "runtime.workspace")
    options = _mapping(values.get("options", {}), "runtime.options", set(RuntimeOptions.__dataclass_fields__))
    limits = _mapping(values.get("limits", {}), "runtime.limits", set(RuntimeLimits.__dataclass_fields__))
    return RuntimeSettings(
        provider=provider, model=model, base_url=base_url, timezone=timezone, workspace=workspace,
        options=RuntimeOptions(
            temperature=_number(options, "temperature", 0.2, ("AI_TEMPERATURE", "OLLAMA_TEMPERATURE"),
                                integer=False, minimum=0, maximum=2),
            timeout_seconds=_number(options, "timeout_seconds", 300, ("AI_TIMEOUT_SECONDS", "OLLAMA_TIMEOUT_SECONDS")),
            max_tool_rounds=_number(options, "max_tool_rounds", 6, ("MAX_TOOL_ROUNDS",)),
            max_agent_tool_rounds=_number(options, "max_agent_tool_rounds", 20, ("MAX_AGENT_TOOL_ROUNDS",)),
            max_language_corrections=_number(options, "max_language_corrections", 2, ("MAX_LANGUAGE_CORRECTIONS",), minimum=0),
            progress_interval_seconds=_number(options, "progress_interval_seconds", 10, ("PROGRESS_INTERVAL_SECONDS",), integer=False, minimum=0),
            approval_ttl_seconds=_number(options, "approval_ttl_seconds", 600, ("APPROVAL_TTL_SECONDS",)),
        ),
        limits=RuntimeLimits(
            max_elapsed_seconds=_number(limits, "max_elapsed_seconds", 900, ("MAX_JOB_SECONDS",), integer=False),
            max_tokens=_number(limits, "max_tokens", 100_000, ("MAX_JOB_TOKENS",)),
            max_tool_calls=_number(limits, "max_tool_calls", 40, ("MAX_TOOL_CALLS",)),
            max_changed_files=_number(limits, "max_changed_files", 10, ("MAX_CHANGED_FILES",)),
            repeated_tool_call_limit=_number(limits, "repeated_tool_call_limit", 3, ("REPEATED_TOOL_CALL_LIMIT",), minimum=2),
        ),
    )


def load_runtime_settings(path: str | Path | None = None) -> RuntimeSettings:
    """Load only runtime fields; no profile, voice, session, or database startup."""
    from application.configuration import read_configuration

    raw = read_configuration(path)
    profile = raw.get("profile")
    legacy_timezone = profile.get("timezone") if isinstance(profile, dict) else None
    return parse_runtime_settings(raw.get("runtime", {}), legacy_timezone=legacy_timezone)
