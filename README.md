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

Never commit `.env` files or API keys to Git.

## Usage

Activate the virtual environment, then start Suto:

```bash
source venv/bin/activate
python3 main.py
python3 main.py setting
python3 main.py api
```

Suto keeps conversation history and uses the local user profile. Each CLI launch
starts a fresh chat; prior chats remain stored but are not used as context.
Saved memories remain available across launches. In the CLI,
personal tasks and reminders are managed with explicit slash commands; chat
does not create or change them.

The `cli` interface is a plain stdin/stdout session. It prints the active model
and mode, accepts requests in a framed `>` prompt, and leaves each submitted
user message in its frame. Assistant replies are printed without a name prefix.
`api` starts the local agent API on `http://127.0.0.1:8766`. See
[API operations](docs/operations.md#local-agent-api) for routes, examples,
security boundaries, and run lifecycle.

`setting` opens the local-only web page in the default browser and prints
a private sign-in link. Dashboard shows saved CLI conversations and their chat
messages for reading. Settings edits `display_name`, `locale`, and `timezone`
in `config.yaml`. AI chat remains in the CLI.
Save validates changes in one step; restart Suto after saving. The CLI
reads these values but cannot change them.
Legacy `SUTO_TIMEZONE`, `SUTO_LOCALE`, and `SUTO_USER_NAME` environment values
remain fallbacks when the corresponding YAML value is absent. Keep API keys and
platform tokens in `.env`, never in `config.yaml`.

Reminders are persisted and appear in the terminal when they become due. If
Suto was closed at that time, it reports the missed reminder on the next start.

`/reminder` lists pending reminders. Create one with an explicit duration or
clock time, for example `/reminder อีกห้านาทีเตือนกินข้าว`,
`/reminder อีก 5 นาที เตือนกินข้าว`, or
`/reminder 00.05 เตือนให้เข้านอนหน่อย`. Clock times use the next occurrence in
the profile timezone; `HH:MM` and `HH.MM` are accepted. `/task` lists open
personal tasks, and `/task ทำอะไรต่างๆบลาๆ` creates one. The commands
`/reminder remove <name-or-id>` and `/task remove <name-or-id>` delete a
pending reminder or open task from the database. If a name matches more than
one item, nothing is deleted and the matching IDs are shown. `/jobs` lists
recent automation jobs without starting one. `/help`, `/version`, `/skills`,
`/skill activate <name>`, `/skill deactivate <name>`, and `/exit` remain
available. Put personal skills in `~/.suto/skills/<name>/SKILL.md`, restart Suto,
then use `/<name> <message>` for one request. See [skills](docs/skills.md) for
the file format and tool rules.

## MCP tools

Copy `mcp.example.yaml` to the ignored local `mcp.yaml`, configure a stdio
server, and set `SUTO_MCP_CONFIG` to that file's absolute path in `.env`.
Set file permissions to `600` on POSIX hosts.
Without this setting, Suto starts no MCP server. Each `allow_tools` entry is
an original tool name returned by that server. Suto exposes an enabled tool as
`mcp.<server>.<tool>`. Tools are
discovered when an AI request starts and the server process is closed when the
request ends. Skills can use these names in `allowed_tools` and
`recommended_tools`. See [operations](docs/operations.md#mcp-server-lifecycle)
for configuration and permission details. Configured MCP server processes run
with the Suto host user's permissions and are not sandboxed.

## Workspace automation capability

Suto has one agent mode. Workspace editing, coding plans, verification commands,
and command sandboxing remain available only to jobs with an explicit workspace
and the required permissions. Direct shell, file-write, Git mutation, and Python
execution tools are disabled in interactive requests until they have the same
permission checks. Existing jobs retain their permissions when upgraded.

See [developer capability](capabilities/developer/README.md) and
[operations](docs/operations.md) for the retained implementation details.
