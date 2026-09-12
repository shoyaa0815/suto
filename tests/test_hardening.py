import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai import AIExecutionResult
from automation.locking import ProcessLock
from automation.migrations import SCHEMA_VERSION
from automation.runner import JobRunner
from automation.store import JobStore
from automation.worker import AutomationWorker
from clients.tui.operations import handle_operations


async def eventually(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_migration_backup_and_newer_schema_refusal(tmp_path):
    path = tmp_path / 'legacy.db'
    # Construct the previous unversioned schema without performing new migrations.
    legacy = object.__new__(JobStore)
    legacy.path = path
    legacy._initialize()
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO jobs(id,prompt,mode,status,source,created_at) VALUES('legacy','keep me','agent','queued','cli','2026-01-01')")
    store = JobStore(path)
    assert store.get_job('legacy').prompt == 'keep me'
    backups = list((tmp_path / 'backups').glob('*.db'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0
        assert db.execute('SELECT prompt FROM jobs').fetchone()[0] == 'keep me'
    JobStore(path)
    assert len(list((tmp_path / 'backups').glob('*.db'))) == 1
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] == SCHEMA_VERSION
        db.execute('PRAGMA user_version=999')
    with pytest.raises(RuntimeError, match='newer'):
        JobStore(path)


def test_migration_rolls_back_failed_version(tmp_path, monkeypatch):
    import automation.migrations as migrations
    path = tmp_path / 'broken.db'
    monkeypatch.setattr(migrations, 'ADVANCED', migrations.ADVANCED + '\nINVALID SQL;')
    with pytest.raises(sqlite3.OperationalError):
        JobStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        assert 'options' not in {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='memories'").fetchone()


def test_online_backup_is_consistent_and_never_overwrites(tmp_path):
    store = JobStore(tmp_path / 'live.db')
    job = store.create_job('retained')
    target = store.backup(tmp_path / 'snapshot.db')
    assert target.stat().st_mode & 0o777 == 0o600
    assert JobStore(target).get_job(job.id).prompt == 'retained'
    with pytest.raises(FileExistsError):
        store.backup(target)
    with pytest.raises(ValueError):
        store.backup(store.path)


def test_database_connections_do_not_leak(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')
    baseline = len(os.listdir('/proc/self/fd'))
    for _ in range(200):
        store.list_jobs()
        store.settings()
    assert len(os.listdir('/proc/self/fd')) <= baseline + 2


def test_existing_database_permissions_are_hardened(tmp_path):
    database = tmp_path / 'jobs.db'
    database.touch(mode=0o644)
    database.chmod(0o644)

    JobStore(database)

    assert database.stat().st_mode & 0o777 == 0o600


def test_claim_is_atomic_and_workspace_limits_are_canonical(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')
    workspace = tmp_path / 'project'
    workspace.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(workspace, target_is_directory=True)
    store.configure(concurrency=4)
    first = store.create_job('first', workspace=str(workspace))
    store.create_job('same workspace', workspace=str(alias))
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = [job for job in pool.map(lambda _: store.claim_next_job(), range(8)) if job]
    assert [job.id for job in claimed] == [first.id]
    store.complete_job(first.id, 'done', 0, 0)
    assert store.claim_next_job() is not None


def test_admission_and_daily_quotas_are_persistent(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')
    store.configure(max_queued_jobs=1)
    job = store.create_job('one')
    with pytest.raises(sqlite3.IntegrityError, match='queue quota'):
        JobStore(store.path).create_job('two')
    store.claim_next_job()
    store.configure(submissions_per_minute=1)
    with pytest.raises(sqlite3.IntegrityError, match='rate limit'):
        store.create_job('two')
    store.configure(submissions_per_minute=100, daily_token_quota=10)
    store.complete_job(job.id, 'done', 9, 1)
    store.create_job('tomorrow')
    assert store.claim_next_job() is None
    assert store.metrics()['tokens_today'] == 10


def test_metrics_logs_notifications_and_retention(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')
    job = store.create_job('run')
    store.claim_next_job()
    store.add_event(job.id, {'activity': 'model', 'detail': 'api_key=not-a-real-secret'})
    store.add_tool_event(job.id, {'tool_name': 'read_workspace_file', 'status': 'failed', 'error': 'missing'})
    store.complete_job(job.id, 'done', 20, 3)
    interrupted = store.create_job('resume later')
    store.claim_next_job()
    store.add_change_event(interrupted.id, {'path': 'a.py', 'diff': 'keep checkpoint', 'after_sha256': 'hash'})
    store.interrupt_job(interrupted.id, 'restart')
    with store._connect() as db:
        db.execute("UPDATE jobs SET finished_at='2000-01-01T00:00:00+00:00'")
        for table in ('job_events', 'tool_events', 'change_events', 'structured_logs', 'notifications'):
            db.execute(f"UPDATE {table} SET created_at='2000-01-01T00:00:00+00:00'")
    log = json.dumps(store.logs(job.id))
    assert 'not-a-real-secret' not in log and '[REDACTED]' in log
    assert all(row['event_id'] and row['job_id'] == job.id for row in store.logs(job.id))
    metrics = store.metrics()
    assert metrics['tool_calls'] == metrics['tool_errors'] == 1
    assert metrics['tokens'] == 23 and metrics['success_rate'] == 1
    assert store.cleanup()['rows']['tool_events'] == 1
    assert store.tool_event_count(job.id) == 1
    store.cleanup(dry_run=False)
    assert store.tool_event_count(job.id) == 0
    assert store.change_event_count(interrupted.id) == 1
    assert store.metrics() == metrics


def test_notification_ack_survives_restart(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')
    job = store.create_job('run')
    store.claim_next_job()
    store.block_job(job.id, 'attention')
    item = store.notifications()[0]
    assert item['status'] == 'blocked'
    assert store.acknowledge_notification(item['id'])
    assert not store.acknowledge_notification(item['id'])
    assert not JobStore(store.path).notifications()


def test_worker_lock_excludes_another_process(tmp_path):
    path = tmp_path / 'worker.lock'
    lock = ProcessLock(path)
    assert lock.acquire()
    script = ('from pathlib import Path; from automation.locking import ProcessLock; '
              'import sys; lock=ProcessLock(Path(sys.argv[1])); '
              'sys.exit(0 if lock.acquire() else 7)')
    try:
        assert subprocess.run([sys.executable, '-c', script, str(path)]).returncode == 7
    finally:
        lock.release()
    assert subprocess.run([sys.executable, '-c', script, str(path)]).returncode == 0


async def test_worker_concurrency_restart_and_shutdown(tmp_path):
    store = JobStore(tmp_path / 'jobs.db')
    store.configure(concurrency=2)
    for name in ('a', 'b'):
        (tmp_path / name).mkdir()
    entered = set()

    async def execute(prompt, **kwargs):
        entered.add(kwargs['execution_context'].job_id)
        await asyncio.Event().wait()

    worker = AutomationWorker(store, JobRunner(store, execute=execute), poll_interval=.01)
    a = worker.submit('a', workspace=tmp_path / 'a')
    b = worker.submit('b', workspace=tmp_path / 'b')
    pending = worker.submit('a again', workspace=tmp_path / 'a')
    task = asyncio.create_task(worker.start())
    await eventually(lambda: len(entered) == 2)
    rival = AutomationWorker(JobStore(store.path), JobRunner(store))
    with pytest.raises(RuntimeError, match='another worker'):
        await rival.start()
    assert store.get_job(a.id).status == store.get_job(b.id).status == 'running'
    assert store.get_job(pending.id).status == 'queued'
    await worker.stop()
    await task
    assert store.get_job(a.id).status == store.get_job(b.id).status == 'interrupted'
    assert not store.diagnostics()['worker_alive']


async def test_submit_to_completion_integration(tmp_path):
    from tools.workspace import build_workspace_tools
    store = JobStore(tmp_path / 'jobs.db')
    (tmp_path / 'source.txt').write_text('verified input')

    async def execute(prompt, **kwargs):
        context = kwargs['execution_context']
        store.create_plan(context.job_id, ['Read source'])
        result = build_workspace_tools(context)['read_workspace_file']('source.txt')
        assert 'verified input' in result
        kwargs['tool_event_callback']({'tool_name': 'read_workspace_file', 'status': 'finished'})
        store.update_step(context.job_id, 1, 'completed', 'source inspected')
        return AIExecutionResult('verified report', 'completed', None, 4, 2, .01)

    worker = AutomationWorker(store, JobRunner(store, execute=execute), poll_interval=.01)
    job = worker.submit('inspect', workspace=tmp_path)
    task = asyncio.create_task(worker.start())
    await eventually(lambda: store.get_job(job.id).status == 'completed')
    await worker.stop()
    await task
    assert store.metrics()['tokens'] == 6
    assert store.notifications()[0]['status'] == 'completed'
    assert store.diagnostics()['ok']


async def test_operator_cli_validation_and_preview(tmp_path, capsys):
    store = JobStore(tmp_path / 'jobs.db')
    assert await handle_operations(store, '/limits', 'concurrency=2')
    assert store.settings()['concurrency'] == 2
    await handle_operations(store, '/cleanup', '30')
    await handle_operations(store, '/health', '')
    await handle_operations(store, '/limits', 'concurrency=0')
    output = capsys.readouterr().out
    assert '"dry_run": true' in output and '"ok": true' in output
    assert 'invalid runtime setting' in output


async def test_worker_write_approve_verify_complete(tmp_path):
    from automation.context import ApprovalRequired
    from tools.command import build_command_tools
    from tools.workspace import build_workspace_tools
    store = JobStore(tmp_path / 'jobs.db')

    async def execute(prompt, **kwargs):
        context = kwargs['execution_context']
        try:
            if not (tmp_path / 'test_generated.py').exists():
                build_workspace_tools(context, kwargs['change_event_callback'])['apply_workspace_patch'](
                    'test_generated.py', 'def test_generated():\n    assert 2 + 2 == 4\n')
            output = await build_command_tools(context, context.command_event_callback)['run_workspace_command'](
                ['pytest', '-q', 'test_generated.py'])
            assert '1 passed' in output
        except ApprovalRequired as error:
            return AIExecutionResult(str(error), 'waiting_approval', str(error), 2, 1, .01)
        return AIExecutionResult('created and verified', 'completed', None, 2, 1, .01)

    worker = AutomationWorker(store, JobRunner(store, execute=execute), poll_interval=.01)
    job = worker.submit('create test', workspace=tmp_path, allow_write=True, allow_command=True)
    task = asyncio.create_task(worker.start())
    try:
        await eventually(lambda: store.get_job(job.id).status == 'waiting_approval')
        assert not (tmp_path / 'test_generated.py').exists()
        assert worker.approve(job.id)[0]
        await eventually(lambda: store.get_job(job.id).status == 'waiting_approval')
        assert (tmp_path / 'test_generated.py').exists()
        assert store.command_event_count(job.id) == 0
        assert worker.approve(job.id)[0]
        await eventually(lambda: store.get_job(job.id).status == 'completed')
    finally:
        await worker.stop()
        await task
    assert store.get_job(job.id).result == 'created and verified'
    assert store.has_successful_verification_after_last_change(job.id)
    assert [item['status'] for item in reversed(store.notifications())] == [
        'waiting_approval', 'waiting_approval', 'completed']
