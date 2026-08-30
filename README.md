# suto

A chat bot powered by a local Ollama model, focused on building out its tool-calling system so a local LLM can answer questions well — not just from its own training data, but by calling tools (search, fetch, file reading, ...) when that gets a better answer. The chat clients are ways to talk to it while it is being built.

## Prerequisites

- Python 3.12 or newer
- [Ollama](https://ollama.com) running locally at `localhost:11434` with the `qwen3.5:9b` model pulled
- [SearXNG](https://docs.searxng.org) running locally at `localhost:8080` (used by the `search_web` tool)

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Create a `.env` file with the credentials for whichever client you run:

```
DISCORD_TOKEN=your_token_here

LINE_CHANNEL_SECRET=your_secret_here
LINE_CHANNEL_ACCESS_TOKEN=your_token_here
LINE_PORT=8000
```

## Run

One client per process — pick which one:

```bash
venv/bin/python main.py personal discord
venv/bin/python main.py private discord
venv/bin/python main.py personal line
venv/bin/python main.py private line
```

The Discord client is implemented. The LINE client module is currently only a
stub, so its commands are reserved for when that client is completed.

## Structure

- `main.py` — entry point, starts the client named on the command line
- `clients/` — one subpackage per way of talking to the bot
  - `clients/discord/` — Discord client
  - `clients/line/` — LINE client (webhook server)
- `ai.py` — talks to Ollama, runs the tool-calling loop
- `tools/` — tools the AI can call (each tool = handler + schema + prompt),
  including request-scoped document reading, summarization, and retrieval

Clients only turn incoming messages into a prompt and send the answer back. What the bot can actually do lives in `ai.py` and `tools/`, shared by all of them.

## Workspaces

Choose one tool policy when starting a client. The selected policy applies to
the entire process and cannot be changed from Discord or another chat app:

- `personal` — web search, URL fetching, current date/time, and attached-file
  reading/search/summarization are available.
- `private` — internet tools are unavailable; answers use only the current
  message and request-scoped attached files.

Tool access is enforced twice: Ollama only receives schemas allowed by the
active workspace, and the Python execution loop rejects any disallowed tool
call. Restart the process with a different first argument to change modes.

## Reply language

The reply language is selected from the original user message before any file
metadata or tool output is added. An explicit language request has priority,
then Lingua detects the prompt language, and ambiguous input falls back to that
user’s previous language in the Discord channel or Thai by default. Attachment
and web-page languages never determine the final answer language. The final
answer is checked again with Lingua; if it is in the wrong language, Ollama
rewrites it without tool access and the result is validated again before being
sent to the client.

## Document pipeline

Files are extracted with source metadata and split into conservative
token-budgeted chunks (2,500 estimated tokens with a small overlap). The bot
then chooses a tool based on intent:

- `read_attached_file` returns raw text only when a document is small.
- `summarize_attachment` maps chunks into factual notes, recursively reduces
  them into one summary, and retains page/sheet citations.
- `search_attachment` uses local sparse word and character-ngram vectors plus
  exact-term matching to retrieve up to 6 relevant chunks for document Q&A.

Extraction, chunk vectors, and summaries are cached in memory by SHA-256 for
the life of the process. No attachment path is accepted from the model and no
private document content is sent to an internet service.

Ollama requests default to a 300-second timeout. Override it when needed:

```bash
OLLAMA_TIMEOUT_SECONDS=420 venv/bin/python main.py personal discord
```

Timing logs report attachment download, extraction/indexing, cache hits,
retrieval, tool execution, Ollama prompt/output token counts, language fixes,
and total request time without logging document contents.
