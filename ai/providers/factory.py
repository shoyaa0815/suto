import aiohttp

from llm.router import ModelRouter

from .. import config

from .base import ChatModelProvider
from .ollama import OllamaProvider
from .openai_compatible import OpenAICompatibleProvider


def build_provider(*, session: aiohttp.ClientSession | None = None) -> ChatModelProvider:
    name = config.AI_PROVIDER.casefold().replace("_", "-")
    if name == "ollama":
        return OllamaProvider(
            config.AI_BASE_URL,
            config.AI_MODEL,
            config.AI_TEMPERATURE,
            session=session,
        )
    if name in {"openai", "openai-compatible"}:
        if name == "openai" and not config.AI_API_KEY:
            raise ValueError("AI_API_KEY is required when AI_PROVIDER=openai")
        return OpenAICompatibleProvider(
            config.AI_BASE_URL,
            config.AI_MODEL,
            config.AI_API_KEY,
            config.AI_TEMPERATURE,
            session=session,
        )
    raise ValueError(
        f"unsupported AI_PROVIDER {config.AI_PROVIDER!r}; "
        "choose ollama, openai, or openai-compatible"
    )


def build_model_router(session: aiohttp.ClientSession) -> ModelRouter:
    """Bind the configured default provider once for an agent run."""
    return ModelRouter(build_provider(session=session))
