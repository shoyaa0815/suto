# Operations and advanced features

## Supported interfaces

The CLI is an Automation command interface. `main.py voice`, chat fallback,
personal task/reminder commands, chat Skill routing and the saved-chat dashboard
are retired. Legacy modules and stored personal data remain for compatibility;
opening the CLI starts no personal delivery loop.

## Local agent API

Run `venv/bin/python main.py api`. The API binds only to `127.0.0.1:8766`;
there is no public binding option. It accepts the numeric loopback Host header,
checks the connecting peer and Origin, sends no CORS permission, and requires
`X-Suto-Request: 1` plus JSON content type on POST. Local processes with access
to the loopback interface can use it; there is no account or token system.
Do not forward or expose this port on a network. Remote exposure needs separate
authentication, transport security, and threat review.

Routes:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Process health (`{"status":"ok"}`) |
| POST | `/runs` | Start a run; JSON `{"message":"...","session_id":"...","skills":["research"]}` |
| GET | `/runs/{run_id}` | Status and final result when available |
| GET | `/runs/{run_id}/events` | SSE trace stream; supports `Last-Event-ID` |
| POST | `/runs/{run_id}/cancel` | Cancel the active task; send `{}` with required headers |

Only `message` is required. Requests are limited to 16 KiB, messages to 8,000
characters, and at most eight registry Skill names. Unknown fields and unknown
Skills are rejected. A new request without `session_id` creates a session in the
existing SQLite conversation tables. A supplied session must belong to the
local API identity. API sessions are separate from CLI sessions. The final
response includes `session_id`, text, status, and token usage. Failed and
blocked responses use generic public text and error values; provider or tool
exception details are not exposed.

`POST /runs` returns 202 with the canonical `run_id` immediately. The executor,
runtime, trace, child-parent links, cancellation, and result use that ID. The
legacy `runtime_run_id` response field equals `run_id`. SSE replays persisted
`run_events` rows, including the bounded child trace, after the existing event
sanitizer has removed prompts, tool arguments, observations, and secret fields.
The stream closes after the run ends. Cancellation cancels the same asyncio
task that awaits the model, tool, and any delegated child. It is best effort
for blocking operations that do not support cancellation.

The `/runs` API passes an `AgentRequest` to the same AI executor as the CLI. The
executor applies native tool, Skill, MCP, and delegation policy. MCP commands,
secrets, and job-scoped workspace permissions come only from trusted local
configuration and cannot be supplied in HTTP payloads. The API starts no
automation or reminder-delivery worker.

Run status, timestamps, sanitized terminal results, and token usage persist in
the same SQLite database as sessions and traces. Completed, failed, and cancelled
runs remain queryable after restart. A run owned by a dead process becomes
`interrupted` on recovery; model and tool calls are not resumed. The database
reserves one active top-level run per session across API and CLI processes.
Up to four independent API runs may execute at once. API approval submission is
not exposed; a request requiring interactive approval fails closed there.
Pending interactive approvals are memory-only and invalid after restart.
Trace delivery retains the database's existing durability and backup behavior.

## Runtime API

Jobs accept optional `mcp_tools` selection metadata through the service/API;
Automation definitions accept the same field. Operator policy defaults to empty
and is configured separately with `SUTO_JOB_MCP_POLICY`. Selected Jobs remain
blocked before execution. See [selection, pinning and revocation](job-mcp-selection.md)
for the policy format, snapshot/migration behavior and remaining execution gates.

The same local server also exposes durable Jobs, Automations, and Schedules.
These routes call `JobService`, `AutomationService`, and `ScheduleService`;
they do not execute jobs in the API process. Start
`venv/bin/python main.py worker` separately, using the same `SUTO_DB_PATH` as
`venv/bin/python main.py api`. A submission returns **202** after the queued
job is persisted, even if no worker is running. Closing or restarting the API
does not cancel these jobs. The worker claims them, calls AgentRuntime, and
persists attempts, results, error metadata, and notifications through the
existing workflow lifecycle.

All routes retain the loopback peer, numeric Host, Origin, and POST header
checks above. Runtime jobs and automation definitions are shared within the
host's workflow database, as in the CLI; they are not chat-session resources.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/jobs` | Persist a job; required `prompt`, optional `workspace`, `allow_write`, `allow_command` |
| GET | `/jobs` | Recent jobs, returned as `{"jobs":[...]}` (up to 20) |
| GET | `/jobs/{job_id}` | Job details, status, full result text, usage, and timestamps |
| GET | `/jobs/{job_id}/result` | Structured result snapshot, including summary, safe error, attempt, automation version, schedule, trigger, and retry references |
| POST | `/jobs/{job_id}/cancel` | Persist cancellation; send `{}`; repeated cancellation is idempotent |
| GET | `/automations` | Saved definitions, returned as `{"automations":[...]}` |
| GET | `/automations/{id_or_name}` | Definition, current version, parameter schema, and pinned Skill names |
| POST | `/automations/{id_or_name}/run` | Persist a run of the current version; send `{"parameters":{...}}` or `{}` for defaults |
| GET | `/schedules` | Recent schedules, returned as `{"schedules":[...]}` (up to 100) |
| GET | `/schedules/{schedule_id}` | Schedule details, timezone, next occurrence, and pinned automation snapshot when present |

Job prompts are limited to 8,000 characters and requests to 16 KiB. Workspace
defaults to the API process's working directory and must resolve to an existing
directory. Write and command flags must be JSON booleans and default to false.
Unknown fields are rejected, including client-supplied tool allowlists,
approval overrides, execution options, and automation version overrides.
Automation run parameters must be an object and satisfy the stored schema;
detectable secrets are rejected by the existing application validation.
Running an automation atomically pins its current version, rendered parameters,
workspace, permission ceiling, and Skill versions. Later definition updates do
not change an existing job or schedule snapshot.

Permission flags only enable consideration of an action. Workspace containment,
exact-action durable approval, verification after writes, command allowlists,
and sandbox checks still apply in the worker. A job requiring approval becomes
`waiting_approval` without performing the action. Use the existing CLI
`/approvals`, `/approval show <approval_id>`, and
`/approval allow <approval_id>` or `/approval deny <approval_id>` to decide;
this API does not add an approval-decision route. Cancellation invalidates
pending approvals and is observed by the independent worker before finalizing
its active attempt. Existing interruption/recovery rules remain in effect.

Job detail `result` contains persisted full text when available; the `/result`
snapshot contains the existing bounded `result_summary` (up to 200 characters).
Both are available while polling, with null result fields before completion.
Job errors expose safe messages rather than internal exception details.
Runtime request failures use the existing string `error` field with an added
stable `error_code`, for example:

```json
{"error":"Job not found.","error_code":"JOB_NOT_FOUND"}
```

Invalid input/workspace/parameters return 400; local access or workspace
permission denial returns 403; missing resources return 404; cancellation from
an incompatible state returns 409 (`JOB_STATE_CONFLICT`); admission quota
failures return 429 (`QUOTA_EXCEEDED`). Oversized bodies return 413 and unexpected
service failures return a generic 500 (`INTERNAL_ERROR`). Error responses also
retain no-store and content-type protection headers.

Example using another terminal on the same host:

```bash
curl -sS http://127.0.0.1:8766/jobs \
  -H 'X-Suto-Request: 1' -H 'Content-Type: application/json' \
  -d '{"prompt":"Inspect source files and report in English.","workspace":"/path/to/project"}'
curl -sS http://127.0.0.1:8766/jobs/job_RETURNED_ID
curl -sS http://127.0.0.1:8766/jobs/job_RETURNED_ID/result
curl -sS http://127.0.0.1:8766/jobs/job_RETURNED_ID/cancel \
  -H 'X-Suto-Request: 1' -H 'Content-Type: application/json' -d '{}'
```

## Host-managed settings

Non-secret runtime settings and legacy profile defaults live in `config.yaml`; secrets
remain in `.env`. Activate `venv` as shown in the README, then run
`python3 main.py settings` to open the local web
editor. It prints a private sign-in link, validates edits before saving, and
persists valid settings atomically. Restart Suto after saving so the CLI uses
the new values.
Existing personal data and timestamps are not rewritten.

YAML profile values take precedence over the legacy `SUTO_TIMEZONE`,
`SUTO_LOCALE`, and `SUTO_USER_NAME` environment defaults. Unknown sections,
unknown profile keys, unsupported config versions, invalid timezones, and
non-mapping YAML fail closed. `config.yaml` is local and ignored by Git;
`config.example.yaml` documents the versioned schema.

## Runtime configuration

`application.runtime_configuration.load_runtime_settings()` is the shared,
validated source for the CLI, Runtime API, AI provider factory and standalone
Worker. Edit the optional `runtime` section of the existing version 1 YAML
directly; no Chat, Dashboard, user identity or chat session is needed to configure
or execute the Worker. The loader reads runtime fields independently of voice
and personal profile validation. The API retains its existing personal routes
and profile validation, but `/jobs` needs no conversation or chat session.

Default path: `config.yaml` in the process working directory. Set
`SUTO_CONFIG_PATH` to an absolute filename in the host environment or `.env` to
select another file for **every** CLI/API/Worker process. An explicitly selected
missing file fails startup; a missing default file or omitted runtime fields use
compatible defaults. A file containing only `version: 1` and `runtime` is enough
for a new runtime installation. Restart all processes after configuration edits:
provider, execution limits and approval TTL are bound at module startup, and this
is not a hot-reload interface. Use the same `SUTO_DB_PATH` for durable jobs.

```yaml
version: 1
runtime:
  provider: ollama
  model: qwen3.5:9b
  base_url: http://localhost:11434
  timezone: Asia/Bangkok
  workspace: /absolute/path/to/project
  options:
    temperature: 0.2
    timeout_seconds: 300
  limits:
    max_tokens: 100000
    max_tool_calls: 40
```

Precedence is per field: runtime YAML → primary environment variable → legacy
alias → built-in default. Timezone is the compatibility exception:
`runtime.timezone` → `profile.timezone` → `SUTO_TIMEZONE` → `UTC`. Personal reminders
keep using the profile/user timezone. Workspace defaults affect newly submitted
ad hoc jobs and schedules; an explicit CLI/API workspace overrides the default.
Relative workspace paths (including the default `.`) resolve from the process
working directory. Use absolute paths for services. Existing jobs, schedules and
pinned automation workspaces/options/permissions are not rewritten. Runtime
configuration grants no write or command permission and adds no tool allowlist.

| YAML field under `runtime` | Environment fallback (legacy alias) | Default |
| --- | --- | --- |
| `provider` | `AI_PROVIDER` | `ollama` |
| `model` | `AI_MODEL` (`OLLAMA_MODEL`) | `qwen3.5:9b` |
| `base_url` | `AI_BASE_URL` (`OLLAMA_URL`) | `http://localhost:11434`; `https://api.openai.com/v1` for `openai` |
| `timezone` | `SUTO_TIMEZONE`, after legacy profile | `UTC` |
| `workspace` | `SUTO_WORKSPACE` | `.` |
| `options.temperature` | `AI_TEMPERATURE` (`OLLAMA_TEMPERATURE`) | `0.2` |
| `options.timeout_seconds` | `AI_TIMEOUT_SECONDS` (`OLLAMA_TIMEOUT_SECONDS`) | `300` |
| `options.max_tool_rounds` | `MAX_TOOL_ROUNDS` | `6` |
| `options.max_agent_tool_rounds` | `MAX_AGENT_TOOL_ROUNDS` | `20` |
| `options.max_language_corrections` | `MAX_LANGUAGE_CORRECTIONS` | `2` |
| `options.progress_interval_seconds` | `PROGRESS_INTERVAL_SECONDS` | `10` |
| `options.approval_ttl_seconds` | `APPROVAL_TTL_SECONDS` | `600` |
| `limits.max_elapsed_seconds` | `MAX_JOB_SECONDS` | `900` |
| `limits.max_tokens` | `MAX_JOB_TOKENS` | `100000` |
| `limits.max_tool_calls` | `MAX_TOOL_CALLS` | `40` |
| `limits.max_changed_files` | `MAX_CHANGED_FILES` | `10` |
| `limits.repeated_tool_call_limit` | `REPEATED_TOOL_CALL_LIMIT` | `3` |

Supported providers are `ollama`, `openai`, and `openai-compatible`. API keys
remain exclusively in `AI_API_KEY` from the environment; `openai` requires a key
when building the provider. Base URLs must be HTTP(S) without embedded
credentials, query strings or fragments. Secrets/unknown fields, unsupported
providers, invalid timezones, non-mapping sections, incorrect YAML types,
non-finite numbers and out-of-range options fail closed without echoing runtime
values. Legacy profile saves preserve the runtime section; legacy profile-only
files remain valid and profile saves do not add a runtime section.

SQLite `runtime_settings` remains the shared, durable source for concurrency,
workspace concurrency, admission quotas, daily token quota and retention. These
already work independently of Chat/Dashboard through `JobStore.settings()` and
validated, atomic `JobStore.configure()`; startup does not overwrite them from
YAML. Execution limits above preserve the existing per-job validation/budget
rules. No database schema or migration changes are needed.

## MCP server lifecycle

MCP servers are configured separately in ignored `mcp.yaml`. Set
`SUTO_MCP_CONFIG` to its absolute path in `.env` to activate it; without that
setting, no MCP command starts, even if a `mcp.yaml` exists in the working
directory. The path must identify a regular, non-symlink file no larger than
32 KiB and must not be group or world writable on POSIX hosts. An explicitly
configured missing or malformed file fails the AI request closed.
CLI, settings, and API startup print one generic trust-boundary diagnostic when
`SUTO_MCP_CONFIG` is set; it contains no path, command, or secret.
Set file permissions with `chmod 600 mcp.yaml` on POSIX hosts.

`mcp.example.yaml` shows the stdio format. Each server needs a
`transport: stdio`, absolute executable `command`, optional `args` and `env`,
and `allow_tools`. The `env` values are names of existing host environment
variables; Suto resolves their values only when connecting and does not write
them to configuration or diagnostics. Put credentials in `.env` or the host
environment, never in command arguments or YAML values. Suto suppresses server
stderr and SDK transport logs that could quote malformed stdout because either
may contain private data. MCP argument values are omitted from progress and
tool audit events.

For each AI request, Suto starts only servers with tools still permitted by the
request's Skill and job restrictions. It initializes the MCP session, discovers
all tool pages, checks names and schemas, and registers explicitly enabled
tools as `mcp.<server>.<tool>`. Duplicate names within one
server and malformed or unsupported schemas reject that server's discovery;
other servers remain available. A connection failure likewise leaves that
server's tools unavailable and reports the server name without exception
details. Suto closes all connections and child processes at request end.

An MCP tool needs its original name in that server's `allow_tools` list. Skills
can narrow that list with the same namespaced names as native tools. Automation
jobs additionally need the namespaced name in their job-scoped tool allowlist.
The runtime validates arguments, checks the request restriction and permission
engine, and only then calls the MCP server. Server tools are not trusted just
because a connection succeeded. The configured stdio command runs as a host
process with the permissions of the Suto user and is not sandboxed; choose a
server whose own capabilities and filesystem access are appropriate. Phase 11
supports stdio
tools only, with no MCP resources, prompts, remote transport, or live tool-list
refresh.

## Database and worker lifecycle

Start the automation worker in its own terminal or host-managed service:

```bash
venv/bin/python main.py worker
```

This foreground process runs until SIGINT (Ctrl-C) or SIGTERM. It loads `.env`
through the normal entry point and uses `SUTO_DB_PATH` (default `data/suto.db`),
the shared runtime configuration and persisted operational limits. Start
the CLI separately with `venv/bin/python main.py`; both processes must use the
same database and configuration paths. The worker needs no user identity, conversation, chat input,
or Dashboard. It owns scheduling, job claims, AgentRuntime execution, retries,
recovery and durable results/notifications. Opening or closing a CLI session
does not start or stop the worker. Keep the worker's terminal/service running
after closing the CLI; exiting the worker's own terminal can still stop it.

Submit jobs, saved automations and schedules using the existing `/run`,
`/automation run` and `/schedule` commands in the CLI. Submissions, cancellations,
resumes and approval decisions commit through application services to SQLite;
the separate worker observes changes on its next poll (at most one second
while idle). No in-process wake or interactive session is required. Jobs remain
queued and schedules remain durable while the worker is stopped. Job availability
messages use the existing running heartbeat freshness check; they describe
recent worker availability, not a guarantee of execution. A duplicate worker
fails with `WORKER_UNAVAILABLE` before recovering or claiming another owner's
jobs, while additional CLI sessions remain usable.

Notifications are created atomically with job transitions even when every CLI
is closed. `SUTO_NOTIFY_CLI=1` still controls presentation/acknowledgement by a
CLI session; the standalone worker leaves inbox items unread for later delivery.
The public `/notifications` command lists unread inbox items without marking
them read; `/notifications ack <event_id>` acknowledges one exact item. Live
notification delivery remains optional with `SUTO_NOTIFY_CLI=1`.

CLI launches do not create identities, conversations or interactive agent runs.
Use `/run <task>` to queue work; `/clear`, `/reset`, `/task`, `/reminder` and
chat Skill commands are unavailable. Existing identities, conversations,
messages, summaries, memories, tasks, reminders and session Skills remain stored.
The compatibility `/runs` API retains its session ownership and history behavior.

Personal Memory tools (`memory.save/search/list/delete` and their aliases) are
no longer registered or assembled, and the executor no longer searches personal
memories for prompt context. `JobStore` exposes shared session-summary methods
through `SessionSummaryStore`, without personal memory CRUD. `SessionStore.compact`
still commits the summary and compaction cursor atomically without deleting raw
messages; `/runs` continues using those summaries after restart. No schema or
data migration accompanies this separation: existing personal memory rows, FTS
index/triggers, session summaries and run records remain intact.

The explicit `assistant.memory` models/store/service/tools and `MemoryRetriever`
imports remain legacy compatibility APIs, outside runtime registration.
`MemoryStore` still offers its original CRUD (including the legacy
`clear_user_memories` behavior that also clears summaries) only to callers that
explicitly compose it; `JobStore` no longer inherits it. `retrieval` loads
`MemoryRetriever` only on explicit access, so shared retrieval contracts and
`ContextManager` do not import personal memory. Installed/private extensions
have not been inventoried; callers of `JobStore` personal CRUD must adapt.
Job `options.retrieval` still selects the existing workspace-scoped index/search
tools with the same path, symlink, redaction, quota and staleness checks.

The unversioned Phase 0–7 database is schema 0. Startup migrates to version 1
(production controls) and version 2 (advanced features). Each version runs in a
transaction, is recorded in `schema_migrations`, and updates `PRAGMA user_version`.
A newer, unsupported database is rejected without changing its schema.
Version 14 adds `agent_runs` and `session_skills`. It preserves existing
conversations, messages, traces, jobs, and backups. The run table stores only
sanitized terminal text and bounded usage counters; active ownership is tied
to the host PID and Linux process start marker for crash recovery.

Existing databases receive a uniquely named, integrity-checked snapshot under
`<database directory>/backups/` before upgrade. Backups use the
[SQLite online backup API](https://www.sqlite.org/backup.html), include committed
WAL contents, use file mode 0600, and never overwrite an existing destination.
Stop the worker before upgrading. Keep backups on a separately managed durable
volume; backup files are not automatically deleted by retention cleanup.

```text
/backup /safe/path/suto-2026-09-11.db
/health
/diagnostics
```

`/health` and `/diagnostics` report database integrity, schema version, foreign-key
checks, worker heartbeat, limits, metrics, and optional dependency configuration.
`ok` describes database health; `ready` additionally requires a fresh running
worker heartbeat. Optional dependency presence does not prove that the host
permits namespaces.

A kernel advisory lock allows one worker process per database on a single Linux
host. Additional processes cannot recover or execute that worker's running jobs.
The owning worker supports a bounded async pool and atomic SQLite claims. The
lock is released on process exit; lock files should not be removed while a
worker may be running. SQLite files and these locks are not a distributed-worker
protocol and should not be shared across machines using a network filesystem.

The scheduler records each due occurrence as a durable job. The worker claims
that job and passes an `AgentRequest` with its job and schedule references through
the same agent runtime used for other requests. The job runner keeps workspace
permissions, budgets, approvals, retries, and results in the existing workflow
tables. Cancelling a running scheduled job cancels its active agent request.
Trigger creation, job creation or requeue, and schedule advancement commit
atomically so restart cannot create a second logical job for the same
occurrence. A retry requeues the same logical job and records another
trigger-history row; the next worker claim creates a new durable attempt.

Ctrl-C and SIGTERM directed at the worker stop job admission, cancel active
execution, kill command process trees and persist interrupted jobs, then release
the worker lock. CLI `/exit`, EOF and Ctrl-C affect only that CLI session. On a
hard crash, the next owning worker marks abandoned running jobs `interrupted`;
the resume path checks
workspace hashes before continuing. Use `/resume <job_id>` to explicitly resume
interrupted work; restart does not automatically replay it. Queued jobs, due
schedules and eligible schedule retries continue on worker startup using the
existing missed-run/retry policies. Interrupted/blocked jobs retain checkpoints.
Each worker claim atomically assigns a durable `attempt_id` and increments the
job's attempt count. Attempt status and start/end times survive restart; a new
claim after resumption receives a new ID while retaining the job ID. Internal
transient retries during one claim remain in that attempt. Databases upgraded
from earlier schemas retain existing jobs and counts without inventing missing
historical attempt records.

## Automation CLI (Phase 1)

Enter the following commands at the CLI prompt. Options for `/run` and
`/schedule create` come before the task; quote tasks with spaces. The workspace
defaults to the current directory and must resolve to an existing directory.
Write and command execution are denied by default. `--allow-write` and
`--allow-command` raise only the job's permission ceiling; each action still
passes the existing permission, approval, and sandbox checks.

```text
/run [--workspace <path>] [--allow-write] [--allow-command] <task>
/run --workspace . --allow-command "run tests and summarize failures"
/jobs
/status <job_id>
/cancel <job_id>
/resume <job_id>
```

`/run` persists a queued one-time job and reports its ID, canonical workspace,
permission ceilings, and whether the independent worker has a fresh heartbeat.
A saved job waits for an available worker; submission does not mean execution
has started. `/jobs`
lists recent jobs with their latest attempt IDs. `/status` reports the persisted
attempt count and ID, timestamps, automation version reference or schedule
trigger when present, result summary, and safe error. Cancellation is
idempotent for queued, running, and approval-waiting jobs. Resume accepts only
interrupted or blocked jobs after workspace checkpoint validation. The next
worker claim creates a new attempt ID under the same job ID. `/status` also
shows the durable `error_code` separately from its safe human-readable error;
older jobs without a recorded code show `none`.

`JobService.result(job_id)` exposes the same structured result snapshot: job and
attempt IDs, status, bounded result summary, error code and safe error message,
creation/start/finish timestamps, pinned automation version ID, schedule and
latest trigger IDs, and retry count. Codes identify failures at their owning
validation, permission, execution, or provider boundary; unclassified exceptions
use `INTERNAL_ERROR` without exposing exception details. Existing exception
types remain available to callers. Schema version 23 adds nullable job and
attempt error metadata without inferring codes for historical rows. A new claim
clears the current job error while preserving earlier attempt metadata. An
expired approval retains `APPROVAL_EXPIRED` while waiting for the next claim;
the job remains queued for a fresh approval. Quota rejection and unavailable
worker ownership surface codes at admission/startup without failing waiting jobs.

Execution and resume revalidate the canonical workspace pinned at submission.
Replacing that path with a symlink to another directory blocks the job with
`SANDBOX_VIOLATION` before provider or tool execution, including after restart.
Older jobs with relative workspace paths retain their existing resolution policy.
Tools also recheck that resolved root before access and after exact approval,
so replacing the directory during a model round or approval cannot redirect
an approved command into another directory.

When child jobs need attention, the blocked parent records the first failed
child's error code in creation order (then job ID), or `INTERNAL_ERROR` for
unclassified historical failures. The parent attempt retains the same safe
error metadata after restart.

```text
/schedule create (--at <ISO> | --every <seconds> | --cron <expr>)
    [--timezone <zone>] [--workspace <path>] [--allow-write]
    [--allow-command] [--missed-run <run_once|skip>]
    [--retry <count>] [--retry-delay <seconds>] <task>
/schedule create --every 3600 --retry 2 "inspect repository status"
/schedule create --cron "0 8 * * *" --timezone Asia/Bangkok "check daily status"
/schedule list
/schedule show <schedule_id>
/schedule pause <schedule_id>
/schedule resume <schedule_id>
/schedule history <schedule_id>
```

Cron occurrence lookup includes leap-day gaps, including the eight-year gap
across a non-leap century, so an accepted leap-day schedule can advance after
its current occurrence.

`--at` accepts an ISO date and time. An included offset sets the instant;
without one, the time is interpreted in `--timezone` or the runtime timezone
(legacy profile timezone if no runtime timezone is set).
Cron uses five fields in the selected timezone; `--every` is an interval in
seconds. The defaults are the runtime timezone, `run_once` for missed runs, zero
retries, and a 60-second retry delay. A retry requeues the same logical job and
records another trigger-history row; the next claim creates a new attempt.
For an overdue occurrence, `run_once` creates one recovery job and advances to
the next occurrence after the current time. `skip` records an occurrence more
than one second overdue as skipped and advances without a job. Pause stops
future triggers without cancelling jobs already queued or running. Use
`/schedule history <schedule_id>` to inspect trigger and retry rows.

A JSON automation definition accepts `name`, `prompt_template`, and optional
`format_version` (currently 1), `description`, `parameter_schema`, `workspace`,
`skills`, `allow_write`, and `allow_command`. `workspace` defaults to the CLI
working directory; both permissions default to false. Parameters can be
required or have defaults. Skill names must already exist. For a runnable
example, save this as `review.json`:

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

Edit the JSON file before `/automation update`; that command saves a new
immutable numbered version. `/automation run <name> [key=value ...]` validates
required and unknown parameters, skills, workspace, and the saved permission
ceiling before queuing a job. Values are parsed as JSON when valid JSON, or as
strings otherwise. Each job pins the exact version and validated parameter
snapshot, including defaults; updates do not change earlier jobs. Detected
structured secrets and known token patterns are rejected from definitions,
run parameters, `/run` prompts, and `/schedule create` prompts.

`/schedule automation <name>` saves a scheduled automation
snapshot. Place exactly one timing option (`--at`, `--every`, or `--cron`), then
optional `--timezone`, `--missed-run`, `--retry`, and `--retry-delay` options,
followed by unique `key=value` parameters. Values use the same JSON-or-string
parsing as `/automation run`. For example:

```text
/schedule automation repo-review --cron "0 8 * * *" --timezone Asia/Bangkok repo=suto
/schedule show <schedule_id>
/schedule upgrade <schedule_id> --automation-version latest repo=suto
```

Creation validates and atomically pins the current automation version,
parameters (including defaults), skill version IDs, workspace, and saved
permission ceiling. Workspace and permission flags cannot override that ceiling.
`/schedule list` and `/schedule show` display these schedules. Due occurrences
create jobs from the saved snapshot, including its exact version and skill
references. Later `/automation update` calls do not alter the schedule.
`/schedule upgrade` explicitly replaces the snapshot with a validated version;
use `latest` or a positive version number. Existing parameters are reused unless
new `key=value` parameters are supplied. If the new schema rejects them, the
schedule remains unchanged. Existing jobs keep their original version.

```text
/approvals
/approval show <approval_id>
/approval allow <approval_id>
/approval deny <approval_id>
```

`/approvals` lists pending job requests. `/approval show` displays the job and
attempt, requested action, tool, workspace, requested permission, timestamps,
and decision state. Allow queues the job for a new attempt; only that exact
request may be consumed after permission and sandbox checks. Deny blocks the
job. Expired and cancelled requests cannot authorize an action. An expired
decision queues the job to request fresh approval. Approval output redacts
recognized secrets and omits previews.

## Logs, metrics, quotas and retention

```text
/logs
/logs job_12345678
/metrics
/limits
/limits concurrency=2 workspace_concurrency=1
/limits max_queued_jobs=100 submissions_per_minute=30 daily_token_quota=1000000
/cleanup 30
/cleanup 30 --apply
/limits retention_days=30
```

Logs are persisted with `event_id`, `job_id`, type, time and redacted detail;
`/logs` emits JSON lines. File diffs, command output and approvals remain in
their existing audit tables. Metrics retain cumulative queue/run seconds,
tool-call/error counts, total tokens, current statuses, and daily token usage.
Success rate is completed / (completed + failed + blocked); an empty denominator
is reported as null. Times accumulate across attempts when states change.
Completed metrics are retained even after detailed logs are cleaned up.

Defaults: one running job, one running job per canonical workspace, 1,000 queued
jobs, 120 submissions/minute, and 10,000,000 reported tokens per UTC day. The
queue and submission checks run transactionally for CLI, automation, schedule
and subtask inserts. Workspace aliases resolve to the same concurrency scope.

Daily token quota gates admission and continued execution. Token usage is known
after provider responses; an in-flight response can exceed a token limit, and
concurrent in-flight requests can also overshoot the daily quota. These are
execution controls, not a guaranteed provider billing cap. Token counters do
not include separate embedding-provider usage; memory has its own bounded
entry size, count, response size and request timeout.

`/cleanup` previews counts unless `--apply` is supplied. Automatic cleanup is off
(`retention_days=0`); setting a positive value enables hourly checks. Cleanup
removes old progress/tool/command/change logs and notifications only for old
completed, failed or cancelled jobs, plus expired operator logs. It preserves
job summaries, plans, metrics, approvals, and all resumable-job checkpoints.
SQLite may reuse freed pages without immediately shrinking the database file.
Unread notifications remain available regardless of age; acknowledged
notifications follow the selected retention window. Cleanup also previews and
redacts expired rejected Skill proposal content while preserving its decision
history and repeated-workflow deduplication hash. Pending proposals, approved
proposals, and published Skills remain unchanged. Deleted proposals already
contain only their status tombstone and deduplication hash.

## Project retrieval

```text
/knowledge index /path/to/project
/knowledge search /path/to/project "schedule retry"
/run --workspace /path/to/project --retrieval inspect scheduling
/knowledge clear /path/to/project
```

The index uses SQLite FTS5 keyword ranking.
Incremental indexing stores hashes and bounded chunks with file/line citations.
`index_project` refreshes the job's index; `search_project` queries it. A search
rehashes candidate files and omits stale/deleted/symlinked results. Reindex after
edits to make the new contents searchable.

Hidden files/directories, `.env`, credential/secret filenames, repositories'
internal metadata, dependencies, and build/data directories are excluded.
Reads open every path component without following symlinks. Only supported text
extensions are indexed, up to 200 KB/file, 5,000 files, 20 MB and 20,000 chunks
per workspace. Exceeding a quota rolls back the refresh and preserves the old
index. This is bounded local project retrieval, not an unlimited repository or
distributed vector-search service.

## Subtasks, permissions and budgets

```text
/run --workspace /path/to/project --subtasks investigate the two modules
/subtasks job_12345678
/status job_12345678
/cancel job_12345678
```

The parent can queue up to eight children with stable idempotency keys. Children
are durable jobs with their own plan, status, approvals, results and audit trail.
They use the same workspace and sandbox, cannot broaden permissions, and cannot
delegate further. Writes and commands default off on each child, even if the
parent has those capabilities. Memory/retrieval capability is inherited.

Each child reserves tokens, tool calls, runtime and changed-file slots from the
parent's existing ceiling. Default reservation: 10,000 tokens, 8 tool calls,
120 seconds and 1 changed file. Reservations are conservative and are not
refunded; siblings and the parent's own work cannot reuse them. Child creation
fails if the remaining parent budget is insufficient.

The parent releases its worker slot as `waiting_children`; children pass through
the normal queue. Once all children complete, the parent is queued again with
their results in its checkpoint prompt. A failed/blocked/interrupted/cancelled
child blocks the parent. Cancelling a parent cancels its active children and
their command processes. Children of failed/blocked/cancelled parents are also
cancelled during reconciliation. Review `/subtasks` before resuming a blocked
parent; a cancelled or failed child is not silently recreated under the same key.

Per-job ceilings can be reduced at submission:

```text
/run --max-tokens 20000 --max-tool-calls 20 --max-seconds 300 --max-files 3 inspect the project
```

Limits and Phase 9 flags apply to `/run` jobs and their children. Existing
schedule/automation definitions retain their previous permission schema and
leave Phase 9 capabilities disabled.

## Stronger command sandbox

```text
/run --workspace /path/to/project --allow-command --sandbox bwrap run pytest
```

New jobs persist their selected sandbox mode explicitly; omitted configuration
means `process` for compatibility with older jobs. `process` preserves the
existing allowlist/process-group behavior and does not provide a mount or
network namespace. The optional
`bwrap` backend requires Linux, [Bubblewrap](https://github.com/containers/bubblewrap),
`prlimit`, and a host that permits unprivileged user namespaces. It fails closed
without falling back if the backend is missing or namespace creation fails.
`Sandbox.capability()` reports `available`, `unavailable`, `misconfigured`, or
`policy-disabled` before a required command starts. A namespace probe uses the
same unshare mode as command execution and never substitutes `process` isolation.
The selected backend is included in the exact command approval digest/preview.

The sandbox isolates process, network and mount namespaces, drops capabilities,
exposes system runtimes read-only, supplies private temporary storage, and mounts
only the selected workspace plus the Python runtime. `.env`, `.git`, `.ssh` and
`.aws` at the workspace root are masked. Workspace writes require the job's
write capability. Limits: 1 GiB virtual memory/process, 64 processes, 256 open
files, 16 MiB/file and CPU seconds equal to the command timeout. The existing
wall timeout and bounded output still apply; namespace teardown stops descendants.

This backend isolates approved commands, not the host AI client or embedding
requests. It is not a VM, and per-process resource limits are not cgroup-wide
aggregate accounting. Files legitimately exposed inside the selected workspace
and runtime are in scope for the approved command.

## Local web settings

Run `python3 main.py settings` to open Suto Settings in the
default browser. It binds only to `127.0.0.1:8765`.
The browser opens after the port is listening. If launching the browser fails,
use the private link printed in the terminal. Ctrl-C or SIGTERM closes the server.

Each launch generates a private sign-in link. Its fragment is exchanged for an
HttpOnly, SameSite=Strict session cookie and removed from browser history.
Keep that link private. Restarting the settings editor invalidates previous cookies.
APIs require that cookie; writes also require an exact local Origin and JSON
request header. Host validation rejects alternate hostnames. No remote binding,
public deployment or reverse-proxy mode is provided.

The Settings page edits validated `config.yaml` YAML, including runtime provider,
model, timezone, workspace and execution limits. `setting` remains an alias for
`settings`. There is no dashboard or saved-conversation endpoint. Opening the
editor does not access or create a user database.

Save becomes available when YAML changes and validates before writing. Unknown
fields, invalid timezones, malformed YAML and files over 32 KiB are rejected.
Secrets remain in `.env`.
The editor normalizes formatting and removes comments; a byte-level revision
check detects changes since load, including external edits. Failed validation or
conflicts preserve the draft and file; Reload requires confirmation for unsaved
edits. Persistence reuses the existing atomic settings writer. Restart relevant
Suto processes to apply saved settings; the editor never restarts them.

Opening the web page does not start AI, automation, or delivery workers.
Job notifications remain in the CLI; personal reminders are not delivered.

## Local notifications and verification

### Bounded agent delegation

The `agent.delegate` tool delegates one task to a `research`, `coding`, or
`review` child. It is available in interactive agent requests unless an active
Skill excludes it. Jobs must explicitly include `agent.delegate` in their
allowed tools. The child receives its task and optional selected context, not
the parent's conversation or memory. Coding jobs can delegate authorized MCP
tools only when the delegation names them explicitly; research and review
children cannot use MCP tools. Tool access is intersected with the parent's
registered tools, role, active Skill restrictions, and parent permission checks.
The same request-level authorization and underlying sandbox/approval path still
run for each child tool call.

One child may be spawned per parent run; children cannot delegate. Each child
has at most three model iterations, six tool calls, 30 seconds per model/tool
call, and 60 seconds overall, further bounded by the parent's limits. Parent
cancellation cancels the awaited child and closes the request's MCP manager.
The child's final status, text, usage, and run ID return as one tool observation.
Child model/tool activity and the parent delegation result are stored in the
existing `run_events` table, linked by `parent_run_id` and `child_run_id`.
Intermediate child messages do not enter the user session.

Lifecycle transitions to completed, failed, blocked, interrupted, cancelled or
waiting_approval create a durable local inbox item in the same transaction.
Each item has a `kind` for result delivery: `completed`, `failed`, `cancelled`,
`approval_required`, or `retry_exhausted` when the corresponding retry allowance
is used up. Existing `blocked` and `interrupted` items remain available. The
`status` field continues to show the job state, and `read_at` records an
acknowledgement. `/notifications` shows unread items; the store's
`notifications(unread_only=False)` query includes acknowledged items until
normal retention cleanup. Cleanup preserves unread items regardless of age and
removes acknowledged notifications past the selected retention cutoff.
Acknowledging an item twice has no effect. These
rows are derived from committed SQLite job transitions, independent of whether
the CLI is open.

```text
/notifications
/notifications ack 42
```

`SUTO_NOTIFY_CLI=1` prints unread completed, failed, cancelled, approval-required,
and retry-exhausted notifications when the CLI starts, then continues with new
notifications during that session. Each message includes the job ID and a fixed,
safe summary; job prompts, results, and errors are not printed. The CLI
acknowledges a shown item after printing it. Delivery reads the persistent
unread inbox in ID order, so items created while the CLI is closed appear on
the next start. An approval-required item is acknowledged without display if
the job has already left `waiting_approval`. If printing fails, the item remains
unread. If acknowledgement fails, the task retries without printing the item
again. Closing the CLI before acknowledgement leaves the item unread for the
next start. External delivery of these job-lifecycle notifications is not included.

```bash
venv/bin/pytest -q
venv/bin/pytest -q -rs tests/tools/test_sandbox.py
```

Tests cover migration rollback/backup, process locks, quotas, shutdown,
submit→approval→write→verification→completion, memory privacy/ranking, index
staleness, subtask joins/cancellation/budgets, and real namespace behavior.
Embedding protocol and semantic ranking use deterministic test doubles; a live
embedding model must be configured separately. Namespace tests skip when the
execution environment forbids namespace creation; run them on the deployment
host to validate filesystem/network isolation and descendant cleanup. On a
supported Linux host, install `bwrap` and `prlimit`, permit unprivileged user
namespaces, and run the command above outside a restrictive test container.
A skip means namespace isolation was not verified on that host. Run
`venv/bin/pytest -q -rs tests/tools/test_sandbox.py` directly on a supported
Linux host to exercise the integration tests. The `bwrap`
setting never falls back to `process` when setup or namespace creation fails.

## Draft Skill proposals (Phase 3D–3G)

Draft Skill proposals are stored separately from versioned Skills. A proposal
contains a validated name, draft instructions, and the IDs of 2–50 distinct,
completed jobs from one workspace. Job prompts, results, tool output, and
permissions are not copied into proposals. Detectable secrets in draft
instructions are rejected.

`/skill-proposal detect` checks one pinned saved automation version for at least
three completed runs across at least two UTC dates. It uses only job ID,
completion time, workspace, and saved automation identity. It does not read
prompts, results, parameters, conversation history, or indexed workspace
content. A proposal contains generic instructions, three completed source job IDs,
and an opaque workflow key used to prevent duplicate proposals. Detection does
not create or change a Skill or its permissions. Repeated detection returns the
existing proposal, including after restart.

The CLI supports `/skill-proposal detect`,
`/skill-proposal list [pending|approved|rejected|deleted|all]`,
`/skill-proposal show <id>`, and `/skill-proposal history <id>` for review.
`/skill-proposal approve <id>` explicitly saves a new versioned automation Skill
and records the approval event in the same SQLite transaction. Approval
revalidates the name and instructions, rejects tool or permission directives,
and fails if the Skill name already exists. A failure leaves the proposal pending
and creates no partial Skill. The new Skill is not added to an automation or
activated in a CLI session. Skills remain subject to existing tool registration,
job permissions, approval, and sandbox checks.

`/skill-proposal reject <id>` and `/skill-proposal delete <id>` do not create or
change a Skill. The state path is `pending` to `approved`, `rejected`, or
`deleted`; reviewed proposals may then become `deleted`. Every decision
transition has a durable event. Deletion clears the name, instructions, and
source job IDs while retaining a status and timestamp tombstone. `/cleanup [days]`
previews expired rejected proposal redaction and acknowledged
notification removal; `--apply` performs both atomically. The worker uses the
same retention window when `retention_days` is enabled. Pending proposals,
approved proposals, decision history, and repeated-workflow deduplication hashes
are preserved. Approval rechecks source job provenance and fails if stored
provenance is invalid or the source jobs are no longer completed in one
workspace. Detection never approves a proposal; every Skill publication still
uses the explicit `/skill-proposal approve <id>` path.
