import re
from collections.abc import Mapping, Sequence


REDACTED = "[REDACTED]"

_NAMED_SECRET = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|passwd|"
    r"secret|token|client[_-]?secret)\b\s*[:=]\s*)([\"']?)([^\s,;&\"']+)(\2)"
)
_BEARER_TOKEN = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+")
_OPENAI_STYLE_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "authorization",
        "password",
        "passwd",
        "secret",
        "client_secret",
    }
)


def redact_text(value: object) -> str:
    text = str(value)
    text = _BEARER_TOKEN.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    text = _NAMED_SECRET.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    return _OPENAI_STYLE_KEY.sub(REDACTED, text)


def redact_value(value):
    if isinstance(value, Mapping):
        redacted = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            redacted[key] = REDACTED if normalized in _SECRET_KEYS else redact_value(item)
        return redacted
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
