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
```

Jobs and progress events are stored in `data/suto.db` by default. Set
`SUTO_DB_PATH` to use another database file. Queued jobs survive restarts; a job
left in `running` state by an interrupted process is marked failed on the next
start to avoid repeating future actions silently.

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

Each automation job is also limited to 15 minutes, 100,000 accumulated model
tokens, 40 tool calls, and 10 distinct changed files. A third identical tool
call is treated as a loop. Jobs that hit one of these limits enter the
`blocked` state with a persistent reason. If a job changes files, it must run a
successful `pytest`, `compileall`, or `ruff` verification after the latest
change before it can be marked completed.

Command permission should be granted only to trusted workspaces: `pytest` runs
the project's Python code, so an allowlist alone is not an operating-system
sandbox. Package installation, arbitrary Python scripts, shell pipelines,
redirects, background processes, and network tools are not available.

## Harness structure

- `main.py` — entry point that selects a harness mode
- `clients/cli/` — interactive terminal shell
- `ai.py` — shared AI runtime, tool loop, progress, and token accounting
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
