"""Live job delivery uses the persisted notification inbox."""

import asyncio
import io
import sqlite3
import sys
import threading

import pytest

from interfaces.cli import backend, operations
from workflows.storage.store import JobStore


@pytest.mark.asyncio
async def test_plain_output_flush_failure_keeps_notification_unread(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("private prompt")
    assert store.cancel_job(job.id)
    attempted = asyncio.Event()
    acknowledged = asyncio.Event()
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_ack = store.acknowledge_notification

    class BufferedOutput(io.StringIO):
        allow_flush = False

        def flush(self):
            attempted.set()
            changed.set()
            if not self.allow_flush:
                raise OSError("output unavailable")
            super().flush()

    stream = BufferedOutput()

    def acknowledge(event_id):
        result = original_ack(event_id)
        loop.call_soon_threadsafe(acknowledged.set)
        loop.call_soon_threadsafe(changed.set)
        return result

    monkeypatch.setattr(store, "acknowledge_notification", acknowledge)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", stream)
        task = asyncio.create_task(operations.notify_cli(store, after_id=0))
        try:
            await asyncio.wait_for(changed.wait(), timeout=5)
            assert attempted.is_set()
            assert not acknowledged.is_set()
            assert len(JobStore(store.path).notifications()) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        stream.allow_flush = True
        attempted.clear()
        restarted = asyncio.create_task(operations.notify_cli(store, after_id=0))
        try:
            await asyncio.wait_for(acknowledged.wait(), timeout=5)
            assert attempted.is_set()
            assert JobStore(store.path).notifications() == []
        finally:
            restarted.cancel()
            await asyncio.gather(restarted, return_exceptions=True)


async def _wait_for_count(lines, count, changed):
    while len(lines) < count:
        changed.clear()
        if len(lines) < count:
            await asyncio.wait_for(changed.wait(), timeout=5)


@pytest.mark.asyncio
async def test_live_delivery_uses_typed_safe_summaries_and_acknowledges(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs.db")
    old = store.create_job("old secret=keep-private")
    assert store.cancel_job(old.id)
    cursor = store.latest_notification_id()
    lines = []
    acknowledged = []
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()

    original_ack = store.acknowledge_notification

    def acknowledge(event_id):
        result = original_ack(event_id)
        acknowledged.append(event_id)
        loop.call_soon_threadsafe(changed.set)
        return result

    def capture(message, **kwargs):
        lines.append(message)
        changed.set()

    monkeypatch.setattr(operations, "print", capture)
    monkeypatch.setattr(store, "acknowledge_notification", acknowledge)
    task = asyncio.create_task(operations.notify_cli(store, after_id=cursor))
    try:
        completed = store.create_job("secret=completed-private")
        store.claim_next_job()
        assert store.complete_job(completed.id, "secret=result-private", 0, 0)
        failed = store.create_job("secret=failed-private")
        store.claim_next_job()
        assert store.fail_job(failed.id, "secret=error-private")
        cancelled = store.create_job("secret=cancelled-private")
        assert store.cancel_job(cancelled.id)
        approval = store.create_job("secret=approval-private")
        store.claim_next_job()
        with store._connect() as db:
            db.execute("UPDATE jobs SET status='waiting_approval' WHERE id=?", (approval.id,))
        retry = store.create_job("secret=retry-private")
        store.claim_next_job()
        assert store.fail_job(retry.id, "secret=retry-error-private")
        with store._connect() as db:
            db.execute("UPDATE notifications SET kind='retry_exhausted' WHERE job_id=?", (retry.id,))

        await _wait_for_count(lines, 5, changed)
        await _wait_for_count(acknowledged, 5, changed)
        assert [line.split(": ", 1)[1] for line in lines] == [
            "Job completed.",
            "Job failed.",
            "Job cancelled.",
            "Approval required. Use /approvals to review.",
            "Job failed after retry allowance was exhausted.",
        ]
        assert all(job.id in line for job, line in zip(
            (completed, failed, cancelled, approval, retry), lines
        ))
        assert "private" not in "".join(lines)
        assert [item["job_id"] for item in store.notifications()] == [old.id]
        assert len(lines) == 5
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_restart_suppresses_stale_approval_request_and_acknowledges_it(
    tmp_path, monkeypatch
):
    path = tmp_path / "jobs.db"
    store = JobStore(path)
    job = store.create_job("private approval prompt")
    assert store.claim_next_job().id == job.id
    with store._connect() as db:
        db.execute("UPDATE jobs SET status='waiting_approval' WHERE id=?", (job.id,))
    assert store.cancel_job(job.id)
    events = list(reversed(store.notifications()))
    assert [event["kind"] for event in events] == ["approval_required", "cancelled"]
    lines = []
    acknowledged = []
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_ack = store.acknowledge_notification

    def acknowledge(event_id):
        result = original_ack(event_id)
        acknowledged.append(event_id)
        loop.call_soon_threadsafe(changed.set)
        return result

    def capture(message, **_kwargs):
        lines.append(message)
        changed.set()

    monkeypatch.setattr(store, "acknowledge_notification", acknowledge)
    monkeypatch.setattr(operations, "print", capture)
    task = asyncio.create_task(operations.notify_cli(store, after_id=0))
    try:
        await _wait_for_count(acknowledged, 2, changed)
        assert acknowledged == [event["id"] for event in events]
        assert len(lines) == 1 and "Job cancelled." in lines[0]
        assert "Approval required" not in lines[0]
        assert JobStore(path).notifications() == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_live_delivery_drains_concurrent_events_without_duplicates(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs.db")
    jobs = [store.create_job(f"event {index}") for index in range(110)]
    lines = []
    acknowledged = []
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()

    original_ack = store.acknowledge_notification

    def acknowledge(event_id):
        result = original_ack(event_id)
        acknowledged.append(event_id)
        loop.call_soon_threadsafe(changed.set)
        return result

    def capture(message, **kwargs):
        lines.append(message)
        changed.set()

    monkeypatch.setattr(operations, "print", capture)
    monkeypatch.setattr(store, "acknowledge_notification", acknowledge)
    task = asyncio.create_task(operations.notify_cli(
        store, after_id=store.latest_notification_id()
    ))
    try:
        assert all(await asyncio.gather(*[
            asyncio.to_thread(store.cancel_job, job.id) for job in jobs[:10]
        ]))
        for job in jobs[10:]:
            assert store.cancel_job(job.id)
        await _wait_for_count(lines, len(jobs), changed)
        await _wait_for_count(acknowledged, len(jobs), changed)
        assert len(lines) == len(set(lines)) == len(jobs)
        assert all(job.id in "".join(lines) for job in jobs)
        assert store.notifications() == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_live_delivery_retries_ack_without_reprinting(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs.db")
    lines = []
    acknowledged = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_ack = store.acknowledge_notification
    attempts = 0

    def acknowledge(event_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("temporary database lock")
        result = original_ack(event_id)
        loop.call_soon_threadsafe(acknowledged.set)
        return result

    monkeypatch.setattr(operations, "print", lambda message, **_: lines.append(message))
    monkeypatch.setattr(store, "acknowledge_notification", acknowledge)
    task = asyncio.create_task(operations.notify_cli(
        store, after_id=store.latest_notification_id()
    ))
    try:
        job = store.create_job("retry acknowledgement")
        assert store.cancel_job(job.id)
        await asyncio.wait_for(acknowledged.wait(), timeout=5)
        assert attempts == 2
        assert len(lines) == 1
        assert store.notifications() == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_notification_store_wait_does_not_block_event_loop():
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    class SlowStore:
        def notifications_after(self, _event_id):
            loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=5)
            return []

    task = asyncio.create_task(operations.notify_cli(SlowStore(), after_id=0))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        progressed = asyncio.Event()
        asyncio.get_running_loop().call_soon(progressed.set)
        await asyncio.wait_for(progressed.wait(), timeout=1)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cli_restart_delivers_multiple_offline_items_once_in_order(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    monkeypatch.setenv("SUTO_DB_PATH", str(path))
    monkeypatch.setenv("SUTO_NOTIFY_CLI", "1")
    lines = []
    acknowledged = []
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()

    original_ack = JobStore.acknowledge_notification

    def acknowledge(self, event_id):
        result = original_ack(self, event_id)
        acknowledged.append(event_id)
        loop.call_soon_threadsafe(changed.set)
        return result

    def capture(message, **kwargs):
        lines.append(message)
        changed.set()

    monkeypatch.setattr(operations, "print", capture)
    monkeypatch.setattr(JobStore, "acknowledge_notification", acknowledge)

    class IdleWorker:
        def __init__(self, *_args):
            self.stopped = asyncio.Event()

        async def start(self):
            await self.stopped.wait()

        async def stop(self):
            self.stopped.set()

    monkeypatch.setattr(backend, "AutomationWorker", IdleWorker)

    async def one_session(started, close):
        async def read_prompt():
            started.set()
            await close.wait()
            raise EOFError

        await backend.run_session("agent", read_prompt)

    started = asyncio.Event()
    close = asyncio.Event()
    first = asyncio.create_task(one_session(started, close))
    await asyncio.wait_for(started.wait(), timeout=5)
    store = JobStore(path)
    live = store.create_job("live")
    assert store.cancel_job(live.id)
    await _wait_for_count(lines, 1, changed)
    await _wait_for_count(acknowledged, 1, changed)
    close.set()
    await asyncio.wait_for(first, timeout=5)

    offline = [store.create_job(f"offline secret={index}") for index in range(3)]
    for job in offline:
        assert store.cancel_job(job.id)
    assert [item["job_id"] for item in JobStore(path).notifications()] == [
        job.id for job in reversed(offline)
    ]
    started = asyncio.Event()
    close = asyncio.Event()
    second = asyncio.create_task(one_session(started, close))
    await asyncio.wait_for(started.wait(), timeout=5)
    newer = store.create_job("newer")
    assert store.cancel_job(newer.id)
    await _wait_for_count(lines, 5, changed)
    await _wait_for_count(acknowledged, 5, changed)
    close.set()
    await asyncio.wait_for(second, timeout=5)

    assert live.id in lines[0]
    assert all(job.id in line for job, line in zip(offline, lines[1:4]))
    assert newer.id in lines[4]
    assert len(lines) == 5
    assert "secret" not in "".join(lines)
    assert JobStore(path).notifications() == []
    assert len(JobStore(path).notifications(unread_only=False)) == 5

    scanned = asyncio.Event()
    original_query = JobStore.notifications_after

    def query(self, event_id, *, limit=100):
        result = original_query(self, event_id, limit=limit)
        loop.call_soon_threadsafe(scanned.set)
        return result

    monkeypatch.setattr(JobStore, "notifications_after", query)
    started = asyncio.Event()
    close = asyncio.Event()
    third = asyncio.create_task(one_session(started, close))
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.wait_for(scanned.wait(), timeout=5)
    close.set()
    await asyncio.wait_for(third, timeout=5)
    assert len(lines) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["presentation", "acknowledgement"])
async def test_startup_delivery_failure_keeps_item_unread_for_restart(
    tmp_path, monkeypatch, failure
):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create_job("secret=private")
    assert store.cancel_job(job.id)
    failed = asyncio.Event()
    delivered = asyncio.Event()
    loop = asyncio.get_running_loop()
    lines = []
    original_ack = store.acknowledge_notification

    def capture(message, **_kwargs):
        if failure == "presentation" and not failed.is_set():
            failed.set()
            raise OSError("output unavailable")
        lines.append(message)

    def acknowledge(event_id):
        if failure == "acknowledgement" and not failed.is_set():
            loop.call_soon_threadsafe(failed.set)
            raise sqlite3.OperationalError("temporary database lock")
        result = original_ack(event_id)
        loop.call_soon_threadsafe(delivered.set)
        return result

    monkeypatch.setattr(operations, "print", capture)
    monkeypatch.setattr(store, "acknowledge_notification", acknowledge)
    first = asyncio.create_task(operations.notify_cli(store, after_id=0))
    try:
        await asyncio.wait_for(failed.wait(), timeout=5)
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

    assert [item["job_id"] for item in JobStore(store.path).notifications()] == [job.id]
    assert JobStore(store.path).notifications(unread_only=False)[0]["read_at"] is None

    second = asyncio.create_task(operations.notify_cli(store, after_id=0))
    try:
        await asyncio.wait_for(delivered.wait(), timeout=5)
    finally:
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
    assert len(lines) == (1 if failure == "presentation" else 2)
    assert all(job.id in line and "private" not in line for line in lines)
    assert JobStore(store.path).notifications() == []


@pytest.mark.asyncio
async def test_startup_query_and_new_notification_share_one_ordered_stream(
    tmp_path, monkeypatch
):
    store = JobStore(tmp_path / "jobs.db")
    offline = [store.create_job(f"offline {index}") for index in range(2)]
    for job in offline:
        assert store.cancel_job(job.id)

    entered = asyncio.Event()
    release = threading.Event()
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()
    lines = []
    acknowledged = []
    original_query = store.notifications_after
    original_ack = store.acknowledge_notification

    def query(event_id):
        if not entered.is_set():
            loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=5)
        return original_query(event_id)

    def acknowledge(event_id):
        result = original_ack(event_id)
        acknowledged.append(event_id)
        loop.call_soon_threadsafe(changed.set)
        return result

    def capture(message, **_kwargs):
        lines.append(message)
        changed.set()

    monkeypatch.setattr(store, "notifications_after", query)
    monkeypatch.setattr(store, "acknowledge_notification", acknowledge)
    monkeypatch.setattr(operations, "print", capture)
    task = asyncio.create_task(operations.notify_cli(store, after_id=0))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        live = store.create_job("live")
        assert store.cancel_job(live.id)
        release.set()
        await _wait_for_count(lines, 3, changed)
        await _wait_for_count(acknowledged, 3, changed)
        assert all(job.id in line for job, line in zip((*offline, live), lines))
        assert len(lines) == len(set(lines)) == 3
        assert JobStore(store.path).notifications() == []
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
