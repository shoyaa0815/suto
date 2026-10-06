import os

from application.runtime_configuration import load_runtime_settings
from application.settings import env_text


_runtime = load_runtime_settings()
AI_PROVIDER = _runtime.provider
AI_BASE_URL = _runtime.base_url
AI_MODEL = _runtime.model
AI_API_KEY = env_text("AI_API_KEY", "")
MAX_TOOL_ROUNDS = _runtime.options.max_tool_rounds
MAX_AGENT_TOOL_ROUNDS = _runtime.options.max_agent_tool_rounds
MAX_LANGUAGE_CORRECTIONS = _runtime.options.max_language_corrections
AI_TIMEOUT_SECONDS = _runtime.options.timeout_seconds
PROGRESS_INTERVAL_SECONDS = _runtime.options.progress_interval_seconds
AI_TEMPERATURE = _runtime.options.temperature

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
