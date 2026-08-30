# suto

Suto is a local-first AI harness for building automation agents. It provides a
shared runtime for tool-calling, mode-based permissions, request progress, and
token accounting.

The current `chat` mode is the interactive interface for exercising the harness
with web, date/time, and attached-document tools. The `agent` mode reserves the
automation execution path; action tools, persistent jobs, and scheduling are
intentionally not implemented yet.

## Prerequisites

- Python 3.12 or newer
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

Start one interface per process:

```bash
venv/bin/python main.py chat cli
venv/bin/python main.py agent cli
venv/bin/python main.py chat discord
venv/bin/python main.py agent discord
venv/bin/python main.py chat line
venv/bin/python main.py agent line
```

The Discord client is implemented. The LINE client module is currently only a
stub, so its commands are reserved for when that client is completed.

The CLI prints live user-facing request progress, including the current AI/tool
step, tool loop number, total and current-step elapsed time, and tokens
accumulated after each model response. A heartbeat is printed every 10 seconds
by default; set `PROGRESS_INTERVAL_SECONDS` to change it. The Discord process
prints developer timing/debug logs in its terminal instead of user-facing
progress. Set `SUTO_DEBUG=1` to enable those raw logs for other interfaces too.

## Harness structure

- `main.py` — entry point, selects a mode and starts a client
- `clients/` — input/output adapters around the shared AI harness
  - `clients/cli/` — interactive terminal client
  - `clients/discord/` — Discord client
  - `clients/line/` — LINE client (webhook server)
- `ai.py` — shared AI runtime, tool loop, progress, and token accounting
- `modes.py` — capability policies for interactive chat and automation
- `tools/` — tools the AI can call (each tool = handler + schema + prompt),
  including request-scoped document reading, summarization, and retrieval
- `progress.py` — client-independent formatting for live process status

Interfaces only translate incoming messages into harness requests and deliver
the result. Automation behavior, permissions, tools, and execution remain in
the shared runtime.

## Modes

Choose one mode when starting a client. The selected mode applies to
the entire process and cannot be changed from Discord or another chat app:

- `chat` — interactive harness mode for testing and using the current web,
  date/time, and attached-file tools.
- `agent` — automation harness placeholder. It can answer from the current
  prompt, but action tools, persistent jobs, and scheduling are not implemented
  or exposed yet.

Tool access is enforced twice: the model only receives schemas allowed by the
selected mode, and the Python execution loop rejects any disallowed tool
call. Restart the process with a different first argument to change modes.
