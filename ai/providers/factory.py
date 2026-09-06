from .. import config

from .base import ChatProvider
from .ollama import OllamaProvider
from .openai_compatible import OpenAICompatibleProvider


def build_provider() -> ChatProvider:
    name = config.AI_PROVIDER.casefold().replace("_", "-")
    if name == "ollama":
        return OllamaProvider(
            config.AI_BASE_URL,
            config.AI_MODEL,
            config.AI_TEMPERATURE,
        )
    if name in {"openai", "openai-compatible"}:
        if name == "openai" and not config.AI_API_KEY:
            raise ValueError("AI_API_KEY is required when AI_PROVIDER=openai")
        return OpenAICompatibleProvider(
            config.AI_BASE_URL,
            config.AI_MODEL,
            config.AI_API_KEY,
            config.AI_TEMPERATURE,
        )
    raise ValueError(
        f"unsupported AI_PROVIDER {config.AI_PROVIDER!r}; "
        "choose ollama, openai, or openai-compatible"
    )
