import os

from core.settings import env_float, env_int, env_text


AI_PROVIDER = env_text("AI_PROVIDER", "ollama")
_default_base_url = (
    "https://api.openai.com/v1"
    if AI_PROVIDER.casefold() == "openai"
    else "http://localhost:11434"
)
AI_BASE_URL = env_text(
    "AI_BASE_URL",
    env_text("OLLAMA_URL", _default_base_url),
)
AI_MODEL = env_text("AI_MODEL", env_text("OLLAMA_MODEL", "qwen3.5:9b"))
AI_API_KEY = env_text("AI_API_KEY", "")
MAX_TOOL_ROUNDS = env_int("MAX_TOOL_ROUNDS", 6, minimum=1)
MAX_AGENT_TOOL_ROUNDS = env_int("MAX_AGENT_TOOL_ROUNDS", 20, minimum=1)
MAX_LANGUAGE_CORRECTIONS = env_int("MAX_LANGUAGE_CORRECTIONS", 2, minimum=0)
AI_TIMEOUT_SECONDS = env_int(
    "AI_TIMEOUT_SECONDS",
    env_int("OLLAMA_TIMEOUT_SECONDS", 300, minimum=1),
    minimum=1,
)
PROGRESS_INTERVAL_SECONDS = env_float(
    "PROGRESS_INTERVAL_SECONDS",
    10,
    minimum=0,
)
AI_TEMPERATURE = env_float(
    "AI_TEMPERATURE",
    env_float("OLLAMA_TEMPERATURE", 0.2, minimum=0, maximum=2),
    minimum=0,
    maximum=2,
)

ATTACHMENT_TOOL_NAMES = frozenset(
    {
        "read_attached_file",
        "search_attachment",
        "summarize_attachment",
    }
)

_debug_logs = os.environ.get("SUTO_DEBUG", "").lower() in {"1", "true", "yes"}


def debug(message: str) -> None:
    if _debug_logs:
        print(message)


def set_debug_logs(enabled: bool) -> None:
    """Enable or disable internal timing logs for the current process."""
    global _debug_logs
    _debug_logs = enabled
