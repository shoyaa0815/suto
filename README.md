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

`chat` answers questions conversationally. `agent` is the personal-assistant
mode and will gain task, reminder, and integration tools as they are developed.

Type `/help` inside the terminal interface to inspect the currently available
commands. The next development phase will add persistent conversations, user
identity, personal memory, reminders, and personal-service integrations.

## Parked developer capability

Workspace editing, coding plans, verification commands, and command sandboxing
have been moved to `capabilities/developer/`. They remain tested for future
opt-in use, but the internal `developer` mode is not exposed by the main
entrypoint.

See [developer capability](capabilities/developer/README.md) and
[operations](docs/operations.md) for the retained implementation details.
