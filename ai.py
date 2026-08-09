import inspect

import aiohttp

from db import get_history, save_message
from harness import TOOLS, TOOL_GUIDANCE, TOOL_SCHEMAS

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen3.5:9b"
MAX_TOOL_ROUNDS = 6

# SYSTEM_PROMPT has two layers:
#   1. This base template: identity, answer style, and rules that apply
#      no matter which tool is involved (never fabricate, only use what
#      a tool actually returned). It does NOT know about any specific tool.
#   2. TOOL_GUIDANCE (imported from harness/): the per-tool "when to call
#      this" rules, each one written and maintained next to the tool it
#      governs. See harness/__init__.py for how those get combined.
# The two are stitched together below so adding/removing a tool in
# harness/ automatically updates what the AI is told, without touching
# this file.
_BASE_PROMPT = """You are a concise, accurate assistant in a Discord chat.

Answer style:
- Be direct. No greetings, no filler, no restating the question.
- Keep answers as short as fully answering the question allows.

Tool use:
{tool_guidance}
- Never guess or make up facts, names, numbers, or links. If you're not sure
  and can't search, say so plainly instead of fabricating an answer.
- After searching, base your answer only on what the results actually say.
- If a message contains a "[attached file: ...]" section, the text under it
  is already the file's full extracted content. Read and answer from it
  directly — never call a tool to try to fetch or re-read that file.

Reply in the same language the user wrote in."""

SYSTEM_PROMPT = _BASE_PROMPT.format(tool_guidance=TOOL_GUIDANCE)


async def _chat(session: aiohttp.ClientSession, messages: list) -> dict:
    async with session.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "stream": False,
        },
    ) as response:
        response.raise_for_status()
        return await response.json()


async def ask_local_ai(channel_id: str, prompt: str) -> str:
    timeout = aiohttp.ClientTimeout(total=120)
    messages = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + get_history(channel_id)
        + [{"role": "user", "content": prompt}]
    )
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for _ in range(MAX_TOOL_ROUNDS):
                data = await _chat(session, messages)
                message = data.get("message", {})
                tool_calls = message.get("tool_calls")

                if not tool_calls:
                    answer = message.get("content", "").strip() or "no response from ai"
                    save_message(channel_id, "user", prompt)
                    save_message(channel_id, "assistant", answer)
                    return answer

                messages.append(message)
                for call in tool_calls:
                    name = call["function"]["name"]
                    args = call["function"].get("arguments") or {}
                    if name in TOOLS:
                        func = TOOLS[name]
                        result = (
                            await func(**args)
                            if inspect.iscoroutinefunction(func)
                            else func(**args)
                        )
                    else:
                        result = f"unknown tool: {name}"
                    messages.append({"role": "tool", "content": str(result)})

            return "no response from ai"
    except aiohttp.ClientConnectorError as e:
        print(f"[ai] connection failed: {e!r}")
        return "can't connect check ai server"
    except Exception as e:
        print(f"[ai] {type(e).__name__}: {e!r}")
        return f"error: {e}"
