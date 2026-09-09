from types import SimpleNamespace
from datetime import UTC, datetime, timedelta

from app.market.models import Candle
from app.v3.runtime import GoblinV3Runtime


class _Journal:
    def __init__(self):
        self.events = []

    def write(self, event_type, payload):
        self.events.append((event_type, payload))


class _FeatureEngine:
    def __init__(self, last_opened_at):
        self._last_opened_at = last_opened_at

    def last_opened_at(self, symbol):
        return self._last_opened_at

    def update(self, candle):
        raise AssertionError("replayed candle must not reach feature update")


class _MustNotMutate:
    def on_base_candle(self, **kwargs):
        raise AssertionError("replayed candle must not reach MTF state")


def test_replayed_persisted_candle_is_skipped_before_feature_and_mtf_mutation():
    opened_at = datetime(2026, 9, 9, 15, 12, tzinfo=UTC)
    candle = Candle(
        symbol="BAYN.DE",
        timeframe_seconds=60,
        open=27.0,
        high=27.1,
        low=26.9,
        close=27.05,
        volume=None,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        sample_count=3,
    )
    quality = SimpleNamespace(
        carried_forward=False,
        last_price_age_seconds=0.1,
        degraded=False,
    )
    result = SimpleNamespace(candle=candle, quality=quality)

    runtime = object.__new__(GoblinV3Runtime)
    runtime.metrics = {"candle_session_rejections": 0, "candle_replay_skips": 0}
    runtime.feature_engine = _FeatureEngine(opened_at)
    runtime.multi_timeframe_service = _MustNotMutate()
    runtime.trade_journal = _Journal()
    runtime._session_at = lambda symbol, timestamp: SimpleNamespace(
        session_active=True,
        collect_snapshots=True,
        session_24_7=True,
        session_end_time=None,
    )

    runtime._process_closed_candle(
        "BAYN.DE",
        result,
        opened_at + timedelta(minutes=1, seconds=2),
        source="event",
    )

    assert runtime.metrics["candle_replay_skips"] == 1
    assert runtime.metrics["candle_session_rejections"] == 0
    assert runtime.trade_journal.events[0][0] == "v3_candle_replay_skipped"
    assert runtime.trade_journal.events[0][1]["symbol"] == "BAYN.DE"
    assert runtime.trade_journal.events[0][1]["last_processed_opened_at"] == opened_at
