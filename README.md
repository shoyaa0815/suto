# Suto

Suto is a local-first personal assistant for chatting with AI, running scheduled
workflows, remembering useful context, and delivering notifications. Personal
assistant integrations are under active development.

## Installation

Python 3.12 or newer is required.

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Create a `.env` file and configure an AI provider. Example for Ollama:

```dotenv
AI_PROVIDER=ollama
AI_BASE_URL=http://localhost:11434
AI_MODEL=qwen3.5:9b
SUTO_USER_NAME=Your name
SUTO_TIMEZONE=Asia/Bangkok
SUTO_LOCALE=th
```

Example for OpenAI:

```dotenv
AI_PROVIDER=openai
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=your-model
AI_API_KEY=your-secret-key
```

Never commit `.env` files or API keys to Git.

## Usage

Start Suto:

```bash
venv/bin/python main.py chat cli
venv/bin/python main.py agent cli
```

`chat` answers questions conversationally. `agent` keeps conversation history,
uses the local user profile, and can create, list, complete, reschedule, or
cancel personal tasks and reminders from natural-language requests. In agent
mode, requests such as `สรุปวันนี้ให้หน่อย` produce a daily briefing from the
current user's open tasks and scheduled reminders.

Reminders are persisted and appear in the terminal when they become due. If
Suto was closed at that time, it reports the missed reminder on the next start.

The terminal interface exposes `/help`, `/setting` for profile settings,
`/noti` for pending reminders, `/noti del <reminder_id>` to remove one, and
`/exit`. Use `/brief` for an immediate briefing, `/brief at 08:30` to deliver
one automatically each day in the profile timezone, `/brief status` to inspect
the schedule, and `/brief off` to disable it. Automatic delivery occurs while
the CLI is running; if it starts after the configured time, that day's briefing
is delivered once. Personal-service integrations are under active development.

## Parked developer capability

Workspace editing, coding plans, verification commands, and command sandboxing
have been moved to `capabilities/developer/`. They remain tested for future
opt-in use, but the internal `developer` mode is not exposed by the main
entrypoint.

See [developer capability](capabilities/developer/README.md) and
[operations](docs/operations.md) for the retained implementation details.
