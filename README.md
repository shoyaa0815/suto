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

Copy `config.example.yaml` to `config.yaml` and set the non-secret local profile:

```yaml
version: 1
profile:
  timezone: Asia/Bangkok
  locale: th
  display_name: Your name
```

Create a `.env` file for provider configuration and secrets. Example for Ollama:

```dotenv
AI_PROVIDER=ollama
AI_BASE_URL=http://localhost:11434
AI_MODEL=qwen3.5:9b
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
venv/bin/python main.py
venv/bin/python main.py settings
```

Suto keeps conversation history, uses the local user profile, and can create,
list, complete, reschedule, or cancel personal tasks and reminders from
natural-language requests. Requests such as `สรุปวันนี้ให้หน่อย` can be
answered from the current user's open tasks and scheduled reminders.

The `cli` interface is a plain stdin/stdout session. It prints the active model
and mode, accepts requests in a framed `>` prompt, and leaves each submitted
user message in its frame. Assistant replies are printed without a name prefix.
The CLI does not launch a full-screen UI.

`settings` opens a local-only web editor for `config.yaml`. It prints a private
sign-in link and opens it in the default browser. Validate and review changes
before saving; restart Suto after saving. The CLI reads these values but cannot
change them.
Legacy `SUTO_TIMEZONE`, `SUTO_LOCALE`, and `SUTO_USER_NAME` environment values
remain fallbacks when the corresponding YAML value is absent. Keep API keys and
platform tokens in `.env`, never in `config.yaml`.

Reminders are persisted and appear in the terminal when they become due. If
Suto was closed at that time, it reports the missed reminder on the next start.

The terminal interface exposes `/help`, `/notification` for pending reminders,
`/notification remove <name>` to remove one by name, and `/exit`. If a name is
ambiguous, no reminder is removed and the matches are listed. Personal-service
integrations are under active development.

## Parked developer capability

Workspace editing, coding plans, verification commands, and command sandboxing
have been moved to `capabilities/developer/`. They remain tested for future
opt-in use, but the internal `developer` mode is not exposed by the main
entrypoint.

See [developer capability](capabilities/developer/README.md) and
[operations](docs/operations.md) for the retained implementation details.
