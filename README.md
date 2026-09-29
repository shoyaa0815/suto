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

Start Suto:

```bash
venv/bin/python main.py
venv/bin/python main.py tui
venv/bin/python main.py settings
venv/bin/python main.py api
```

Suto keeps conversation history, uses the local user profile, and can create,
list, complete, reschedule, or cancel personal tasks and reminders from
natural-language requests. Requests such as `สรุปวันนี้ให้หน่อย` can be
answered from the current user's open tasks and scheduled reminders.

The `cli` interface is a plain stdin/stdout session. It prints the active model
and mode, accepts requests in a framed `>` prompt, and leaves each submitted
user message in its frame. Assistant replies are printed without a name prefix.
The CLI does not launch a full-screen UI.

`tui` opens a full-screen terminal chat over the same agent executor. It shows
model, tool, permission, and delegated child activity from the agent event
trace. PageUp scrolls history; End returns to the latest entry and input;
F2 shows the previous tool's safe metadata. Ctrl-C or `/cancel` cancels the
active request. Use `/new`, `/session`, and `/resume <session-id>` to manage
the local TUI conversation, and `/skills` or `/skill activate|deactivate <name>`
to select registered Skills. The input remains available while a request runs;
a draft submitted during an active run stays in the input box.

`api` starts the local agent API on `http://127.0.0.1:8766`. See
[API operations](docs/operations.md#local-agent-api) for routes, examples,
security boundaries, and run lifecycle.

`settings` opens a local-only control center. It prints a private sign-in link
and opens it in the default browser. The dashboard shows recent job status and
automation skills from the existing database; these views are read-only and
show an empty state until the database exists. MCP configuration is managed in
`mcp.yaml`, outside the settings editor.
The profile form edits `display_name`, `locale`, and `timezone` in `config.yaml`.
Validate and review changes before saving; restart Suto after saving. The CLI
reads these values but cannot change them.
Legacy `SUTO_TIMEZONE`, `SUTO_LOCALE`, and `SUTO_USER_NAME` environment values
remain fallbacks when the corresponding YAML value is absent. Keep API keys and
platform tokens in `.env`, never in `config.yaml`.

Reminders are persisted and appear in the terminal when they become due. If
Suto was closed at that time, it reports the missed reminder on the next start.

The terminal interface exposes `/help`, `/notification` for pending reminders,
`/notification remove <name>` to remove one by name, `/skills` to list skills,
`/skill activate <name>` and `/skill deactivate <name>` to select skills for the
current CLI run, and `/exit`. See [skills](docs/skills.md) for the file format
and tool restriction rules. If a reminder name is ambiguous, no reminder is
removed and the matches are listed. Personal-service
integrations are under active development.

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
