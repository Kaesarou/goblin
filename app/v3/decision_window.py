from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.market.models import MarketSnapshot
from app.runtime.runtime_policy import DECISION_WINDOW_GRACE_SECONDS
from app.v3.features import OnlineFeatureSnapshot


@dataclass
class _DecisionWindow:
    closed_at: datetime
    expected_symbols: set[str]
    feature_by_symbol: dict[str, OnlineFeatureSnapshot]
    snapshot_by_symbol: dict[str, MarketSnapshot]
    quality_by_symbol: dict[str, bool]


@dataclass(frozen=True)
class V3DecisionWindowBatch:
    closed_at: datetime
    expected_symbols: tuple[str, ...]
    completed_symbols: tuple[str, ...]
    missing_symbols: tuple[str, ...]
    finalization_reason: str
    features: Mapping[str, OnlineFeatureSnapshot]
    snapshots: Mapping[str, MarketSnapshot]
    quality: Mapping[str, bool]


class V3DecisionWindowCoordinator:
    """Synchronize completed M1 states without candidate-domain coupling."""

    def __init__(self, *, grace_seconds: float = DECISION_WINDOW_GRACE_SECONDS) -> None:
        self.grace_seconds = float(grace_seconds)
        self._windows: dict[datetime, _DecisionWindow] = {}
        self._finalized: set[datetime] = set()

    def record(
        self,
        *,
        feature: OnlineFeatureSnapshot,
        snapshot: MarketSnapshot,
        quality_ok: bool,
        expected_symbols: set[str],
    ) -> bool:
        key = _utc(feature.asof)
        if key in self._finalized:
            return False
        window = self._windows.setdefault(
            key,
            _DecisionWindow(key, set(expected_symbols), {}, {}, {}),
        )
        window.expected_symbols.update(expected_symbols)
        window.feature_by_symbol[feature.symbol] = feature
        window.snapshot_by_symbol[feature.symbol] = snapshot
        window.quality_by_symbol[feature.symbol] = bool(quality_ok)
        return True

    def reset_symbol(self, symbol: str) -> None:
        for key, window in list(self._windows.items()):
            window.expected_symbols.discard(symbol)
            window.feature_by_symbol.pop(symbol, None)
            window.snapshot_by_symbol.pop(symbol, None)
            window.quality_by_symbol.pop(symbol, None)
            if not window.expected_symbols and not window.feature_by_symbol:
                self._windows.pop(key)

    def pop_ready(self, *, now: datetime) -> tuple[V3DecisionWindowBatch, ...]:
        actual_now = _utc(now)
        ready: list[tuple[datetime, str]] = []
        for key, window in self._windows.items():
            complete = window.expected_symbols.issubset(window.feature_by_symbol)
            expired = actual_now >= key + timedelta(seconds=self.grace_seconds)
            if complete:
                ready.append((key, "all_symbols_completed"))
            elif expired:
                ready.append((key, "grace_expired"))

        result: list[V3DecisionWindowBatch] = []
        for key, reason in sorted(ready):
            window = self._windows.pop(key)
            self._finalized.add(key)
            completed = set(window.feature_by_symbol)
            result.append(
                V3DecisionWindowBatch(
                    closed_at=key,
                    expected_symbols=tuple(sorted(window.expected_symbols)),
                    completed_symbols=tuple(sorted(completed)),
                    missing_symbols=tuple(sorted(window.expected_symbols - completed)),
                    finalization_reason=reason,
                    features=dict(window.feature_by_symbol),
                    snapshots=dict(window.snapshot_by_symbol),
                    quality=dict(window.quality_by_symbol),
                )
            )

        cutoff = actual_now - timedelta(days=1)
        self._finalized = {value for value in self._finalized if value >= cutoff}
        return tuple(result)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
