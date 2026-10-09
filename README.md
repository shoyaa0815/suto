# Suto

Suto is a local-first AI automation runtime for durable Jobs, reusable
Automations and Schedules. Submit and inspect work through the CLI or Runtime
API; a standalone Worker executes it under workspace, permission and approval
controls, with results and notifications persisted in SQLite.

## Installation

Python 3.12 or newer is required.

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Copy `config.example.yaml` to `config.yaml` and set the runtime:

```yaml
version: 1
runtime:
  provider: ollama
  model: qwen3.5:9b
  base_url: http://localhost:11434
  timezone: Asia/Bangkok
  workspace: /absolute/path/to/project
```

The CLI, API and Worker use this same runtime configuration without opening
Chat or Dashboard. Set `SUTO_CONFIG_PATH=/absolute/path/to/config.yaml` when
running processes from different directories, and use the same `SUTO_DB_PATH`.
Restart all running Suto processes after edits. See
[runtime settings](docs/operations.md#runtime-configuration) for options and limits.

Existing `.env` provider settings still work when their YAML fields are absent:

```dotenv
AI_PROVIDER=ollama
AI_BASE_URL=http://localhost:11434
AI_MODEL=qwen3.5:9b
```

Never commit `.env` files or API keys to Git.
For OpenAI, select `provider: openai`, set a model, and put `AI_API_KEY` in `.env`.
YAML runtime fields override environment fallbacks; secrets remain environment only.

## Usage

Activate the virtual environment, then start Suto:

```bash
source venv/bin/activate
python3 main.py
python3 main.py settings
python3 main.py api
```

Run automation in a separate terminal or host-managed service:

```bash
venv/bin/python main.py worker
```

The worker runs jobs and schedules without a chat session. The CLI submits and
manages work through the same `SUTO_DB_PATH`; closing the CLI leaves the worker
running. Stop the worker with Ctrl-C or SIGTERM. Interrupted jobs can be resumed
after restart. See [worker lifecycle](docs/operations.md#database-and-worker-lifecycle)
for ownership, recovery and notification delivery.

The CLI opens an Automation command prompt with scrollable command output.
Use `/help` for Jobs, Automations, Schedules, durable approvals and notifications.
Submit free-form task descriptions with `/run <task>`; plain text does not start
an interactive AI request. `/notifications` lists unread Job inbox items and
`/notifications ack <event_id>` acknowledges one. Live presentation is optional
with `SUTO_NOTIFY_CLI=1`; inbox items are persisted while the CLI is closed.

`api` starts the guarded local API on `http://127.0.0.1:8766`. Durable runtime
routes and the existing `/runs` compatibility API remain available. See
[API operations](docs/operations.md#local-agent-api) for routes and boundaries.

`settings` (also accepted as `setting`) opens a local web editor and prints a
private sign-in link. Edit validated `config.yaml` YAML, including runtime
provider, model, timezone, workspace and limits. Save checks for concurrent edits
and writes atomically; restart Suto processes afterward. Keep secrets in `.env`.

Chat fallback, personal task/reminder commands, chat Skill activation, voice
entry and the saved-chat dashboard are retired. Existing personal data and
legacy storage remain intact. Personal Memory CRUD tools and automatic memory
retrieval into prompts are also retired, including in `/runs`. Session summaries
and opt-in Job workspace index/search remain available.
Versioned Automation Skills and `/skill-proposal`
review remain available; see [Skills](docs/skills.md).

Review saved draft automation Skills with `/skill-proposal list`,
`/skill-proposal show <id>`, and `/skill-proposal history <id>`.
Run `/skill-proposal detect` to inspect completed saved-automation runs and
create review-only proposals when one pinned automation version completed at
least three runs across at least two UTC dates. Detection reads execution
metadata only and stores three source job IDs as provenance; it does not copy
prompts, results, parameters, or conversation history into proposal data.
Use `/skill-proposal approve <id>`, `/skill-proposal reject <id>`, or
`/skill-proposal delete <id>` to decide a draft. Approval saves a new versioned
Skill without activating it.

## Automation CLI

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
Jobs saved while the independent worker is unavailable wait until a worker is ready.

Create a schedule with exactly one of `--at <ISO>`, `--every <seconds>`, or
`--cron <five-field expression>`. Options precede the task. An `--at` value
without an offset uses the runtime timezone (legacy profile timezone if unset);
an offset in the value specifies the instant. `--timezone` overrides the default for local times and
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
