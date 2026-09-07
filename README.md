# Suto

Suto is a local-first AI agent for chatting with AI and working on projects. It
can read files, update code, and run tests. File changes and commands require
user approval before execution.

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

Start general chat mode:

```bash
venv/bin/python main.py chat cli
```

Start agent mode for project work:

```bash
venv/bin/python main.py agent cli
```

In agent mode, enter a task directly:

```text
> fix the failing validation and run the tests
```

Use `/run` to create a background job:

```text
/run inspect this project
/run --allow-write update the README
/run --allow-write --allow-command fix the code and run tests
/run --workspace /path/to/project inspect this project
```

`--allow-write` lets the agent propose file changes. `--allow-command` lets it
propose allowlisted commands. These flags grant capabilities but do not approve
individual actions.

When an action needs approval:

```text
/status <job_id>   review the proposed diff or command
/approve <job_id>  approve the action and continue the job
/reject <job_id>   reject the action and stop the job
```

An approval applies only to the displayed action, expires after a limited time,
and can be used once.

## Commands

```text
/help               show all commands
/jobs               list jobs
/status <job_id>    show job status and pending approval
/plan <job_id>      show the job plan
/commands <job_id>  show command results
/changes <job_id>   show changed files and diffs
/approve <job_id>   approve an action
/reject <job_id>    reject an action
/cancel <job_id>    cancel a job
/resume <job_id>    resume an interrupted job
/exit               exit Suto
```
