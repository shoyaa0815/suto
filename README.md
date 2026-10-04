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
one item, nothing is deleted and the matching IDs are shown. `/help`,
`/version`, `/skills`, `/skill activate <name>`, `/skill deactivate <name>`,
and `/exit` remain available. Put personal skills in
`~/.suto/skills/<name>/SKILL.md`, restart Suto,
then use `/<name> <message>` for one request. See [skills](docs/skills.md) for
the file format and tool rules.

## Automation CLI (Phase 1)

Enter these commands at the Suto CLI prompt. `/run` saves a one-time job and
returns its ID; a queued job has not necessarily started. The default workspace
is the current directory, with read access only and command execution denied.
`--allow-write` and `--allow-command` set permission ceilings; an action that
requires approval still waits for a separate decision.

```text
/run "inspect this repository"
/run --workspace ~/projects/suto --allow-command "run the tests and summarize failures"
/jobs
/status <job_id>
/cancel <job_id>
/resume <job_id>
```

`/jobs` lists recent jobs. `/status` shows the latest attempt, timestamps,
result summary, and safe error. Cancel is repeatable for queued, running, or
approval-waiting jobs. Resume applies to interrupted or blocked jobs after
workspace checkpoint checks; it does not restart a completed or cancelled job.
Jobs saved while the CLI worker is unavailable wait until a worker is ready.

Create a schedule with exactly one of `--at <ISO>`, `--every <seconds>`, or
`--cron <five-field expression>`. Options precede the task. An `--at` value
without an offset uses the profile timezone; an offset in the value specifies
the instant. `--timezone` overrides the profile timezone for local times and
cron. The defaults are `--missed-run run_once`, `--retry 0`, and
`--retry-delay 60` seconds. Workspace and permission defaults match `/run`.

```text
/schedule create --every 3600 --retry 2 --retry-delay 60 "inspect repository status"
/schedule create --cron "0 8 * * *" --timezone Asia/Bangkok --missed-run skip "check daily status"
/schedule list
/schedule show <schedule_id>
/schedule pause <schedule_id>
/schedule resume <schedule_id>
/schedule history <schedule_id>
```

`run_once` queues one recovery occurrence after downtime; `skip` records an
overdue occurrence as skipped and moves to the next one. A retry keeps the same
job ID and creates a new execution attempt. Pausing prevents new triggers but
does not cancel jobs already queued or running.

Save a JSON definition, then create and run a versioned automation. For example,
save this as `review.json` in the CLI working directory:

```json
{
  "name": "repo-review",
  "prompt_template": "Review {{repo}} at {{depth}} depth.",
  "parameter_schema": {
    "repo": {"type": "string", "required": true},
    "depth": {"type": "string", "default": "quick"}
  },
  "workspace": "."
}
```

```text
/automation create review.json
/automation list
/automation show repo-review
/automation run repo-review repo=suto depth=full
/automation history repo-review
/automation update repo-review review.json
```

An update saves a new immutable version after you edit the JSON file; existing
jobs keep their original version. A run validates required parameters and saves
their values, including defaults. Definition skills must already exist. Keep
secrets out of definitions and run parameters.

You can also save a schedule for an existing automation:

```text
/schedule automation repo-review --cron "0 8 * * *" --timezone Asia/Bangkok repo=suto
/schedule show <schedule_id>
```

The schedule pins the current automation version, validated parameters (including
defaults), skill versions, workspace, and permission ceiling. Options may include
`--at`, `--every`, `--cron`, `--timezone`, `--missed-run`, `--retry`, and
`--retry-delay`; workspace and permissions come from the saved automation.
`/automation update` does not change existing schedules. Scheduled automation
execution is not available yet; creating one does not queue a job.

If a job requests a write or command approval, inspect and decide its pending
request by ID:

```text
/approvals
/approval show <approval_id>
/approval allow <approval_id>
/approval deny <approval_id>
```

Allow only authorizes the displayed request; the action still passes permission
and sandbox checks. Denied, expired, or cancelled requests cannot authorize an
action. See [automation operations](docs/operations.md#automation-cli-phase-1)
for lifecycle details.

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
