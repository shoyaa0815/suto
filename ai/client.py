import time

import aiohttp

from . import config


async def chat(
    session: aiohttp.ClientSession,
    messages: list,
    tool_schemas: list,
    think: bool = False,
    max_output_tokens: int | None = None,
) -> dict:
    started = time.perf_counter()
    options = {"temperature": config.TEMPERATURE}
    if max_output_tokens is not None:
        options["num_predict"] = max_output_tokens
    payload = {
        "model": config.OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "think": think,
        "options": options,
    }
    if tool_schemas:
        payload["tools"] = tool_schemas

    async with session.post(config.OLLAMA_URL, json=payload) as response:
        response.raise_for_status()
        data = await response.json()
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    config.debug(
        f"[timing] event=ollama_chat ms={elapsed_ms} "
        f"prompt_tokens={data.get('prompt_eval_count', 'unknown')} "
        f"output_tokens={data.get('eval_count', 'unknown')} "
        f"tools={len(tool_schemas)}"
    )
    return data
