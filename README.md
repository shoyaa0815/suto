# suto

Automation bot powered by a local Ollama model, focused on building out its tool-calling harness. Discord is just a test client for it right now.

## Prerequisites

- [Ollama](https://ollama.com) running locally at `localhost:11434` with the `qwen3.5:9b` model pulled
- [SearXNG](https://docs.searxng.org) running locally at `localhost:8080` (used by the `search_web` tool)

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file:

```
DISCORD_TOKEN=your_token_here
```

## Run

```bash
python main.py
```

## Structure

- `main.py` — entry point
- `discord_bot.py` — Discord client, used as a test client to talk to the bot
- `ai.py` — talks to Ollama, runs the tool-calling loop
- `file_reader.py` — extracts text from attachments (pdf, docx, xlsx, etc.)
- `harness/` — tools the AI can call (each tool = handler + schema + prompt)
