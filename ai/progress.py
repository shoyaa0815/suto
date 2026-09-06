import asyncio
import inspect
import time
from dataclasses import dataclass, field

from . import config
from .models import ProgressCallback


@dataclass
class RequestProgress:
    callback: ProgressCallback | None
    started: float
    max_rounds: int = config.MAX_TOOL_ROUNDS
    prompt_tokens: int = 0
    output_tokens: int = 0
    activity: str = "starting"
    detail: str = "starting request"
    round_number: int | None = None
    activity_started: float = field(default_factory=time.perf_counter)
    _heartbeat_task: asyncio.Task | None = field(default=None, init=False)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    def record_usage(self, data: dict) -> None:
        self.prompt_tokens += int(data.get("prompt_eval_count") or 0)
        self.output_tokens += int(data.get("eval_count") or 0)

    async def emit(
        self,
        activity: str | None = None,
        detail: str | None = None,
        round_number: int | None = None,
        heartbeat: bool = False,
    ) -> None:
        if not heartbeat and (activity is not None or detail is not None):
            self.activity_started = time.perf_counter()
        if activity is not None:
            self.activity = activity
        if detail is not None:
            self.detail = detail
        if round_number is not None:
            self.round_number = round_number
        if self.callback is None:
            return

        update = {
            "activity": self.activity,
            "detail": self.detail,
            "elapsed_seconds": time.perf_counter() - self.started,
            "activity_elapsed_seconds": time.perf_counter() - self.activity_started,
            "round": self.round_number,
            "max_rounds": self.max_rounds,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "heartbeat": heartbeat,
        }
        try:
            result = self.callback(update)
            if inspect.isawaitable(result):
                await result
        except Exception as error:
            print(f"[progress] callback failed: {error!r}")

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(config.PROGRESS_INTERVAL_SECONDS)
            await self.emit(heartbeat=True)

    def start(self) -> None:
        if self.callback is not None and config.PROGRESS_INTERVAL_SECONDS > 0:
            self._heartbeat_task = asyncio.create_task(self._heartbeat())

    async def stop(self) -> None:
        if self._heartbeat_task is None:
            return
        self._heartbeat_task.cancel()
        try:
            await self._heartbeat_task
        except asyncio.CancelledError:
            pass
