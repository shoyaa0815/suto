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
