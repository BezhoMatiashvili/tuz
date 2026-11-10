"""One wall-clock and usage budget shared by orchestration and native research."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Lock
from typing import Any

_READS = ThreadPoolExecutor(max_workers=8, thread_name_prefix='chat-read')
_SLOTS = BoundedSemaphore(8)


class TurnBudget:
    def __init__(self, deadline: float, cancelled: Any, usage: dict) -> None:
        self.deadline, self.cancelled, self.usage = deadline, cancelled, usage
        self._lock = Lock()
        self.usage.setdefault('web_searches', 0)

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def check(self) -> None:
        if self.cancelled():
            raise RuntimeError('CANCELLED')
        if not self.remaining_seconds():
            raise TimeoutError('TURN_DEADLINE')

    def _take(self, key: str, limit: int, count: int = 1, *, scheduling: bool = True) -> None:
        if scheduling:
            self.check()
        with self._lock:
            if self.usage.get(key, 0) + count > limit:
                raise RuntimeError(f'{key.upper()}_EXHAUSTED')
            self.usage[key] = self.usage.get(key, 0) + count

    def take_model(self) -> None:
        self._take('model_requests', 6)

    def take_tool(self) -> None:
        self._take('tool_executions', 10)

    def take_search(self, count: int = 1) -> None:
        # Native searches may have completed immediately before transport timeout.
        # Accounting for work already consumed must survive the deadline itself.
        self._take('web_searches', 2, count, scheduling=False)

    def read(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        """Bound even an uncooperative sync read; late values cannot mutate turn state."""
        self.check()
        if not _SLOTS.acquire(blocking=False):
            raise RuntimeError('READ_CAPACITY')

        def run() -> Any:
            try:
                return function(*args, **kwargs)
            finally:
                _SLOTS.release()

        try:
            future = _READS.submit(run)
        except BaseException:
            _SLOTS.release()
            raise
        try:
            value = future.result(timeout=self.remaining_seconds())
        except TimeoutError:
            # A running transport owns its timeout; queued work is cancelled here.
            if future.cancel():
                _SLOTS.release()
            raise TimeoutError('TURN_DEADLINE') from None
        self.check()
        return value


class DeadlineRepository:
    """The existing read-only repository with a turn-scoped timeout on every read."""

    def __init__(self, repository: Any, budget: TurnBudget) -> None:
        self._repository, self._budget = repository, budget

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._repository, name)
        if callable(value):
            return lambda *args, **kwargs: self._budget.read(value, *args, **kwargs)
        return value
