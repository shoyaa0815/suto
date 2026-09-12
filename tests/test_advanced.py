import asyncio
import json

import pytest

import ai
from ai import AIExecutionResult
from automation.runtime.context import ApprovalRequired, ExecutionContext
from automation.runtime.options import validate_options
from automation.runtime.runner import JobRunner
from automation.storage.store import JobStore
from automation.runtime.worker import AutomationWorker
from clients.tui.backend import _parse_run
from tests.test_hardening import eventually
from tools.advanced import TOOL_NAMES, build_advanced_tools


class SemanticEmbedder:
    identity = 'fixture-semantic-model-v1'

    async def embed(self, text):
        # Deterministic semantic fixture: related concepts without shared words.
        return [1., 0., 0.] if any(word in text.lower() for word in ('car', 'vehicle', 'automobile')) else [0., 1., 0.]


async def test_semantic_memory_opt_in_scope_model_and_delete(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')
    one, two = tmp_path / 'one', tmp_path / 'two'
    one.mkdir()
    two.mkdir()
    embedder = SemanticEmbedder()
    with pytest.raises(PermissionError, match='disabled'):
        await store.remember(one, 'car', embedder=embedder)
    store.set_memory_enabled(one, True)
    store.set_memory_enabled(two, True)
    saved = await store.remember(one, 'car uses electricity; api_key=test-secret', embedder=embedder)
    await store.remember(one, 'garden flowers', embedder=embedder)
    await store.remember(two, 'private vehicle note', embedder=embedder)
    reopened = JobStore(store.path)
    matches = await reopened.recall(one, 'automobile', embedder=embedder)
    assert matches[0]['id'] == saved
    assert 'test-secret' not in matches[0]['content']
    assert all('private vehicle' not in row['content'] for row in matches)
    embedder.identity = 'different-model'
    assert await store.recall(one, 'car', embedder=embedder) == []
    store.set_memory_enabled(one, False)
    with pytest.raises(PermissionError):
        await store.recall(one, 'car', embedder=embedder)
    assert not store.forget_memory(two, saved)
    assert store.forget_memory(one, saved)
    assert all(row['id'] != saved for row in store.list_memories(one))


async def test_memory_disable_during_embedding_and_invalid_vectors(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')
    store.set_memory_enabled(tmp_path, True)

    class DisablingEmbedder(SemanticEmbedder):
        async def embed(self, text):
            store.set_memory_enabled(tmp_path, False)
            return [1., 0.]

    with pytest.raises(PermissionError, match='disabled while'):
        await store.remember(tmp_path, 'note', embedder=DisablingEmbedder())
    assert not store.list_memories(tmp_path)
    from automation.storage.knowledge import normalized_vector
    for value in ([float('nan')], [0., 0.], [float('inf')], [], [True]):
        with pytest.raises(ValueError):
            normalized_vector(value)


async def test_memory_tools_require_job_permission_and_approval(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')
    store.set_memory_enabled(tmp_path, True)
    job = store.create_job('save memory', workspace=str(tmp_path), options={'memory': True})
    store.claim_next_job()
    context = ExecutionContext(job.id, tmp_path, plan_store=store)
    with pytest.raises(PermissionError, match='not allowed'):
        await build_advanced_tools(context)['remember_memory']('note')

    def approval(kind, action, summary, preview):
        authorized, request = store.request_or_consume_approval(job.id, kind, action, summary, preview)
        if not authorized:
            raise ApprovalRequired(request.id, summary)

    context = ExecutionContext(job.id, tmp_path, plan_store=store, allowed_tools=TOOL_NAMES, approval_callback=approval)
    with pytest.raises(ApprovalRequired):
        await build_advanced_tools(context)['remember_memory']('note')
    assert store.get_job(job.id).status == 'waiting_approval'
    assert not store.list_memories(tmp_path)
    assert store.notifications()[0]['status'] == 'waiting_approval'


def test_project_index_incremental_citations_secrets_and_staleness(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')
    workspace = tmp_path / 'project'
    workspace.mkdir()
    source = workspace / 'source.py'
    source.write_text('def schedule_daily():\n    token="super-secret"\n    return 1\n')
    (workspace / '.env').write_text('api_key=hidden')
    (workspace / 'credentials.json').write_text('hidden')
    outside = tmp_path / 'outside.py'
    outside.write_text('outside secret')
    (workspace / 'escape.py').symlink_to(outside)
    report = store.index_workspace(workspace)
    assert report['files'] == 1
    assert store.index_workspace(workspace)['updated'] == 0
    hits = store.search_knowledge(workspace, 'schedule_daily')
    assert hits[0]['path'] == 'source.py' and hits[0]['line_start'] == 1
    assert '[REDACTED]' in hits[0]['content'] and 'super-secret' not in hits[0]['content']
    assert store.search_knowledge(tmp_path, 'schedule_daily') == []
    source.write_text('def schedule_hourly(): pass\n')
    assert store.search_knowledge(workspace, 'schedule_daily') == []
    assert store.index_workspace(workspace)['updated'] == 1
    assert store.search_knowledge(workspace, 'schedule_hourly')
    source.unlink()
    source.symlink_to(outside)
    assert store.search_knowledge(workspace, 'schedule_hourly') == []
    assert store.index_workspace(workspace)['removed'] == 1
    store.clear_knowledge(workspace)
    assert store.search_knowledge(workspace, 'schedule_hourly') == []


def test_index_quota_rolls_back_and_query_is_not_sql(tmp_path, monkeypatch):
    import automation.storage.knowledge as knowledge
    store = JobStore(tmp_path / 'jobs.db')
    (tmp_path / 'a.py').write_text('original')
    store.index_workspace(tmp_path)
    (tmp_path / 'b.py').write_text('extra')
    monkeypatch.setattr(knowledge, 'MAX_INDEX_FILES', 1)
    with pytest.raises(ValueError, match='quota'):
        store.index_workspace(tmp_path)
    assert store.search_knowledge(tmp_path, 'original')
    assert not store.search_knowledge(tmp_path, 'extra')
    store.search_knowledge(tmp_path, '" OR 1=1; DROP TABLE jobs; --')
    assert store.diagnostics()['ok']


def test_index_redacts_json_and_prefixed_environment_secrets(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')
    (tmp_path / 'config.json').write_text('{"password": "two secret words", "feature": "searchable"}')
    (tmp_path / 'config.txt').write_text('searchable\nCUSTOM_API_KEY=another-secret')
    store.index_workspace(tmp_path)
    hits = json.dumps(store.search_knowledge(tmp_path, 'searchable'))
    assert 'two secret words' not in hits and 'another-secret' not in hits
    assert hits.count('[REDACTED]') == 2


def test_subtasks_permission_budget_idempotency_and_no_nested_agents(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')
    parent = store.create_job('delegate', workspace=str(tmp_path), options={'subtasks': True, 'sandbox': 'bwrap'})
    store.claim_next_job()
    with pytest.raises(PermissionError, match='permissions'):
        store.create_subtask(parent.id, 'write', key='unsafe', allow_write=True)
    with pytest.raises(ValueError, match='budget'):
        store.create_subtask(parent.id, 'too much', key='large', max_tokens=100000)
    child = store.create_subtask(parent.id, 'inspect', key='inspect')
    assert child.parent_id == parent.id and child.workspace == parent.workspace
    assert child.options['sandbox'] == 'bwrap' and not child.options.get('subtasks')
    assert store.create_subtask(parent.id, 'inspect', key='inspect').id == child.id
    with pytest.raises(ValueError, match='different action'):
        store.create_subtask(parent.id, 'changed prompt', key='inspect')
    assert store.wait_for_children(parent.id)
    store.claim_next_job()
    with pytest.raises(PermissionError, match='unavailable'):
        store.create_subtask(child.id, 'grandchild', key='no')
    store.complete_job(child.id, 'found evidence', 1, 1)
    store.reconcile_children()
    assert store.get_job(parent.id).status == 'queued'


async def test_parent_children_join_and_results_survive_restart(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')
    prompts = []

    async def execute(prompt, **kwargs):
        context = kwargs['execution_context']
        job = store.get_job(context.job_id)
        if job.parent_id:
            assert 'create_subtask' not in context.allowed_tools
            return AIExecutionResult('child evidence', 'completed', None, 4, 2, .01)
        prompts.append(prompt)
        if job.attempt_count == 1:
            store.create_plan(job.id, ['Delegate', 'Merge evidence'])
            tool = build_advanced_tools(context)['create_subtask']
            tool(key='inspect', prompt='inspect source')
            store.update_step(job.id, 1, 'completed', 'delegated')
            return AIExecutionResult('delegated', 'completed', None, 3, 1, .01)
        assert 'child evidence' in prompt
        store.update_step(job.id, 2, 'completed', 'merged')
        return AIExecutionResult('merged report', 'completed', None, 3, 1, .01)

    runner = JobRunner(store, execute=execute)
    parent = store.create_job('investigate', workspace=str(tmp_path), options={'subtasks': True})
    await runner.run(store.claim_next_job())
    assert store.get_job(parent.id).status == 'waiting_children'
    store = JobStore(store.path)
    worker = AutomationWorker(store, JobRunner(store, execute=execute), poll_interval=.01)
    task = asyncio.create_task(worker.start())
    await eventually(lambda: store.get_job(parent.id).status == 'completed')
    await worker.stop()
    await task
    assert store.get_job(parent.id).result == 'merged report'
    assert store.children(parent.id)[0].status == 'completed'
    assert store.metrics()['tokens'] == 14
    assert len(prompts) == 2


async def test_parent_cancel_stops_running_child(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def execute(prompt, **kwargs):
        job = store.get_job(kwargs['execution_context'].job_id)
        if not job.parent_id:
            store.create_subtask(job.id, 'long child', key='child')
            return AIExecutionResult('delegated', 'completed', None, 0, 0, 0)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker = AutomationWorker(store, JobRunner(store, execute=execute), poll_interval=.01)
    parent = worker.submit('delegate', workspace=tmp_path, options={'subtasks': True})
    task = asyncio.create_task(worker.start())
    await asyncio.wait_for(started.wait(), 3)
    assert await worker.cancel(parent.id)
    await asyncio.wait_for(cancelled.wait(), 3)
    await worker.stop()
    await task
    assert store.get_job(parent.id).status == store.children(parent.id)[0].status == 'cancelled'


async def test_parent_budget_cannot_be_reused_after_delegation(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')

    async def execute(prompt, **kwargs):
        store.create_subtask(kwargs['execution_context'].job_id, 'child', key='child', max_tokens=90)
        return AIExecutionResult('claimed success', 'completed', None, 9, 9, .01)

    parent = store.create_job('delegate', workspace=str(tmp_path), options={'subtasks': True, 'max_tokens': 100})
    await JobRunner(store, execute=execute).run(store.claim_next_job())
    assert store.get_job(parent.id).status == 'blocked'
    assert 'reserved subtasks' in store.get_job(parent.id).error


async def test_exact_tool_budget_allows_final_summary(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')

    async def execute(prompt, **kwargs):
        context = kwargs['execution_context']
        assert context.budget_check(pending_tool=True) is None
        kwargs['tool_event_callback']({'tool_name': 'read_workspace_file', 'status': 'finished'})
        assert context.budget_check() is None
        assert 'tool budget' in context.budget_check(pending_tool=True)
        return AIExecutionResult('final summary', 'completed', None, 1, 1, .01)

    job = store.create_job('read one file', workspace=str(tmp_path), options={'max_tool_calls': 1})
    await JobRunner(store, execute=execute).run(store.claim_next_job())
    assert store.get_job(job.id).status == 'completed'


def test_cli_opt_in_and_invalid_limits(tmp_path):
    result = _parse_run(f'--workspace "{tmp_path}" --memory --retrieval --subtasks --sandbox bwrap '
                        '--max-tokens 2000 --max-seconds 60 inspect', include_options=True)
    assert result[-1] == {'memory': True, 'retrieval': True, 'subtasks': True, 'sandbox': 'bwrap',
                          'max_tokens': 2000, 'max_elapsed_seconds': 60}
    assert _parse_run('inspect', include_options=True)[-1] == {}
    for options in ({'memory': 'true'}, {'max_tokens': 0}, {'max_tokens': True}, {'sandbox': 'unsafe'}, {'unknown': True}):
        with pytest.raises(ValueError):
            validate_options(options)


async def test_advanced_tools_not_exposed_without_job_opt_in(tmp_path, monkeypatch):
    from tests.ai_helpers import FakeClientSession
    observed = []

    async def fake_chat(session, messages, schemas, think=False):
        observed.extend(item['function']['name'] for item in schemas)
        return {'message': {'content': 'done'}}

    monkeypatch.setattr(ai.client.aiohttp, 'ClientSession', FakeClientSession)
    monkeypatch.setattr(ai.client, 'chat', fake_chat)
    context = ExecutionContext('test', tmp_path)
    await ai.execute_local_ai('inspect', mode='developer', execution_context=context,
                              reply_language=ai.ReplyLanguage('en', 'English', 'test'))
    assert not set(observed) & TOOL_NAMES


async def test_ollama_embedding_protocol_handles_fragmented_response(monkeypatch):
    from automation.storage.knowledge import OllamaEmbeddings
    observed = {}

    class Body:
        async def iter_chunked(self, size):
            for part in (b'{"embeddings":', b'[[3.0,4.0]]}'):
                yield part

    class Response:
        status = 200
        content = Body()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session(Response):
        def __init__(self, **kwargs):
            pass

        def post(self, url, json):
            observed.update(url=url, payload=json)
            return Response()

    monkeypatch.setenv('SUTO_EMBED_MODEL', 'test-model')
    monkeypatch.setenv('SUTO_EMBED_URL', 'http://localhost:11434')
    monkeypatch.setattr('automation.storage.knowledge.aiohttp.ClientSession', Session)
    assert await OllamaEmbeddings().embed('vehicle') == [.6, .8]
    assert observed['url'] == 'http://localhost:11434/api/embed'
    assert observed['payload'] == {'model': 'test-model', 'input': 'vehicle', 'truncate': False}
