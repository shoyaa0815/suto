import json
import time

import aiohttp

from .. import config

from .base import raise_for_provider_status


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        temperature: float,
    ) -> None:
        url = base_url.rstrip("/")
        self.url = (
            url if url.endswith("/chat/completions") else f"{url}/chat/completions"
        )
        self.model = model
        self.api_key = api_key
        self.temperature = temperature

    @staticmethod
    def _request_messages(messages: list) -> list:
        prepared = []
        for message in messages:
            copied = dict(message)
            if copied.get("role") == "assistant" and copied.get("tool_calls"):
                calls = []
                for call in copied["tool_calls"]:
                    normalized = dict(call)
                    function = dict(normalized.get("function") or {})
                    arguments = function.get("arguments")
                    if not isinstance(arguments, str):
                        function["arguments"] = json.dumps(
                            arguments or {},
                            ensure_ascii=False,
                        )
                    normalized["function"] = function
                    calls.append(normalized)
                copied["tool_calls"] = calls
            prepared.append(copied)
        return prepared

    @staticmethod
    def _response_message(message: dict) -> dict:
        normalized = {
            "role": message.get("role", "assistant"),
            "content": message.get("content") or "",
        }
        tool_calls = []
        for call in message.get("tool_calls") or []:
            function = dict(call.get("function") or {})
            arguments = function.get("arguments") or "{}"
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        "provider returned invalid JSON tool arguments"
                    ) from error
            function["arguments"] = arguments
            tool_calls.append(
                {
                    "id": call.get("id"),
                    "type": call.get("type", "function"),
                    "function": function,
                }
            )
        if tool_calls:
            normalized["tool_calls"] = tool_calls
        return normalized

    async def chat(
        self,
        session: aiohttp.ClientSession,
        messages: list,
        tool_schemas: list,
        think: bool = False,
        max_output_tokens: int | None = None,
    ) -> dict:
        started = time.perf_counter()
        payload = {
            "model": self.model,
            "messages": self._request_messages(messages),
            "stream": False,
            "temperature": self.temperature,
        }
        if tool_schemas:
            payload["tools"] = tool_schemas
        if max_output_tokens is not None:
            payload["max_tokens"] = max_output_tokens
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with session.post(self.url, json=payload, headers=headers) as response:
            raise_for_provider_status(response)
            data = await response.json()
        choices = data.get("choices") or []
        if not choices:
            raise ValueError("provider response did not contain a chat choice")
        usage = data.get("usage") or {}
        normalized = {
            "message": self._response_message(choices[0].get("message") or {}),
            "prompt_eval_count": int(usage.get("prompt_tokens") or 0),
            "eval_count": int(usage.get("completion_tokens") or 0),
        }
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        config.debug(
            f"[timing] event=provider_chat provider={self.name} ms={elapsed_ms} "
            f"prompt_tokens={normalized['prompt_eval_count']} "
            f"output_tokens={normalized['eval_count']} tools={len(tool_schemas)}"
        )
        return normalized
