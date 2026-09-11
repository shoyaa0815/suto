# Operations and advanced features

## Database and worker lifecycle

The unversioned Phase 0–7 database is schema 0. Startup migrates to version 1
(production controls) and version 2 (advanced features). Each version runs in a
transaction, is recorded in `schema_migrations`, and updates `PRAGMA user_version`.
A newer, unsupported database is rejected without changing its schema.

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
permits namespaces or that an embedding model is serving requests.

A kernel advisory lock allows one worker process per database on a single Linux
host. Additional processes cannot recover or execute that worker's running jobs.
The owning worker supports a bounded async pool and atomic SQLite claims. The
lock is released on process exit; lock files should not be removed while a
worker may be running. SQLite files and these locks are not a distributed-worker
protocol and should not be shared across machines using a network filesystem.

Exit, Ctrl-C and SIGTERM stop job admission, cancel active execution, kill command
process trees and persist interrupted jobs. On a hard crash, the next owning
worker marks abandoned running jobs `interrupted`; `/resume <job_id>` checks
workspace hashes before continuing. Interrupted/blocked jobs retain checkpoints.

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

## Opt-in semantic memory

Memory is separated by canonical workspace. Enable it as the user, configure an
already available Ollama embedding model, and opt each job in independently:

```dotenv
SUTO_EMBED_URL=http://localhost:11434
SUTO_EMBED_MODEL=your-installed-embedding-model
```

```text
/memory on /path/to/project
/memory add /path/to/project "The API uses cursor pagination"
/memory search /path/to/project "How do we paginate results?"
/memory list /path/to/project
/run --workspace /path/to/project --memory review the API conventions
/memory delete /path/to/project mem_123456789abc
/memory off /path/to/project
```

The provider uses Ollama's [`/api/embed`](https://docs.ollama.com/api/embed).
Text is redacted before embedding and storage. Similarity uses normalized vector
dot products; endpoint/model identity and vector dimensions must match. Changing
the configured model excludes old embeddings from search until notes are added
using the new model. Limits: 500 notes/workspace, 4,000 characters/note or query,
4,096 dimensions, and 30 seconds/request. No model is downloaded automatically.

Agent `remember_memory` requires approval for the exact note; agents cannot
enable workspace memory. Turning memory off blocks retrieval and new saves but
keeps existing notes for user inspection/deletion. The configured embedding
endpoint receives the redacted note/query; use the default local endpoint when
keeping this data on the machine. Retrieval results are reference data, not
instructions or permission grants.

## Project retrieval

```text
/knowledge index /path/to/project
/knowledge search /path/to/project "schedule retry"
/run --workspace /path/to/project --retrieval inspect scheduling
/knowledge clear /path/to/project
```

The index uses SQLite FTS5 keyword ranking, separate from semantic memory.
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

`process` preserves the existing allowlist/process-group behavior. The optional
`bwrap` backend requires Linux, [Bubblewrap](https://github.com/containers/bubblewrap),
`prlimit`, and a host that permits unprivileged user namespaces. It fails closed
without falling back if the backend is missing or namespace creation fails.
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

## Local notifications and verification

Lifecycle transitions to completed, failed, blocked, interrupted, cancelled or
waiting_approval create a durable local inbox item in the same transaction.

```text
/notifications
/notifications ack 42
```

`SUTO_NOTIFY_CLI=1` prints live CLI notifications and acknowledges them after
printing. Delivery is at least once: a crash between printing and acknowledgement
can repeat a message. External Discord/LINE/email/webhook delivery is not included.

```bash
venv/bin/pytest -q
venv/bin/pytest tests/test_sandbox.py -q
```

Tests cover migration rollback/backup, process locks, quotas, shutdown,
submit→approval→write→verification→completion, memory privacy/ranking, index
staleness, subtask joins/cancellation/budgets, and real namespace behavior.
Embedding protocol and semantic ranking use deterministic test doubles; a live
embedding model must be configured separately. Namespace tests skip when the
execution environment forbids namespace creation; run them on the deployment
host to validate filesystem/network isolation and descendant cleanup.
