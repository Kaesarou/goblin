from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.brokers.etoro.account_equity_mapper import ACCOUNT_EQUITY_SOURCE
from app.config.settings import Settings
from app.instruments.instrument_registry import InstrumentRegistry
from app.instruments.models import AssetClass
from app.market.data_quality import MarketDataValidator
from app.market.session_timeframe_service import FullSessionMultiTimeframeService
from app.market_data.candle_stream import QualityAwareCandleBuilder
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from app.runtime.trading_session_window import AssetTradingSessionConfig, TradingSessionService, parse_trading_sessions
from app.v3.config import etoro5_research_config
from app.v3.features import OnlineFeatureEngine
from app.v3.persistence import InventoryEventStore
from app.v3.runtime import GoblinV3Runtime, V3DecisionWindowBatch
from app.v3.state_store import V3RuntimeStateStore
from tests.v3.test_planner import _planner
from tests.v3.test_runtime import NOW, RecordingJournal, _feature, _snapshot


def runtime_for_test(tmp_path):
    settings = Settings(BROKER="etoro_demo", WATCHLIST="AAPL", EQUITY_US_SYMBOLS="AAPL",
                        EQUITY_EU_SYMBOLS="", CRYPTO_SYMBOLS="", TRADING_SESSION_TIMEZONE="UTC")
    registry = InstrumentRegistry(settings)
    config = etoro5_research_config()
    runtime = GoblinV3Runtime(
        settings=settings, symbols=["AAPL"], run_id="integrity-test", instrument_registry=registry,
        execution_broker=SimpleNamespace(), rest_market_data=SimpleNamespace(),
        live_market_data=SimpleNamespace(requires_websocket_health=False),
        candle_builders={"AAPL": QualityAwareCandleBuilder()},
        trading_session_service=TradingSessionService({AssetClass.EQUITY_US: AssetTradingSessionConfig(
            AssetClass.EQUITY_US, parse_trading_sessions("07:00-17:00"))}, "UTC"),
        market_context_service=SimpleNamespace(observe_accepted_snapshot=lambda snapshot: None,
                                               reset_session=lambda key: None),
        multi_timeframe_service=FullSessionMultiTimeframeService(),
        market_data_validator=MarketDataValidator(), planner=_planner(config), config=config,
        feature_engine=OnlineFeatureEngine({"AAPL": AssetClass.EQUITY_US}),
        event_store=InventoryEventStore(tmp_path / "runtime.sqlite"),
        runtime_state_store=V3RuntimeStateStore(tmp_path / "runtime.sqlite"),
        trade_journal=RecordingJournal(), market_journal=RecordingJournal(),
        candle_journal=RecordingJournal(), heartbeat=SimpleNamespace(),
    )
    runtime.coordinator.entry_allowed = lambda symbol, **kwargs: True
    runtime._journal_budget_metrics = lambda: {}
    runtime._refresh_sessions(NOW)
    return runtime


def trailing_inventory(runtime):
    inv = runtime.book.apply_entry_fill(inventory_id="inventory", symbol="AAPL", position_id="p1",
        units=10, price=100, fee=0, filled_at=NOW-timedelta(days=1))
    inv = replace(inv, trailing_max_since_open=150, trailing_min_since_max=105)
    runtime.book._inventories[inv.inventory_id] = inv
    return inv


def decision_batch():
    feature = replace(_feature("AAPL"), ema_readiness=1.0)
    return V3DecisionWindowBatch(feature.asof, ("AAPL",), ("AAPL",), (), "all_symbols_completed",
                                {"AAPL": feature}, {"AAPL": _snapshot("AAPL", 105)}, {"AAPL": True})


def test_restored_equity_is_exit_only_with_identical_exit_math(tmp_path, monkeypatch):
    runtime = runtime_for_test(tmp_path)
    runtime.runtime_state_store.save_broker_equity(value=100_000, observed_at=NOW-timedelta(days=1), source=ACCOUNT_EQUITY_SOURCE)
    runtime._restore_exit_planning_equity_reference()
    inventory = trailing_inventory(runtime)
    batch = decision_batch()
    market = batch.features["AAPL"].market_state(snapshot=batch.snapshots["AAPL"], entry_allowed=False)
    expected = runtime.planner._exit(market, runtime.book.portfolio(equity=100_000), inventory)
    def forbidden(*args):
        raise AssertionError("reentry must not be evaluated")
    monkeypatch.setattr(runtime.planner, "_reentry", forbidden)
    runtime._process_decision_window(batch)
    assert runtime._current_run_equity is None
    assert runtime._exit_planning_equity == 100_000
    assert runtime.intent_book.snapshot() == expected.intents
    assert expected.intents[0].metadata["close_fraction_of_units"] == 0.84
    authority = runtime._heartbeat_metrics()
    assert not authority["v3_new_risk_allowed"]
    assert "current_run_equity_available" in authority["new_risk_blockers"]["AAPL"]


def test_flat_buy_needs_current_run_equity(tmp_path):
    runtime = runtime_for_test(tmp_path)
    runtime._exit_planning_equity = 100_000
    runtime._process_decision_window(decision_batch())
    assert not runtime.intent_book.snapshot()
    runtime._current_run_equity = 100_000
    runtime._process_decision_window(decision_batch())
    assert runtime.intent_book.snapshot()[0].side == "BUY"
    assert runtime._operational_entry_allowed("AAPL")


@pytest.mark.parametrize("blocker", ["executor", "cutoff", "equity"])
def test_new_risk_blockers_do_not_suppress_trailing_reduce_only(tmp_path, blocker, monkeypatch):
    runtime = runtime_for_test(tmp_path)
    runtime._exit_planning_equity = runtime._current_run_equity = 100_000
    trailing_inventory(runtime)
    if blocker == "executor":
        runtime.executor.halted_reason = "broker_quantity_reduction_unattributed"
    elif blocker == "cutoff":
        runtime._refresh_sessions(NOW.replace(hour=16, minute=50))
        assert runtime.session_decisions["AAPL"].session_active
        assert not runtime.session_decisions["AAPL"].new_entries_allowed
    else:
        runtime._current_run_equity = None
    monkeypatch.setattr(runtime.planner, "_reentry", lambda *args: pytest.fail("reentry was evaluated"))
    assert not runtime._operational_entry_allowed("AAPL")
    runtime._process_decision_window(decision_batch())
    assert runtime.intent_book.snapshot()
    assert all(intent.reduce_only for intent in runtime.intent_book.snapshot())


def test_no_equity_reference_retains_protection_and_logs_blocker_once(tmp_path):
    runtime = runtime_for_test(tmp_path)
    runtime._exit_planning_equity = 100_000
    trailing_inventory(runtime)
    runtime._process_decision_window(decision_batch())
    before = runtime.intent_book.snapshot()
    runtime._exit_planning_equity = None
    for _ in range(3):
        runtime._process_decision_window(decision_batch())
    assert runtime.intent_book.snapshot() == before
    blocked = [payload for name, payload in runtime.trade_journal.events if name == "v3_decision_window_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "reduce_only_planning_blocked_no_equity_reference"


def test_equity_failure_metrics_are_incident_scoped_and_recovery_persists(tmp_path):
    runtime = runtime_for_test(tmp_path)
    completions = []
    submitted = []
    runtime.execution_broker.get_account_equity = lambda: 98_765.0
    runtime.maintenance_runner = SimpleNamespace(drain=lambda: list(completions),
        has_pending_kind=lambda kind: False, submit=lambda **kwargs: submitted.append(kwargs))
    for index, value in enumerate([RuntimeError("unavailable")]*20 + [True, float("nan")]):
        runtime._maybe_schedule_equity_refresh(index * 30, force=True)
        completions[:] = [BrokerTaskCompletion("equity", "v3_account_equity", BrokerTaskLane.STANDARD,
            None, None if isinstance(value, Exception) else value, value if isinstance(value, Exception) else None)]
        runtime._drain_broker_tasks()
    assert runtime.metrics["equity_refresh_attempts"] == 22
    assert runtime.metrics["equity_refresh_failures"] == 22
    assert runtime.metrics["equity_consecutive_failures"] == 22
    assert runtime.metrics["errors"] == 0
    assert sum(name == "v3_account_equity_error" for name, _ in runtime.trade_journal.events) == 1
    completions[:] = [BrokerTaskCompletion("equity", "v3_account_equity", BrokerTaskLane.STANDARD, None, 98_765.0)]
    runtime._drain_broker_tasks()
    assert runtime._current_run_equity == runtime._exit_planning_equity == 98_765
    assert runtime.metrics["equity_refresh_recoveries"] == 1
    assert runtime.metrics["equity_consecutive_failures"] == 0
    assert runtime.metrics["equity_last_success_at"] is not None
    assert runtime.runtime_state_store.load_broker_equity().source == ACCOUNT_EQUITY_SOURCE
