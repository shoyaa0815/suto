import math
import os


def env_text(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from error
    _validate_range(name, value, minimum, maximum)
    return value


def env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number, got {raw!r}") from error
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {raw!r}")
    _validate_range(name, value, minimum, maximum)
    return value


def _validate_range(
    name: str,
    value: int | float,
    minimum: int | float | None,
    maximum: int | float | None,
) -> None:
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}, got {value}")
