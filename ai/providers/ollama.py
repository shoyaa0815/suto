import time

import aiohttp
from llm.types import ModelRequest, ModelResponse

from .. import config

from .base import model_response, raise_for_provider_status


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, model: str, temperature: float, *, session: aiohttp.ClientSession | None = None) -> None:
        url = base_url.rstrip("/")
        self.url = url if url.endswith("/api/chat") else f"{url}/api/chat"
        self.model = model
        self.temperature = temperature
        self.session = session

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if self.session is None:
            raise RuntimeError("OllamaProvider requires a session for generate")
        data = await self.chat(
            self.session, request.messages, request.available_tools,
            think=bool(request.generation_options.get("think", False)),
            max_output_tokens=request.generation_options.get("max_output_tokens"),
        )
        reason = "tool_calls" if (data.get("message") or {}).get("tool_calls") else data.get("done_reason")
        return model_response({**data, "finish_reason": reason})

    async def chat(
        self,
        session: aiohttp.ClientSession,
        messages: list,
        tool_schemas: list,
        think: bool = False,
        max_output_tokens: int | None = None,
    ) -> dict:
        started = time.perf_counter()
        options = {"temperature": self.temperature}
        if max_output_tokens is not None:
            options["num_predict"] = max_output_tokens
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": think,
            "options": options,
        }
        if tool_schemas:
            payload["tools"] = tool_schemas

        async with session.post(self.url, json=payload) as response:
            raise_for_provider_status(response)
            data = await response.json()
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        config.debug(
            f"[timing] event=provider_chat provider={self.name} ms={elapsed_ms} "
            f"prompt_tokens={data.get('prompt_eval_count', 'unknown')} "
            f"output_tokens={data.get('eval_count', 'unknown')} "
            f"tools={len(tool_schemas)}"
        )
        return data
