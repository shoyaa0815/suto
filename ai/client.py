import aiohttp

from .providers import build_provider


async def chat(
    session: aiohttp.ClientSession,
    messages: list,
    tool_schemas: list,
    think: bool = False,
    max_output_tokens: int | None = None,
) -> dict:
    provider = build_provider()
    return await provider.chat(
        session,
        messages,
        tool_schemas,
        think=think,
        max_output_tokens=max_output_tokens,
    )
