from typing import Protocol

import aiohttp


class ProviderTransientError(RuntimeError):
    pass


class ChatProvider(Protocol):
    name: str

    async def chat(
        self,
        session: aiohttp.ClientSession,
        messages: list,
        tool_schemas: list,
        think: bool = False,
        max_output_tokens: int | None = None,
    ) -> dict: ...


def raise_for_provider_status(response) -> None:
    if response.status == 429 or response.status >= 500:
        raise ProviderTransientError(
            f"provider returned transient HTTP status {response.status}"
        )
    response.raise_for_status()
