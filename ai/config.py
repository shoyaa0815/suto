import os


OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen3.5:9b"
MAX_TOOL_ROUNDS = 6
MAX_AGENT_TOOL_ROUNDS = 20
MAX_LANGUAGE_CORRECTIONS = 2
OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "300"))
PROGRESS_INTERVAL_SECONDS = float(
    os.environ.get("PROGRESS_INTERVAL_SECONDS", "10")
)
TEMPERATURE = 0.2

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
