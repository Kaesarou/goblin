from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from queue import SimpleQueue
from threading import Lock
from typing import Any, Callable
from uuid import uuid4


class BrokerTaskLane(StrEnum):
    STANDARD = 'standard'
    CLOSE = 'close'
    QUERY = 'query'


CLOSE_TASK_KINDS = frozenset({'close_position'})
QUERY_TASK_KINDS = frozenset({'v3_close_execution_lookup', 'v3_broker_reconciliation'})


@dataclass(frozen=True)
class BrokerTaskCompletion:
    task_id: str
    kind: str
    lane: BrokerTaskLane
    context: Any
    value: Any = None
    error: Exception | None = None


class BrokerTaskRunner:
    """Run blocking broker work outside the market-data consumer.

    Standard account/order work, close mutations and read-only reconciliation are
    isolated on independent single-worker lanes. A slow GET/confirmation lookup
    can therefore never queue in front of a TP/SL close mutation. Runtime state is
    mutated only by callers draining structured completions on the main loop.
    """

    def __init__(self) -> None:
        self._executors = {
            BrokerTaskLane.STANDARD: ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix='goblin-broker-standard',
            ),
            BrokerTaskLane.CLOSE: ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix='goblin-broker-close',
            ),
            BrokerTaskLane.QUERY: ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix='goblin-broker-query',
            ),
        }
        self._pending: dict[
            str,
            tuple[str, BrokerTaskLane, Any, Future],
        ] = {}
        self._completed: SimpleQueue[BrokerTaskCompletion] = SimpleQueue()
        self._lock = Lock()
        self._closed = False

    def submit(
        self,
        *,
        kind: str,
        operation: Callable[[], Any],
        context: Any = None,
        task_id: str | None = None,
        lane: BrokerTaskLane | None = None,
    ) -> str:
        identifier = task_id or f'{kind}:{uuid4()}'
        actual_lane = lane or self._lane_for_kind(kind)
        with self._lock:
            if self._closed:
                raise RuntimeError('Broker task runner is closed.')
            if identifier in self._pending:
                raise ValueError(f'Duplicate broker task id: {identifier}')
            future = self._executors[actual_lane].submit(operation)
            self._pending[identifier] = (kind, actual_lane, context, future)
        future.add_done_callback(
            lambda completed, current_id=identifier: self._complete(
                current_id,
                completed,
            )
        )
        return identifier

    def has_pending_kind(
        self,
        kind: str,
        *,
        lane: BrokerTaskLane | None = None,
    ) -> bool:
        with self._lock:
            return any(
                item_kind == kind and (lane is None or item_lane == lane)
                for item_kind, item_lane, _, _ in self._pending.values()
            )

    def pending_count(self, *, lane: BrokerTaskLane | None = None) -> int:
        with self._lock:
            if lane is None:
                return len(self._pending)
            return sum(
                1
                for _, item_lane, _, _ in self._pending.values()
                if item_lane == lane
            )

    def drain(self) -> list[BrokerTaskCompletion]:
        completions: list[BrokerTaskCompletion] = []
        while not self._completed.empty():
            completions.append(self._completed.get())
        return completions

    def close(self, *, wait: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for executor in self._executors.values():
            executor.shutdown(wait=wait, cancel_futures=not wait)

    @staticmethod
    def _lane_for_kind(kind: str) -> BrokerTaskLane:
        if kind in CLOSE_TASK_KINDS:
            return BrokerTaskLane.CLOSE
        if kind in QUERY_TASK_KINDS:
            return BrokerTaskLane.QUERY
        return BrokerTaskLane.STANDARD

    def _complete(self, task_id: str, future: Future) -> None:
        with self._lock:
            pending = self._pending.pop(task_id, None)
        if pending is None:
            return
        kind, lane, context, _ = pending
        try:
            value = future.result()
        except Exception as exc:  # noqa: BLE001 - propagated as structured result
            self._completed.put(
                BrokerTaskCompletion(
                    task_id=task_id,
                    kind=kind,
                    lane=lane,
                    context=context,
                    error=exc,
                )
            )
            return
        self._completed.put(
            BrokerTaskCompletion(
                task_id=task_id,
                kind=kind,
                lane=lane,
                context=context,
                value=value,
            )
        )
