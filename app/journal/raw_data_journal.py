from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import logging
import shutil
from typing import Any

from app.journal.jsonl_journal import JsonlJournal

RawEventObserver = Callable[[str, dict[str, Any], bool], None]
RawStateObserver = Callable[[str, dict[str, Any]], None]

logger = logging.getLogger(__name__)

MARKET_RAW_SAMPLE_INTERVAL_SECONDS = 10.0
MARKET_RAW_MAX_BYTES = 512 * 1024 * 1024
MARKET_RAW_MIN_FREE_BYTES = 1024 * 1024 * 1024


class RawDataJournal:
    """Raw journal with optional sampling and physical write budget.

    Sampling is persistence-only: callers still consume every market event before
    deciding whether the raw event should be retained on disk. Intentional sampling
    suppression is therefore not reported as a journal failure.

    The market stream is bounded by default because it is fed by tick-level eToro
    WebSocket updates. Other raw streams (notably finalized M1 candles) remain
    exhaustive unless an explicit policy is supplied.
    """

    def __init__(
        self,
        journal: JsonlJournal,
        observer: RawEventObserver,
        *,
        sample_interval_seconds: float | None = None,
        max_bytes: int | None = None,
        min_free_bytes: int | None = None,
        state_observer: RawStateObserver | None = None,
    ):
        self.journal = journal
        self.observer = observer
        market_stream = journal.stream_name == "market"
        if market_stream and sample_interval_seconds is None:
            sample_interval_seconds = MARKET_RAW_SAMPLE_INTERVAL_SECONDS
        if market_stream and max_bytes is None:
            max_bytes = MARKET_RAW_MAX_BYTES
        if market_stream and min_free_bytes is None:
            min_free_bytes = MARKET_RAW_MIN_FREE_BYTES
        self.sample_interval = (
            None
            if sample_interval_seconds is None
            else timedelta(seconds=max(0.0, sample_interval_seconds))
        )
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.state_observer = state_observer
        self.suppressed_count = 0
        self.budget_exhausted = False
        self.budget_reason: str | None = None
        self._last_written_at_by_symbol: dict[str, datetime] = {}

    def write(self, event_type: str, payload: dict[str, Any]) -> bool:
        if self._sampled_out(payload):
            self.suppressed_count += 1
            return True
        if self._budget_blocks_write():
            self.suppressed_count += 1
            return True
        written = self.journal.write(event_type, payload)
        if written:
            self._remember_sample(payload)
        self.observer(event_type, payload, written)
        return written

    def _sampled_out(self, payload: dict[str, Any]) -> bool:
        if self.sample_interval is None or self.sample_interval.total_seconds() <= 0:
            return False
        symbol = str(payload.get("symbol") or "").strip().upper()
        event_at = _event_at(payload)
        if not symbol or event_at is None:
            return False
        previous = self._last_written_at_by_symbol.get(symbol)
        if previous is None:
            return False
        return event_at - previous < self.sample_interval

    def _remember_sample(self, payload: dict[str, Any]) -> None:
        if self.sample_interval is None:
            return
        symbol = str(payload.get("symbol") or "").strip().upper()
        event_at = _event_at(payload)
        if symbol and event_at is not None:
            self._last_written_at_by_symbol[symbol] = event_at

    def _budget_blocks_write(self) -> bool:
        if self.budget_exhausted:
            return True
        reason = None
        path = self.journal.path
        if self.max_bytes is not None and path.exists():
            if path.stat().st_size >= self.max_bytes:
                reason = "max_bytes"
        if reason is None and self.min_free_bytes is not None:
            free = shutil.disk_usage(path.parent).free
            if free <= self.min_free_bytes:
                reason = "min_free_bytes"
        if reason is None:
            return False
        self.budget_exhausted = True
        self.budget_reason = reason
        payload = {
            "stream": self.journal.stream_name,
            "reason": reason,
            "max_bytes": self.max_bytes,
            "min_free_bytes": self.min_free_bytes,
            "path": str(path),
        }
        logger.warning("Raw journal budget exhausted | %s", payload)
        if self.state_observer is not None:
            self.state_observer("raw_journal_budget_exhausted", payload)
        return True


def _event_at(payload: dict[str, Any]) -> datetime | None:
    snapshot = payload.get("snapshot")
    value = getattr(snapshot, "received_at", None) or getattr(snapshot, "timestamp", None)
    if value is None and isinstance(snapshot, dict):
        value = snapshot.get("received_at") or snapshot.get("timestamp")
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
