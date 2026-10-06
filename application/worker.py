"""Long-lived automation process, independent of interactive sessions."""

import asyncio
import os
import signal

from application.modes import get_mode_policy
from workflows.runtime.runner import JobRunner
from workflows.runtime.worker import AutomationWorker
from workflows.storage.store import JobStore


async def run_worker(*, stop_event: asyncio.Event | None = None) -> None:
    store = JobStore(os.environ.get("SUTO_DB_PATH", "data/suto.db"))
    worker = AutomationWorker(store, JobRunner(store))
    loop = asyncio.get_running_loop()
    signals = []
    if stop_event is None:
        stop_event = asyncio.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop_event.set)
            signals.append(signum)

    worker_task = asyncio.create_task(worker.start())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        # Observe startup/loop failures even when no shutdown signal arrives.
        await asyncio.wait((worker_task, stop_task), return_when=asyncio.FIRST_COMPLETED)
    finally:
        try:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            await worker.stop()
            await worker_task
        finally:
            for signum in signals:
                loop.remove_signal_handler(signum)


def run(mode: str = "agent") -> None:
    get_mode_policy(mode)
    asyncio.run(run_worker())
