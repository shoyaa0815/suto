# suto

Suto is a local-first AI harness for building automation agents. It provides a
shared runtime for tool-calling, mode-based permissions, request progress, and
token accounting.

The current `chat` mode is the interactive shell for exercising the harness
with web, date/time, and attached-document tools. The `agent` mode provides the
automation execution path with persistent jobs and restricted workspace tools.
Jobs are read-only by default and may receive explicit file-write or controlled
verification-command permission. Scheduling is intentionally not implemented
yet.

## Prerequisites

- Python 3.12 or newer
- [SearXNG](https://docs.searxng.org) running locally at `localhost:8080` (used by the `search_web` tool)

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Create a `.env` file in the project root for credentials, AI provider settings,
and runtime limits. The file is ignored by Git and must not be committed. All
settings are optional; omitted values use the defaults shown below. Restart
Suto after changing `.env` because settings are loaded when the process starts.

Ollama is the default provider:

```dotenv
AI_PROVIDER=ollama
AI_BASE_URL=http://localhost:11434
AI_MODEL=qwen3.5:9b
AI_API_KEY=
AI_TIMEOUT_SECONDS=300
AI_TEMPERATURE=0.2
```

To use OpenAI's Chat Completions API, set a supported model and keep the API key
only in your local `.env` file:

```dotenv
AI_PROVIDER=openai
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=your-model
AI_API_KEY=your-secret-key
```

For another service exposing an OpenAI-compatible Chat Completions endpoint,
use `AI_PROVIDER=openai-compatible` and set its base URL, model, and API key if
the service requires one. Suto appends `/chat/completions` automatically.
Existing `OLLAMA_URL`, `OLLAMA_MODEL`, `OLLAMA_TIMEOUT_SECONDS`, and
`OLLAMA_TEMPERATURE` values remain accepted for backward compatibility.

The remaining request, automation, storage, and diagnostic settings can be
added to the same `.env` file:

```dotenv
MAX_TOOL_ROUNDS=6
MAX_AGENT_TOOL_ROUNDS=20
MAX_LANGUAGE_CORRECTIONS=2
PROGRESS_INTERVAL_SECONDS=10

MAX_JOB_SECONDS=900
MAX_JOB_TOKENS=100000
MAX_TOOL_CALLS=40
MAX_CHANGED_FILES=10
REPEATED_TOOL_CALL_LIMIT=3

SUTO_DB_PATH=data/suto.db
SUTO_DEBUG=0
```

Client credentials such as `DISCORD_TOKEN`, `LINE_CHANNEL_SECRET`, and
`LINE_CHANNEL_ACCESS_TOKEN` belong in this same local `.env` file. Never put
real credentials in README, source code, commits, or GitHub.

## Run

Start the interactive shell in chat or automation mode:

```bash
venv/bin/python main.py chat cli
venv/bin/python main.py agent cli
```

In `agent cli` mode, type a task normally to run it as an automation job in the
current directory. Conversational jobs receive file-write and allowlisted
verification-command access, and the CLI waits for the job and prints its
result automatically:

```text
> fix the failing validation tests and update the documentation
```

Starting agent mode therefore grants tasks typed this way access to modify the
current workspace and run trusted-project verification code. Use `/run` when a
different workspace, read-only execution, or background submission is needed.

The CLI prints live user-facing request progress, including the current AI/tool
step, tool loop number, total and current-step elapsed time, and tokens
accumulated after each model response. A heartbeat is printed every 10 seconds
by default; set `PROGRESS_INTERVAL_SECONDS` to change it. Set `SUTO_DEBUG=1` to
enable raw developer timing logs.

### Automation jobs

The CLI includes a persistent single-worker automation queue:

```text
/run [--workspace <path>] [--allow-write] [--allow-command] <task>
                    create a background automation job
/jobs               list recent jobs
/status <job_id>    show progress, result, errors, and token usage
/plan <job_id>      show the job's current ordered plan and step statuses
/commands <job_id>  show commands executed by a job and their captured output
/changes <job_id>   show the files and unified diffs changed by a job
/cancel <job_id>    cancel a queued or running job
/resume <job_id>    safely resume an interrupted job from its checkpoint
```

Jobs and progress events are stored in `data/suto.db` by default. Set
`SUTO_DB_PATH` to use another database file. Queued jobs survive restarts. A job
left in `running` state by an interrupted process becomes `interrupted`;
`/resume` first validates the hashes of files changed by the earlier attempt,
preserves completed plan steps, and continues only when the checkpoint still
matches the workspace.

Automation jobs run in `agent` mode and may list, search, and read files only
inside the workspace assigned at submission. The default workspace is the
current directory. Add `--allow-write` to let that one job create or replace
text files. Existing files must be read first and are changed only when their
SHA-256 still matches, preventing stale writes. Writes use an atomic replace;
file deletion and arbitrary shell execution are unavailable.

Resolved paths and symlinks are checked to prevent access outside the
workspace. Every tool call records its arguments, status, elapsed time, result
size, and error. File content is omitted from tool-call arguments. Successful
writes additionally record the path, before/after hashes, and unified diff for
`/changes`.

For multi-step work, the agent can create a durable ordered plan, mark each step
as pending, in progress, completed, or failed, and replace the plan when new
information changes the approach. Plans survive restarts with their jobs. Use
`/plan <job_id>` to inspect the full plan; `/status <job_id>` shows the current
step. A job with an unfinished plan cannot be marked completed.

Add `--allow-command` to grant one job access to a small allowlist of verification
commands: `git status`, `git diff`, `pytest`, `python -m pytest`,
`python -m compileall`, and `ruff check`. Commands run without a shell, inside
the assigned workspace, with a scrubbed environment, timeout, and output limit.
Every execution records its arguments, status, exit code, elapsed time, stdout,
and stderr for `/commands`. `compileall` additionally requires `--allow-write`.

By default, each automation job is limited to 15 minutes, 100,000 accumulated
model tokens, 40 tool calls, and 10 distinct changed files. A third identical
tool call is treated as a loop. These defaults can be overridden in `.env`.
Jobs that hit one of the limits enter the `blocked` state with a persistent
reason. If a job changes files, it must run a successful `pytest`, `compileall`,
or `ruff` verification after the latest change before it can be marked
completed.

Transient AI connection failures and timeouts are retried up to 3 times with
1, 2, and 4 second backoff. Retries are allowed only when the failed attempt
did not change a file or run a command. Retry counts, cumulative token usage,
and progress are persisted in SQLite. Completed checkpoint steps cannot be
reopened accidentally.

Command permission should be granted only to trusted workspaces: `pytest` runs
the project's Python code, so an allowlist alone is not an operating-system
sandbox. Package installation, arbitrary Python scripts, shell pipelines,
redirects, background processes, and network tools are not available.

## Harness structure

- `main.py` — entry point that selects a harness mode
- `clients/cli/` — interactive terminal shell
- `ai/` — shared AI runtime, provider adapters, prompts, progress, tool loop, and
  token accounting
- `automation/` — execution context, job store, runner, and background worker
- `core/modes.py` — capability policies for interactive chat and automation
- `tools/` — tools the AI can call (each tool = handler + schema + prompt),
  including request-scoped documents and restricted workspace access
- `clients/cli/progress.py` — formatting for live CLI process status

Automation behavior, permissions, tools, and execution remain in the shared
runtime rather than the terminal shell.

## Modes

Choose one mode when starting the harness. The selected mode applies to the
entire process:

- `chat` — interactive harness mode for testing and using the current web,
  date/time, and attached-file tools.
- `agent` — persistent automation jobs with workspace listing, searching, and
  file reading. File writes require `--allow-write`; controlled verification
  commands require `--allow-command`; scheduling remains unavailable.

Tool access is enforced twice: the model only receives schemas allowed by the
selected mode, and the Python execution loop rejects any disallowed tool
call. Restart the process with a different first argument to change modes.
