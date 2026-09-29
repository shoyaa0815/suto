"""Run-scoped, expiring approval handoff to an interactive interface."""

import asyncio
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    expires_at: datetime


@dataclass(frozen=True)
class ApprovalDecision:
    request_id: str
    choice: str  # allow_once or deny


class ApprovalBroker:
    """Only live requests can be decided; a restart creates an empty broker."""

    def __init__(self, on_request=None, *, timeout_seconds: float = 60) -> None:
        self.on_request = on_request
        self.timeout_seconds = timeout_seconds
        self.pending: dict[str, tuple[ApprovalRequest, asyncio.Future]] = {}

    async def request(self, run_id: str, tool_call_id: str, tool_name: str) -> tuple[ApprovalRequest, ApprovalDecision]:
        request = ApprovalRequest(
            uuid4().hex, run_id, tool_call_id, tool_name,
            datetime.now(UTC) + timedelta(seconds=self.timeout_seconds),
        )
        future = asyncio.get_running_loop().create_future()
        self.pending[request.id] = (request, future)

        async def deliver():
            if self.on_request is not None:
                result = self.on_request(request)
                if inspect.isawaitable(result):
                    callback = asyncio.create_task(result)
                    try:
                        done, _ = await asyncio.wait({callback, future},
                                                     return_when=asyncio.FIRST_COMPLETED)
                        if callback in done:
                            callback.result()
                    finally:
                        if not callback.done():
                            callback.cancel()
                            await asyncio.gather(callback, return_exceptions=True)
            return await future

        try:
            return request, await asyncio.wait_for(deliver(), self.timeout_seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if isinstance(asyncio.current_task(), asyncio.Task) and asyncio.current_task().cancelling():
                raise
            return request, ApprovalDecision(request.id, "deny")
        finally:
            self.pending.pop(request.id, None)

    def submit(self, request_id: str, choice: str) -> bool:
        entry = self.pending.get(request_id)
        if entry is None or choice not in {"allow_once", "deny"}:
            return False
        request, future = entry
        if datetime.now(UTC) >= request.expires_at or future.done():
            return False
        future.set_result(ApprovalDecision(request_id, choice))
        return True

    def cancel_run(self, run_id: str) -> None:
        for request, future in tuple(self.pending.values()):
            if request.run_id == run_id and not future.done():
                future.set_result(ApprovalDecision(request.id, "deny"))
