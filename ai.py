import inspect
import unicodedata

import aiohttp

from harness import get_tools
from modes import DEFAULT_MODE, get_mode_policy

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen3.5:9b"
MAX_TOOL_ROUNDS = 6

# Ollama's default temperature leaves both the answer and, more importantly,
# the decision to call a tool at all up to sampling — the same question can
# get search_web called on one attempt and skipped (answered from stale
# training memory) on the next. This bot answers questions, so a repeatable,
# tool-grounded answer matters more than variety; a low temperature is what
# makes "call search_web when the rules say to" actually reliable instead of
# a coin flip.
TEMPERATURE = 0.2

# The system prompt tells the model to answer with 0-9 digits only. That rule
# holds most of the time but is still only a request, and the failure it
# prevents is silent: transcribing 2569 into another numeral system, the model
# has produced ๒๕๖๗ — a different number, in a reply that reads as confident as
# any other. Rewriting digits back is exact and costs nothing, so the guarantee
# comes from here rather than from the model following instructions.
#
# Every numeral system is covered, not just Thai: asked the same question nine
# times the model reached for Bengali (256৯), Arabic-Indic (20۲6), and a mix of
# Burmese and Khmer within one year (၂០၂۶). Which script it picks is not
# predictable, so listing them one at a time would keep missing new ones.
def _to_ascii_digits(text: str) -> str:
    return "".join(
        str(unicodedata.digit(ch)) if ch.isdigit() and not ch.isascii() else ch
        for ch in text
    )

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
_BASE_PROMPT = """You are a concise, accurate assistant in a chat.

Active workspace policy:
{mode_prompt}

Answer style:
- Be direct. No greetings, no filler, no restating the question.
- Keep answers as short as fully answering the question allows.
- Write all numbers with the digits 0-9, whatever language you are answering
  in. Never use Thai, Arabic-Indic, or any other numeral set — transcribing
  digits between numeral systems is where their values get corrupted.

Tool use:
{tool_guidance}
- Never guess or make up facts, names, numbers, or links. If you're not sure
  and can't search, say so plainly instead of fabricating an answer.
- After searching, base your answer only on what the results actually say.
- If a message contains a "[attached file: ...]" section, the text under it
  is already the file's full extracted content. Read and answer from it
  directly — never call a tool to try to fetch or re-read that file.

Reply in the same language the user wrote in."""


def _build_system_prompt(mode_prompt: str, tool_guidance: str) -> str:
    guidance = tool_guidance or "- No tools are available in this workspace."
    return _BASE_PROMPT.format(
        mode_prompt=mode_prompt,
        tool_guidance=guidance,
    )


async def _chat(
    session: aiohttp.ClientSession,
    messages: list,
    tool_schemas: list,
    think: bool = False,
) -> dict:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "think": think,
        "options": {"temperature": TEMPERATURE},
    }
    # Ollama treats tools as optional. Omitting the field entirely is the most
    # compatible representation of a workspace where no tools are allowed.
    if tool_schemas:
        payload["tools"] = tool_schemas

    async with session.post(
        OLLAMA_URL,
        json=payload,
    ) as response:
        response.raise_for_status()
        return await response.json()


# Each call starts from a clean slate: system prompt + this one prompt.
# Nothing from an earlier message is fed back in. This is deliberate — the
# bot answers one-off questions, where the same question has to get the
# same quality of answer regardless of what was asked before it.
#
# The context that DOES matter is `messages` below: it accumulates the
# assistant's tool calls and their results across the tool-calling rounds,
# so the model remembers what it already looked up while working on this
# one task. It's thrown away once the task is answered.
#
# think=False turns off the model's reasoning pass. Ollama enables it by
# default on any model that declares the "thinking" capability (qwen3.5 does),
# and on this workload it cost ~20x the wall time — a plain question spent
# ~2000 tokens reasoning before ~150 tokens of answer — without being more
# accurate, since the tool-calling loop already supplies the reasoning
# structure. Pass think=True for a task that genuinely needs multi-step
# reasoning before acting.
async def ask_local_ai(
    prompt: str,
    think: bool = False,
    mode: str = DEFAULT_MODE,
) -> str:
    policy = get_mode_policy(mode)
    tools, tool_schemas, tool_guidance = get_tools(policy.allowed_tools)
    timeout = aiohttp.ClientTimeout(total=120)
    messages = [
        {
            "role": "system",
            "content": _build_system_prompt(policy.prompt, tool_guidance),
        },
        {"role": "user", "content": prompt},
    ]
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for _ in range(MAX_TOOL_ROUNDS):
                data = await _chat(session, messages, tool_schemas, think)
                message = data.get("message", {})
                tool_calls = message.get("tool_calls")

                if not tool_calls:
                    answer = _to_ascii_digits(message.get("content", "").strip())
                    return answer or "no response from ai"

                messages.append(message)
                for call in tool_calls:
                    name = call["function"]["name"]
                    args = call["function"].get("arguments") or {}
                    if name in tools:
                        func = tools[name]
                        result = (
                            await func(**args)
                            if inspect.iscoroutinefunction(func)
                            else func(**args)
                        )
                    else:
                        result = f"tool is not allowed in {mode} mode: {name}"
                    messages.append({"role": "tool", "content": str(result)})

            return "no response from ai"
    except aiohttp.ClientConnectorError as e:
        print(f"[ai] connection failed: {e!r}")
        return "can't connect check ai server"
    except Exception as e:
        print(f"[ai] {type(e).__name__}: {e!r}")
        return f"error: {e}"
