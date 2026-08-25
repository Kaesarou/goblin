from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.market.data_quality import MarketDataStatus
from app.market.models import Candle, MarketSnapshot
from app.market_data.models import MarketDataSource
from app.v3.features import OnlineFeatureSnapshot
from app.v3.models import DecisionBatch, DecisionReason, DecisionRecord
from app.v3.runtime import (
    GoblinV3Runtime,
    V3DecisionWindowCoordinator,
    _decision_quote_for_candle,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 25, 7, 0, tzinfo=UTC)


class RecordingJournal:
    def __init__(self):
        self.events = []

    def write(self, event_type, payload):
        self.events.append((event_type, payload))
        return True


def _feature(symbol: str, minute: int = 0) -> OnlineFeatureSnapshot:
    asof = NOW + timedelta(minutes=minute + 1)
    return OnlineFeatureSnapshot(
        symbol=symbol,
        asof=asof,
        candle_close=100.0,
        ema_lower=99.0,
        ema_upper=101.0,
        volatility_1m=0.01,
        volatility_1h=0.02,
        forager_volatility=0.03,
        activity=6.0,
        ema_readiness=0.01,
        features={},
    )


def _snapshot(symbol: str, bid: float, *, timestamp=NOW) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        bid=bid,
        ask=bid + 0.1,
        last=bid + 0.05,
        timestamp=timestamp,
    )


def _candle() -> Candle:
    return Candle(
        symbol="AAPL",
        timeframe_seconds=60,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=None,
        opened_at=NOW,
        closed_at=NOW + timedelta(minutes=1),
        sample_count=3,
    )


def _bare_runtime() -> GoblinV3Runtime:
    runtime = object.__new__(GoblinV3Runtime)
    runtime.trade_journal = RecordingJournal()
    runtime._decision_reason_counts = Counter()
    runtime._last_logged_decision_signature = {}
    runtime._market_quality_signature = {}
    runtime._maintenance_errors = set()
    return runtime


def test_decision_window_freezes_snapshot_at_candle_finalization():
    coordinator = V3DecisionWindowCoordinator(grace_seconds=5)
    first = _feature("AIR.PA")
    second = _feature("SAN.PA")
    first_snapshot = _snapshot("AIR.PA", 100.0)
    second_snapshot = _snapshot("SAN.PA", 50.0)

    assert coordinator.record(
        feature=first,
        snapshot=first_snapshot,
        quality_ok=True,
        expected_symbols={"AIR.PA", "SAN.PA"},
    )
    assert coordinator.record(
        feature=second,
        snapshot=second_snapshot,
        quality_ok=True,
        expected_symbols={"AIR.PA", "SAN.PA"},
    )

    batches = coordinator.pop_ready(now=first.asof)
    assert len(batches) == 1
    batch = batches[0]
    assert batch.finalization_reason == "all_symbols_completed"
    assert batch.snapshots["AIR.PA"].bid == 100.0
    assert batch.snapshots["SAN.PA"].bid == 50.0


def test_closed_candle_prefers_its_explicit_quote_over_future_latest_quote():
    candle = _candle()
    causal = _snapshot(
        "AAPL",
        100.2,
        timestamp=NOW + timedelta(seconds=50),
    )
    future = _snapshot(
        "AAPL",
        110.0,
        timestamp=NOW + timedelta(minutes=1, seconds=5),
    )
    result = SimpleNamespace(decision_snapshot=causal)

    selected, in_bucket = _decision_quote_for_candle(
        result=result,
        latest_snapshot=future,
        candle=candle,
    )
    assert selected == causal
    assert in_bucket
    assert selected != future


def test_future_quote_is_never_relabelled_when_closed_bucket_quote_is_missing():
    candle = _candle()
    future = _snapshot(
        "AAPL",
        110.0,
        timestamp=NOW + timedelta(minutes=1, seconds=5),
    )
    selected, in_bucket = _decision_quote_for_candle(
        result=SimpleNamespace(decision_snapshot=None),
        latest_snapshot=future,
        candle=candle,
    )
    assert selected is None
    assert not in_bucket


def test_older_quote_can_complete_carried_state_but_has_no_trade_authority():
    candle = _candle()
    older = _snapshot(
        "AAPL",
        99.5,
        timestamp=NOW - timedelta(seconds=5),
    )
    selected, in_bucket = _decision_quote_for_candle(
        result=SimpleNamespace(decision_snapshot=None),
        latest_snapshot=older,
        candle=candle,
    )
    assert selected == older
    assert not in_bucket


def test_material_decision_logging_is_transition_deduplicated():
    runtime = _bare_runtime()
    decision = DecisionBatch(
        decisions=(
            DecisionRecord(
                "AAPL",
                DecisionReason.ECONOMICS_REJECTED,
                NOW,
                {},
            ),
        )
    )

    runtime._journal_decision("AAPL", decision, ())
    runtime._journal_decision("AAPL", decision, ())
    assert [event for event, _ in runtime.trade_journal.events] == [
        "v3_inventory_decision"
    ]
    assert runtime._decision_reason_counts["economics_rejected"] == 2

    quiet = DecisionBatch(
        decisions=(
            DecisionRecord(
                "AAPL",
                DecisionReason.NO_OPPORTUNITY,
                NOW,
                {},
            ),
        )
    )
    runtime._journal_decision("AAPL", quiet, ())
    assert [event for event, _ in runtime.trade_journal.events] == [
        "v3_inventory_decision",
        "v3_inventory_decision_cleared",
    ]


def test_repeated_market_quality_rejection_logs_only_transition_and_recovery():
    runtime = _bare_runtime()
    for _ in range(20):
        runtime._record_market_quality_transition(
            symbol="AAPL",
            source=MarketDataSource.WEBSOCKET,
            status=MarketDataStatus.REJECTED,
            reasons=("snapshot_too_old",),
        )
    runtime._record_market_quality_recovery(
        symbol="AAPL",
        source=MarketDataSource.WEBSOCKET,
    )

    assert [event for event, _ in runtime.trade_journal.events] == [
        "v3_market_data_quality_changed",
        "v3_market_data_quality_recovered",
    ]


def test_maintenance_error_is_transition_deduplicated():
    runtime = _bare_runtime()
    for _ in range(10):
        runtime._record_maintenance_error(
            "equity_refresh",
            "v3_account_equity_error",
            {"message": "offline"},
        )
    runtime._clear_maintenance_error(
        "equity_refresh",
        "v3_account_equity_recovered",
        {"equity": 100_000.0},
    )
    assert [event for event, _ in runtime.trade_journal.events] == [
        "v3_account_equity_error",
        "v3_account_equity_recovered",
    ]
