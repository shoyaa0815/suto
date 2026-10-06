"""Fresh CLI/API/Worker processes resolve the same host configuration."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from application.runtime_configuration import load_runtime_settings
from workflows.storage.store import JobStore


PROCESS = r'''
import asyncio
from contextlib import redirect_stdout
from dataclasses import asdict
from io import StringIO
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from ai import config, executor, response
from ai.providers import build_provider
from application.runtime_configuration import load_runtime_settings
from workflows.runtime.context import ExecutionLimits
from workflows.runtime.runner import APPROVAL_TTL_SECONDS
from workflows.storage.store import JobStore

runtime = load_runtime_settings()
provider = build_provider()
summary = {
    'runtime': asdict(runtime),
    'provider': [provider.name, provider.model, provider.url, provider.temperature],
    'timeout': config.AI_TIMEOUT_SECONDS,
    'rounds': [config.MAX_TOOL_ROUNDS, config.MAX_AGENT_TOOL_ROUNDS],
    'limits': asdict(ExecutionLimits()),
    'approval_ttl': APPROVAL_TTL_SECONDS,
}

def no_chat(*args, **kwargs):
    raise AssertionError('runtime opened a chat session')

JobStore.get_or_create_conversation = no_chat
JobStore.begin_agent_run = no_chat
kind = sys.argv[1]
if kind == 'cli':
    from interfaces.cli.commands import CommandContext, handle_command
    store = JobStore(sys.argv[2])
    context = CommandContext(store, SimpleNamespace(timezone='Pacific/Honolulu'), '', 'agent')
    with redirect_stdout(StringIO()):
        assert handle_command(context, '/run Report in English.').handled
        assert handle_command(context, '/schedule create --at 2030-01-01T09:00:00 Report.').handled
    summary['job'] = asdict(store.list_jobs()[0])
    summary['schedule'] = asdict(store.list_schedules()[0])
elif kind == 'api':
    from aiohttp.test_utils import make_mocked_request
    from interfaces.api.server import create_app, local_guard
    from interfaces.api.runtime import runtime_errors

    async def submit():
        app = create_app(database_path=sys.argv[2])
        transport = Mock()
        transport.get_extra_info.return_value = ('127.0.0.1', 8766)
        request = make_mocked_request('POST', '/jobs', app=app, transport=transport,
            headers={'Host': '127.0.0.1:8766', 'Content-Type': 'application/json', 'X-Suto-Request': '1'})
        request.json = AsyncMock(return_value={'prompt': 'Report in English.'})
        match = await app.router.resolve(request)
        match.add_app(app)
        request._match_info = match
        async def guarded(req):
            return await local_guard(req, match.handler)
        result = await runtime_errors(request, guarded)
        assert result.status == 202, result.text
        summary['job'] = json.loads(result.text)
    asyncio.run(submit())
elif kind == 'worker':
    from agent import AgentRuntime
    from application.worker import run_worker
    from llm.types import ModelResponse
    from tests.support.ai_helpers import FakeClientSession

    JobStore.resolve_channel_identity = no_chat
    calls = []
    async def generate(self, request):
        assert self.model == runtime.model
        assert self.temperature == runtime.options.temperature
        calls.append(request)
        return ModelResponse('Configured runtime completed.')
    type(provider).generate = generate
    class Session(FakeClientSession):
        def __init__(self, *args, timeout, **kwargs):
            assert timeout.total == runtime.options.timeout_seconds
    executor.aiohttp.ClientSession = Session
    response.detect_language_code = lambda text: 'en'
    run = AgentRuntime.run
    async def observed_run(self, request, *args, **kwargs):
        assert request.session_id is None
        assert self.limits.max_iterations == runtime.options.max_agent_tool_rounds
        assert self.limits.max_tool_calls == runtime.limits.max_tool_calls
        return await run(self, request, *args, **kwargs)
    AgentRuntime.run = observed_run

    async def work():
        stopped = asyncio.Event()
        complete = JobStore.complete_job
        count = 0
        def completed(self, *args, **kwargs):
            nonlocal count
            result = complete(self, *args, **kwargs)
            count += 1
            if count == 2:
                stopped.set()
            return result
        JobStore.complete_job = completed
        await asyncio.wait_for(run_worker(stop_event=stopped), timeout=5)
        assert count == len(calls) == 2
    asyncio.run(work())
    assert 'interfaces.cli' not in sys.modules
    assert 'interfaces.web' not in sys.modules
print(json.dumps(summary))
'''


def test_cli_api_and_headless_worker_share_file_config_and_execute(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(yaml.safe_dump({
        "profile": {"timezone": "UTC", "locale": "en"},
        "runtime": {
            "provider": "openai-compatible", "model": "host-model",
            "base_url": "http://localhost:1234/v1", "timezone": "Asia/Bangkok",
            "workspace": str(workspace),
            "options": {"temperature": 0.6, "timeout_seconds": 25,
                        "max_tool_rounds": 4, "max_agent_tool_rounds": 7,
                        "approval_ttl_seconds": 45},
            "limits": {"max_elapsed_seconds": 90, "max_tokens": 10_000,
                       "max_tool_calls": 5, "max_changed_files": 2,
                       "repeated_tool_call_limit": 2},
        },
    }))
    database = tmp_path / "jobs.db"
    monkeypatch.setenv("SUTO_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("SUTO_DB_PATH", str(database))
    monkeypatch.setenv("AI_API_KEY", "")
    monkeypatch.setenv("AI_MODEL", "legacy-environment-model")
    monkeypatch.setenv("SUTO_DEBUG", "")
    root = Path(__file__).resolve().parents[2]
    environment = {**os.environ, "PYTHONPATH": str(root)}
    summaries = []
    for kind in ("cli", "api", "worker"):
        process = subprocess.run(
            [sys.executable, "-c", PROCESS, kind, str(database)],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15,
        )
        assert process.returncode == 0, process.stderr
        summaries.append(json.loads(process.stdout))
    cli, api, worker = summaries
    for summary in summaries:
        assert summary["runtime"] == worker["runtime"]
        assert summary["provider"] == ["openai-compatible", "host-model",
                                      "http://localhost:1234/v1/chat/completions", 0.6]
        assert summary["timeout"] == 25
        assert summary["rounds"] == [4, 7]
        assert summary["limits"] == worker["runtime"]["limits"]
        assert summary["approval_ttl"] == 45
    assert cli["schedule"]["timezone"] == "Asia/Bangkok"
    assert cli["schedule"]["next_run_at"] == "2030-01-01T02:00:00+00:00"
    store = JobStore(database)
    for summary in (cli, api):
        job = store.get_job(summary["job"]["id"])
        assert job.workspace == str(workspace)
        assert not job.allow_write and not job.allow_command
        assert job.options == {"sandbox": "process"}
        assert job.status == "completed"
        assert job.result == "Configured runtime completed."
        assert job.attempt_count == 1
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


async def test_invalid_runtime_stops_worker_and_api_before_storage(tmp_path, monkeypatch):
    from application.worker import run_worker
    from interfaces.api.server import create_app

    path = tmp_path / "bad.yaml"
    path.write_text("runtime:\n  model: []\n")
    database = tmp_path / "must-not-exist.db"
    monkeypatch.setenv("SUTO_CONFIG_PATH", str(path))
    monkeypatch.setenv("SUTO_DB_PATH", str(database))
    with pytest.raises(ValueError, match="runtime.model"):
        create_app()
    with pytest.raises(ValueError, match="runtime.model"):
        await run_worker()
    assert not database.exists()


def test_workspace_override_and_pinned_automation_preserve_existing_state(tmp_path, monkeypatch):
    from application.automation import AutomationService, JobService, ScheduleService
    from workflows.models import ScheduleKind

    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text("runtime:\n  workspace: /missing/default\n  timezone: Asia/Bangkok\n")
    store = JobStore(tmp_path / "jobs.db")
    store.configure(concurrency=2, daily_token_quota=1234)
    job = JobService(store).submit("Inspect.", workspace=tmp_path)
    store.create_automation("pinned", "Inspect pinned workspace.", tmp_path, {})
    automation_job, version = AutomationService(store).run("pinned")
    schedule = ScheduleService(store).create(
        kind=ScheduleKind.ONCE, expression="2030-01-01T10:00:00",
        prompt="Inspect.", workspace=tmp_path, timezone="UTC",
    )
    assert job.workspace == automation_job.workspace == version.workspace == str(tmp_path)
    assert schedule.timezone == "UTC"
    assert schedule.workspace == str(tmp_path)
    assert store.settings()["concurrency"] == 2
    assert store.settings()["daily_token_quota"] == 1234
    assert load_runtime_settings().workspace == "/missing/default"
