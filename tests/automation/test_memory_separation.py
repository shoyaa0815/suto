"""Personal memory is absent from Jobs; workspace retrieval remains executable."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from ai.execution import loop
from application.modes import ASSISTANT_MEMORY_TOOLS
from assistant.memory.store import MemoryStore
from llm.types import ModelResponse, ToolCall
from tests.support.ai_helpers import FakeClientSession
from tools import ALL_TOOLS, ALL_TOOL_SCHEMAS, ALL_TOOL_GUIDANCE, get_tools
from workflows.runtime.runner import JobRunner
from workflows.storage.store import JobStore


@pytest.fixture
def legacy_database(tmp_path):
    class LegacyStore(MemoryStore, JobStore):
        pass

    old = LegacyStore(tmp_path / "suto.db")
    user = old.resolve_channel_identity("cli", "local")
    session = old.get_or_create_conversation(user.id, "cli", "old")
    old.save_session_summary(session.id, user.id, "legacy session summary")
    old.save_memory(user.id, "schedule_daily PRIVATE_PERSONAL_FACT", "preference")
    # Reopen with the runtime's composition, rather than the legacy opt-in mixin.
    return JobStore(old.path)


def personal_rows(store):
    with store._connect() as db:
        return {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
            for table in ("assistant_memories", "assistant_memories_fts", "session_summaries")
        }


def test_runtime_imports_do_not_load_personal_memory():
    script = """
import importlib.abc
import sys

class RejectPersonalMemory(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'assistant.memory' or fullname.startswith('assistant.memory.') or fullname == 'retrieval.memory':
            raise AssertionError('runtime imported personal memory: ' + fullname)

sys.meta_path.insert(0, RejectPersonalMemory())
from workflows.storage.store import JobStore
from ai import execute_local_ai
from context import ContextManager
from retrieval import Retriever, RetrievalResult, RetrievalError
from tools import get_tools
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_runtime_store_and_registry_have_no_personal_crud(legacy_database):
    store = legacy_database
    for name in ("save_memory", "search_memories", "list_memories", "delete_memory", "clear_user_memories"):
        assert not hasattr(store, name)
    for registry in (ALL_TOOLS, ALL_TOOL_SCHEMAS, ALL_TOOL_GUIDANCE):
        assert not ASSISTANT_MEMORY_TOOLS & registry.keys()
    with pytest.raises(ValueError, match="unknown tools"):
        get_tools(ASSISTANT_MEMORY_TOOLS)
    assert len(personal_rows(store)["assistant_memories"]) == 1


def configure_model(monkeypatch, model):
    monkeypatch.setattr("ai.executor.aiohttp.ClientSession", FakeClientSession)
    monkeypatch.setattr(loop, "build_model_router", lambda session: model)
    monkeypatch.setattr("ai.response.detect_language_code", lambda text: "en")


async def test_job_executes_workspace_retrieval_without_personal_injection(
    tmp_path, monkeypatch, legacy_database,
):
    store = legacy_database
    before = personal_rows(store)
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "source.py").write_text("def schedule_daily(): return 'WORKSPACE_EVIDENCE'\n")
    requests = []

    class Model:
        async def generate(self, request):
            requests.append(request)
            assert "PRIVATE_PERSONAL_FACT" not in str(request.messages)
            assert "legacy session summary" not in str(request.messages)
            assert not ASSISTANT_MEMORY_TOOLS & {
                tool["function"]["name"] for tool in request.available_tools
            }
            if len(requests) == 1:
                return ModelResponse(None, [ToolCall("index_project", {})])
            if len(requests) == 2:
                assert json.loads(request.messages[-1]["content"])["files"] == 1
                return ModelResponse(None, [ToolCall("search_project", {"query": "schedule_daily"})])
            hits = json.loads(request.messages[-1]["content"])
            assert hits[0]["path"] == "source.py"
            assert hits[0]["line_start"] == 1
            assert "WORKSPACE_EVIDENCE" in hits[0]["content"]
            return ModelResponse("Workspace evidence verified.")

    configure_model(monkeypatch, Model())
    job = store.create_job("Inspect schedule_daily in English.", workspace=str(workspace), options={"retrieval": True})
    await JobRunner(store).run(store.claim_next_job())
    reopened = JobStore(store.path)
    assert reopened.get_job(job.id).status == "completed"
    assert reopened.get_job(job.id).result == "Workspace evidence verified."
    assert {
        event.tool_name for event in reopened.list_tool_events(job.id)
        if event.status == "finished"
    } == {"index_project", "search_project"}
    assert reopened.search_knowledge(workspace, "schedule_daily")
    assert reopened.search_knowledge(tmp_path, "schedule_daily") == []
    assert personal_rows(reopened) == before
    assert len(requests) == 3


@pytest.mark.parametrize("name", sorted(ASSISTANT_MEMORY_TOOLS))
async def test_job_rejects_personal_memory_calls_without_mutating_legacy_data(
    tmp_path, monkeypatch, legacy_database, name,
):
    store = legacy_database
    before = personal_rows(store)

    class Model:
        async def generate(self, request):
            return ModelResponse(None, [ToolCall(name, {})])

    configure_model(monkeypatch, Model())
    job = store.create_job("Try a personal memory tool.", workspace=str(tmp_path), options={"retrieval": True})
    await JobRunner(store).run(store.claim_next_job())
    reopened = JobStore(store.path)
    assert reopened.get_job(job.id).status == "blocked"
    assert reopened.get_job(job.id).result is None
    assert not any(event.status == "finished" for event in reopened.list_tool_events(job.id))
    assert personal_rows(reopened) == before
