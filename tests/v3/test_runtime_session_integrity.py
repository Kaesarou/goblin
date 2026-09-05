from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.instruments.models import AssetClass
from app.market.models import Candle
from app.market.timeframes import Timeframe
from app.market_data.models import CandleBuildResult, CandleQuality, MarketDataEvent, MarketDataSource
from app.runtime.trading_session_window import AssetTradingSessionConfig, TradingSessionService, parse_trading_sessions
from tests.v3.test_runtime import NOW, _snapshot
from tests.v3.test_runtime_equity_authority import runtime_for_test, trailing_inventory

FRIDAY = datetime(2026, 9, 4, 16, 58, 30, tzinfo=timezone.utc)
MONDAY = datetime(2026, 9, 7, 7, 0, 10, tzinfo=timezone.utc)


def feed(runtime, timestamp):
    runtime._refresh_sessions(timestamp)
    runtime._handle_event(MarketDataEvent("AAPL", MarketDataSource.WEBSOCKET, timestamp,
        _snapshot("AAPL", 105, timestamp=timestamp)), timestamp)


def candles(runtime):
    return [payload["candle"] for name, payload in runtime.candle_journal.events if name == "candle_finalized"]


def test_friday_to_monday_keeps_last_session_candle_and_no_ghosts(tmp_path):
    runtime = runtime_for_test(tmp_path)
    trailing_inventory(runtime)
    feed(runtime, FRIDAY)
    runtime._refresh_sessions(FRIDAY.replace(hour=17, minute=0, second=0))
    assert [c.opened_at.minute for c in candles(runtime)] == [58, 59]
    before = runtime.book.inventories
    runtime._refresh_sessions(FRIDAY+timedelta(days=1))
    runtime._finalize_clocked_candles(MONDAY)
    feed(runtime, MONDAY)
    assert len(candles(runtime)) == 2
    assert runtime.book.inventories == before
    assert not runtime.executor._pending_actions
    feed(runtime, MONDAY+timedelta(minutes=1))
    assert len(candles(runtime)) == 3
    assert candles(runtime)[-1].opened_at == MONDAY.replace(second=0)
    assert len(runtime.multi_timeframe_service.bars("AAPL", Timeframe.M1)) == 3


def test_clock_finalization_is_capped_by_last_real_snapshot_session(tmp_path):
    runtime = runtime_for_test(tmp_path)
    feed(runtime, FRIDAY)
    runtime._finalize_clocked_candles(MONDAY)
    runtime._finalize_clocked_candles(MONDAY+timedelta(hours=1))
    assert len(candles(runtime)) == 2
    assert candles(runtime)[-1].closed_at == FRIDAY.replace(hour=17, minute=0, second=0)


def result_for(opened_at):
    candle = Candle("AAPL", 60, 105, 106, 104, 105, None, opened_at,
                    opened_at+timedelta(minutes=1), 1)
    quote = _snapshot("AAPL", 105, timestamp=opened_at+timedelta(seconds=30))
    return CandleBuildResult(candle, CandleQuality("websocket", 1, 1, 0, 0, False), quote)


def test_0659_to_0700_rejects_previous_candle_before_any_state_mutation(tmp_path, monkeypatch):
    runtime = runtime_for_test(tmp_path)
    trailing_inventory(runtime)
    before = runtime.book.inventories
    def forbidden(*args, **kwargs):
        pytest.fail("Out-of-session candle reached a state engine")
    monkeypatch.setattr(runtime.feature_engine, "update", forbidden)
    monkeypatch.setattr(runtime.multi_timeframe_service, "on_base_candle", forbidden)
    monkeypatch.setattr(runtime.windows, "record", forbidden)
    runtime._process_closed_candle("AAPL", result_for(NOW-timedelta(minutes=1)), NOW, source="clock")
    assert runtime.metrics["candle_session_rejections"] == 1
    assert runtime.book.inventories == before
    assert not candles(runtime)


def test_candle_uses_own_session_even_when_current_decision_is_next_day(tmp_path, monkeypatch):
    runtime = runtime_for_test(tmp_path)
    runtime.session_decisions["AAPL"] = runtime._session_at("AAPL", NOW+timedelta(days=1))
    seen = []
    actual = runtime.multi_timeframe_service.on_base_candle
    def observe(**kwargs):
        seen.append(kwargs["session_decision"].session_key)
        return actual(**kwargs)
    monkeypatch.setattr(runtime.multi_timeframe_service, "on_base_candle", observe)
    runtime._process_closed_candle("AAPL", result_for(NOW), NOW+timedelta(days=1), source="clock")
    assert seen == [runtime._session_at("AAPL", NOW).session_key]
    assert runtime.metrics["candle_session_rejections"] == 0


def test_new_session_clears_quote_and_research_state_preserving_inventory(tmp_path):
    runtime = runtime_for_test(tmp_path)
    trailing_inventory(runtime)
    feed(runtime, FRIDAY)
    runtime._finalize_clocked_candles(MONDAY)
    before = runtime.book.inventories
    resets = []
    runtime.research_pipeline = SimpleNamespace(reset_symbol=lambda symbol: resets.append(symbol))
    runtime._refresh_sessions(MONDAY)
    assert resets == ["AAPL"]
    assert "AAPL" not in runtime.latest_snapshots
    assert "AAPL" not in runtime.latest_features
    assert runtime.book.inventories == before


@pytest.mark.parametrize("asset", [AssetClass.EQUITY_US, AssetClass.EQUITY_EU])
@pytest.mark.parametrize("day", [5, 6])
@pytest.mark.parametrize("zone", ["Europe/Paris", "America/New_York"])
def test_equities_closed_on_local_weekend(asset, day, zone):
    service = TradingSessionService({asset: AssetTradingSessionConfig(asset, parse_trading_sessions("07:00-22:00"))}, zone)
    decision = service.evaluate(asset_class=asset, now=datetime(2026, 9, day, 12, tzinfo=ZoneInfo(zone)))
    assert not decision.session_active and not decision.new_entries_allowed


def test_weekends_use_local_date_and_crypto_is_unchanged():
    asset = AssetClass.EQUITY_US
    service = TradingSessionService({asset: AssetTradingSessionConfig(asset, ())}, "America/New_York")
    assert service.evaluate(asset_class=asset, now=datetime(2026, 9, 5, 1, tzinfo=timezone.utc)).session_active
    assert not service.evaluate(asset_class=asset, now=datetime(2026, 9, 7, 1, tzinfo=timezone.utc)).session_active
    crypto = TradingSessionService({AssetClass.CRYPTO: AssetTradingSessionConfig(AssetClass.CRYPTO, ())}, "UTC")
    for day in (5, 6):
        decision = crypto.evaluate(asset_class=AssetClass.CRYPTO, now=datetime(2026, 9, day, 12, tzinfo=timezone.utc))
        assert decision.session_24_7 and decision.session_active and decision.new_entries_allowed
