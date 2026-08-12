# suto

A chat bot powered by a local Ollama model, focused on building out its tool-calling harness so a local LLM can answer questions well — not just from its own training data, but by calling tools (search, fetch, ...) when that gets a better answer. The chat clients are ways to talk to it while it is being built.

## Prerequisites

- [Ollama](https://ollama.com) running locally at `localhost:11434` with the `qwen3.5:9b` model pulled
- [SearXNG](https://docs.searxng.org) running locally at `localhost:8080` (used by the `search_web` tool)

## Setup

```bash
pip install -r requirements.txt
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
python main.py discord
python main.py line
```

## Structure

- `main.py` — entry point, starts the client named on the command line
- `clients/` — one subpackage per way of talking to the bot
  - `clients/discord/` — Discord client
  - `clients/line/` — LINE client (webhook server)
- `ai.py` — talks to Ollama, runs the tool-calling loop
- `file_reader.py` — extracts text from attachments (pdf, docx, xlsx, etc.)
- `harness/` — tools the AI can call (each tool = handler + schema + prompt)

Clients only turn incoming messages into a prompt and send the answer back. What the bot can actually do lives in `ai.py` and `harness/`, shared by all of them.
