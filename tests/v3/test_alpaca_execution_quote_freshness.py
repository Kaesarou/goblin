"""Executable Alpaca quotes must be fresh even if broader market-quality passes."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.market.models import MarketSnapshot
from app.v3.runtime import GoblinV3Runtime


def _runtime(bound):
    runtime = object.__new__(GoblinV3Runtime)
    runtime.live_market_data = (
        SimpleNamespace(max_entry_quote_age_seconds=5.0) if bound
        else SimpleNamespace()
    )
    return runtime


def _snapshot(now, *, broker_age=0, receipt_age=0):
    return MarketSnapshot(
        "AMAT", 475.0, 493.33, 484.165,
        now - timedelta(seconds=broker_age),
        received_at=now - timedelta(seconds=receipt_age),
    )


def test_alpaca_freshness_blocks_delayed_broker_quotes_and_backlogs():
    now = datetime.now(UTC)
    runtime = _runtime(True)
    assert runtime._entry_quote_fresh(_snapshot(now, broker_age=1, receipt_age=1), now=now)
    assert not runtime._entry_quote_fresh(
        _snapshot(now, broker_age=9.5, receipt_age=0), now=now,
    )
    assert not runtime._entry_quote_fresh(
        _snapshot(now, broker_age=24, receipt_age=9), now=now,
    )
    assert not runtime._entry_quote_fresh(
        _snapshot(now, broker_age=1, receipt_age=8), now=now,
    )


def test_eToro_unchanged_and_replay_clock_is_respected():
    historical = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)
    assert _runtime(False)._entry_quote_fresh(
        _snapshot(historical, broker_age=30), now=historical,
    )
    assert _runtime(True)._entry_quote_fresh(
        _snapshot(historical, broker_age=1), now=historical,
    )
    assert not _runtime(True)._entry_quote_fresh(
        _snapshot(historical, broker_age=30), now=historical,
    )
