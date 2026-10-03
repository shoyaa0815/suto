# Operations and advanced features

## Voice interface

Run `venv/bin/python main.py voice` for discrete, push-to-record turns. Voice
uses its own local identity in the existing SQLite conversations and run tables;
`/session`, `/new`, and `/resume <id>` select sessions, and `/skills` plus
`/skill activate <name>` and `/skill deactivate <name>` persist selected Skill
names. Missing Skills block the next run until deactivated. The same executor,
tools, MCP restrictions, delegation, permission engine, and one-active-run
session reservation apply. Voice does not start automation workers.

Voice is disabled by default. Set `voice.stt_provider` and
`voice.tts_provider` to `openai` in `config.yaml`, and put `OPENAI_API_KEY` in
`.env` or the host environment. Recorded audio and the bounded spoken response
are sent to OpenAI when this provider is selected. The configured audio source
and sink can be `alsa` (using installed `arecord` and `aplay`) or `file` with
absolute WAV `input_path` and `output_path`. ALSA capture lasts at most 30
seconds; file input and generated audio are limited to 4 MiB. Startup prints
provider names and audio availability without keys or device IDs. File mode can
be used to inspect a generated WAV without speaker hardware. The executable
check cannot confirm that an ALSA device will open; capture or playback reports
a generic failure if the device is unavailable.

Use `/listen` to record one turn. During a turn, `/cancel` cancels the executor
task and any awaited child work and stops supported playback. An approval
request prints its ID; `/approve <id>` permits that single call and `/deny <id>`
denies it. Transcribed speech never submits approval. Requests expire through
the existing broker, and pending approvals vanish on cancellation or restart.
The text response remains available in the terminal. A short, simple, sanitized
response may be spoken; long, code-heavy, or potentially sensitive content gets
a fixed spoken notice instead. Failed runs speak a fixed error. A completed run
stays completed in SQLite if only TTS or playback fails afterward. There is no
wake word, always-listening process, voice authentication, or automatic crash
continuation.

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

The API passes an `AgentRequest` to the same AI executor as the CLI. The
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

## Host-managed settings

Non-secret local profile and new-user defaults live in `config.yaml`; secrets
remain in `.env`. Activate `venv` as shown in the README, then run
`python3 main.py setting` to open the local web
editor. It prints a private sign-in link, validates edits before saving, and
persists valid settings atomically. Restart Suto after saving so the CLI uses
the new values.
Existing reminder timestamps are not rewritten; newly created reminders use
the user's stored timezone.

YAML profile values take precedence over the legacy `SUTO_TIMEZONE`,
`SUTO_LOCALE`, and `SUTO_USER_NAME` environment defaults. Unknown sections,
unknown profile keys, unsupported config versions, invalid timezones, and
non-mapping YAML fail closed. `config.yaml` is local and ignored by Git;
`config.example.yaml` documents the versioned schema.

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

CLI conversations remain in the existing `conversations` and `messages` tables.
Each CLI launch starts a new conversation for the same local user and interface;
earlier conversations and run history remain stored but are not used as chat
context after reopening. Saved memories still carry across launches. Before
each prompt, the session service keeps up to 20 recent messages for model
context and condenses older messages into a bounded session summary. The summary
and its message cursor are stored together; the original messages remain
available in SQLite. Active Skill selection starts fresh with each CLI launch.
`/clear` deletes both messages and their summary for the current conversation,
then resets the visible CLI history to its startup view. It does not delete
saved memories, tasks, reminders, or other conversations;
`/reset all` removes saved conversations. Model context uses a 32,000-character
history budget and an 8,000-character summary budget. The deterministic summary
keeps excerpts of older turns, so details outside that budget can be omitted.

Explicitly saved user memories live separately from conversations in SQLite and
are indexed by FTS5. Searches are scoped to the active user and return ranked
matches; an empty query lists recent memories. Matching memories enter the
model request through the context manager with a separate 4,000-character,
five-result limit. Retrieved text is reference data, not instructions. Saving a
memory requires the memory tool; conversation turns are not saved automatically.
If the local memory index fails, the request reports that memory search is
unavailable before calling the model; the CLI remains available for another
request. It does not substitute unrelated recent memories.

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
The public `/schedule create` command accepts `--at`, `--every`, or `--cron`,
plus timezone, workspace permission, missed-run, and retry options. The other
public commands are `/schedule list`, `show`, `pause`, `resume`, and `history`.
The profile timezone is used when none is supplied. Pause stops future triggers
without cancelling jobs already queued or running. A retry requeues the same
logical job and records another trigger-history row; the next worker claim
creates a new durable execution attempt. Trigger creation, job creation or
requeue, and schedule advancement commit atomically so restart cannot create a
second logical job for the same occurrence. A `skip` occurrence more than one
second overdue is recorded as skipped; `run_once` creates one recovery job.

Exit, Ctrl-C and SIGTERM stop job admission, cancel active execution, kill command
process trees and persist interrupted jobs. On a hard crash, the next owning
worker marks abandoned running jobs `interrupted`; the resume path checks
workspace hashes before continuing. Interrupted/blocked jobs retain checkpoints.
Each worker claim atomically assigns a durable `attempt_id` and increments the
job's attempt count. Attempt status and start/end times survive restart; a new
claim after resumption receives a new ID while retaining the job ID. Internal
transient retries during one claim remain in that attempt. Databases upgraded
from earlier schemas retain existing jobs and counts without inventing missing
historical attempt records.

The public CLI accepts `/run [--workspace <path>] [--allow-write]
[--allow-command] <task>`, `/jobs`, `/status <job_id>`, `/cancel <job_id>`, and
`/resume <job_id>`. `/run` persists a queued one-time job and reports its ID,
canonical workspace, permission ceilings, and whether the session worker is
ready. A saved job waits for an available worker; submission does not mean the
job has started. `/jobs` lists recent jobs with their latest attempt IDs, and
`/status` reports persisted timestamps, result summary, and safe error.
Cancellation is idempotent, including queued and approval-waiting jobs. Resume
accepts interrupted or blocked jobs only after checking the existing workspace
checkpoint policy; the next worker claim creates a new attempt ID for the same
job. Write and command flags do not bypass tool approval or sandbox checks.
`/approvals` lists pending job requests. `/approval show <approval_id>` displays
the job and attempt, requested action, tool, workspace, permission, timestamps,
and decision state. `/approval allow <approval_id>` or `/approval deny
<approval_id>` decides only that pending request. Allow queues the job for a new
attempt; its exact action still passes the existing permission and sandbox
checks before execution. Deny blocks the job. Expired and cancelled requests
cannot authorize an action, and an expired decision queues the job to request
a new approval. Approval output redacts recognized secrets and omits previews.

The public `/automation create <definition.json>` and `/automation update <name>
<definition.json>` commands save immutable numbered definitions. A definition is a
JSON object with `name`, `prompt_template`, optional `description`,
`parameter_schema`, `workspace`, `skills`, `allow_write`, and `allow_command`.
Use `/automation list`, `/automation show <name>`, `/automation run <name>
[key=value ...]`, and `/automation history <name>` to inspect and run them.
Each run validates parameters, skills, workspace, and the saved permission ceiling
before queuing a job. The job pins the exact version and a validated parameter
snapshot, including defaults. Updating an automation does not change earlier jobs.
Detected structured secrets and known token patterns are rejected; values must
not be put in definition files or run parameters.

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

Run `python3 main.py setting` to open Suto Settings in the
default browser. It binds only to `127.0.0.1:8765`.
The browser opens after the port is listening. If launching the browser fails,
use the private link printed in the terminal. Ctrl-C or SIGTERM closes the server.

Each launch generates a private sign-in link. Its fragment is exchanged for an
HttpOnly, SameSite=Strict session cookie and removed from browser history.
Keep that link private. Restarting the settings editor invalidates previous cookies.
APIs require that cookie; writes also require an exact local Origin and JSON
request header. Host validation rejects alternate hostnames. No remote binding,
public deployment or reverse-proxy mode is provided.

The Dashboard tab lists saved CLI conversations for the local CLI identity from
`SUTO_DB_PATH` (default `data/suto.db`), including conversations with no chat
messages. Open a conversation to read its saved
user and assistant messages in order; long lists and threads load in pages.
Internal tool messages are hidden. The viewer is read-only, requires the same
sign-in cookie, and does not create a database when none exists. `/clear` removes
messages but leaves the empty conversation listed; `/reset all` removes the
conversations from this view.

The Settings tab edits the supported `config.yaml` profile fields. Save becomes available
when a field changes and validates before writing. Unknown fields, invalid timezones, malformed
YAML and files over 32 KiB are rejected. Secrets remain in `.env`.
The editor normalizes formatting and removes comments; a byte-level revision
check detects changes since load, including external edits. Failed validation or
conflicts preserve the draft and file; Reload requires confirmation for unsaved
edits. Persistence reuses the existing atomic settings writer. Restart relevant
Suto processes to apply saved settings; the editor never restarts them.

Opening the web page does not start AI, automation, or delivery workers.
AI chat and reminder delivery remain in the CLI.

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

```text
/notifications
/notifications ack 42
```

`SUTO_NOTIFY_CLI=1` prints live CLI notifications and acknowledges them after
printing. Delivery is at least once: a crash between printing and acknowledgement
can repeat a message. External delivery of these job-lifecycle notifications is
not included.

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
