"""Live dispatch risk: October 22 LRCX race and broker reservation integrity."""
from datetime import UTC, datetime, timedelta

import pytest

from app.brokers.paper.paper_broker import PaperBrokerClient
from app.market.models import MarketSnapshot
from app.v3.book import InventoryBook
from app.v3.config import InventoryRiskConfig
from app.v3.entry_risk_authority import validate_entry_dispatch
from app.v3.live_execution import V3BrokerExecutor
from app.v3.models import ExecutionStyle, IntentPurpose, OrderIntent
from app.v3.persistence import InventoryEventStore
from tests.v3.test_live_execution import ImmediateRunner

NOW = datetime(2026, 10, 22, 14, 22, 5, tzinfo=UTC)
CONFIG = InventoryRiskConfig(
    max_entry_fills=5, max_symbol_exposure_pct=.04,
    max_portfolio_exposure_pct=.15, max_inventories=5,
)


def intent(symbol, amount, *, purpose=IntentPurpose.INITIAL_ENTRY,
           inventory=None, count=None, token=None):
    return OrderIntent(
        token or f"buy:{symbol}:{count}", purpose, symbol, "BUY",
        amount, NOW, ExecutionStyle.MARKET,
        inventory_id=inventory, metadata=(
            {"inventory_entry_fill_count": count} if count is not None else {}
        ),
    )


def book_with_lrcx(fills=4, notional=2200.0):
    book = InventoryBook()
    for i in range(fills):
        book.apply_entry_fill(
            inventory_id="lrcx-1", symbol="LRCX", position_id=f"broker-{i}",
            units=notional / fills / 100, price=100.0,
            account_notional=notional / fills, fee=0.0,
            filled_at=NOW-timedelta(hours=1),
        )
    return book


def check(book, order, *, equity=100_000.0, observed_at=NOW, reservations=None):
    return validate_entry_dispatch(
        intent=order, book=book, equity=equity,
        observed_at=observed_at, now=NOW, config=CONFIG,
        reservations=reservations or {},
    )


def test_lrcx_second_intent_created_pre_fill_is_rejected_at_dispatch():
    book = book_with_lrcx()
    first = intent("LRCX", 1651, purpose=IntentPurpose.REENTRY,
                   inventory="lrcx-1", count=4, token="first")
    second = intent("LRCX", 1651, purpose=IntentPurpose.REENTRY,
                    inventory="lrcx-1", count=4, token="second")
    assert check(book, first).allowed
    # The second BUY was computed at 14:22:01, before the first filled.
    assert not check(book, second, reservations={
        "first": ("LRCX", 1651)
    }).allowed
    book.apply_entry_fill(
        inventory_id="lrcx-1", symbol="LRCX", position_id="broker-fifth",
        units=16.51, price=100, account_notional=1651,
        fee=0, filled_at=NOW,
    )
    rejected = check(book, second)
    assert not rejected.allowed
    assert rejected.reason == "stale_reentry_fill_count"
    assert not check(book, intent(
        "LRCX", 1, purpose=IntentPurpose.REENTRY,
        inventory="lrcx-1", count=5,
    )).allowed


def test_symbol_cap_and_preexisting_breach_are_fail_closed():
    book = book_with_lrcx(notional=3900)
    rejected = check(book, intent(
        "LRCX", 101, purpose=IntentPurpose.REENTRY,
        inventory="lrcx-1", count=4,
    ))
    assert rejected.reason == "symbol_exposure_cap"
    book = book_with_lrcx(notional=5586.36)
    assert check(book, intent(
        "LRCX", 300, purpose=IntentPurpose.REENTRY,
        inventory="lrcx-1", count=4,
    )).reason == "existing_symbol_exposure_breach"
    # An already-breached instrument cannot contaminate another symbol's
    # independent cap while total committed exposure remains below 15%.
    assert check(book, intent("AAPL", 300)).allowed


def test_cross_symbol_reservations_prevent_portfolio_budget_double_spend():
    book = InventoryBook()
    pending = {
        "a": ("AAPL", 3800.0),
        "b": ("MSFT", 3800.0),
        "c": ("QCOM", 3800.0),
    }
    assert check(book, intent("CAT", 3800), reservations=pending).reason == "portfolio_exposure_cap"
    assert check(book, intent("CAT", 3000), reservations=pending).allowed


def test_equity_missing_old_or_invalid_blocks_buy_without_assumed_cash():
    book = InventoryBook()
    order = intent("AAPL", 300)
    assert check(book, order, equity=None).reason == "broker_equity_unavailable"
    assert check(book, order, observed_at=None).reason == "broker_equity_timestamp_unavailable"
    assert check(book, order, observed_at=NOW.replace(tzinfo=None)).reason == "broker_equity_timestamp_unavailable"
    assert check(book, order, observed_at=NOW-timedelta(minutes=4)).reason == "broker_equity_stale"
    assert check(book, intent("AAPL", float("nan"))).reason == "invalid_entry_notional"


def test_executor_reserves_before_worker_completes_then_replays_uncertain_reservation(tmp_path, monkeypatch):
    monkeypatch.setattr("app.v3.live_execution._utc_now", lambda: NOW)
    store = InventoryEventStore(tmp_path / "risk.sqlite")
    book = InventoryBook()
    broker = PaperBrokerClient(equity=100_000)
    runner = ImmediateRunner()
    make = lambda book, runner: V3BrokerExecutor(
        broker=broker, task_runner=runner, event_store=store, book=book,
        strategy_version="ETORO5", model_version=None,
        risk_config=CONFIG, equity_reference=lambda: (100_000, NOW),
    )
    executor = make(book, runner)
    snap = lambda symbol: MarketSnapshot(symbol, 99.9, 100.1, 100.0, NOW)
    assert executor.schedule(intent("AAPL", 3800), snapshot=snap("AAPL"))
    assert executor.schedule(intent("MSFT", 3800), snapshot=snap("MSFT"))
    assert executor.schedule(intent("QCOM", 3800), snapshot=snap("QCOM"))
    assert not executor.schedule(intent("CAT", 3800), snapshot=snap("CAT"))
    assert executor.last_entry_risk_rejection[1] == "portfolio_exposure_cap"
    assert len(executor._open_reservations) == 3
    # Process died before accounting for the broker's responses.
    restored = make(InventoryBook.from_events(store.events()), ImmediateRunner())
    assert not restored.new_risk_allowed
    assert sum(x[1] for x in restored._open_reservations.values()) == 11400
    assert len([e for e in store.events() if e.event_type == "ORDER_SUBMISSION_STARTED"]) == 3


def test_reduce_only_is_never_subject_to_buy_risk_gate(tmp_path, monkeypatch):
    monkeypatch.setattr("app.v3.live_execution._utc_now", lambda: NOW)
    store = InventoryEventStore(tmp_path / "risk.sqlite")
    book = book_with_lrcx(fills=1, notional=5586.36)
    executor = V3BrokerExecutor(
        broker=PaperBrokerClient(equity=100_000),
        task_runner=ImmediateRunner(), event_store=store, book=book,
        strategy_version="ETORO5", model_version=None,
        risk_config=CONFIG, equity_reference=lambda: (100_000, NOW),
    )
    assert not executor.schedule(intent("AAPL", 300),
                                 snapshot=MarketSnapshot("AAPL", 99, 100, 100, NOW))
    # A broker-owned SELL still goes through the original independent close path.
    sell = OrderIntent(
        "exit-lrcx", IntentPurpose.RISK_REDUCTION, "LRCX", "SELL",
        5586, NOW, ExecutionStyle.MARKET, inventory_id="lrcx-1",
        reduce_only=True, metadata={"close_fraction_of_units": 1.0},
    )
    assert executor.schedule(sell, snapshot=MarketSnapshot("LRCX", 100, 101, 100, NOW))


def test_durable_ledger_failure_prevents_broker_buy(tmp_path, monkeypatch):
    """No broker mutation if START/reservation cannot be committed to SQLite."""
    monkeypatch.setattr("app.v3.live_execution._utc_now", lambda: NOW)
    store = InventoryEventStore(tmp_path / "write_fail.sqlite")
    broker = PaperBrokerClient(equity=100_000)
    runner = ImmediateRunner()
    executor = V3BrokerExecutor(
        broker=broker, task_runner=runner, event_store=store,
        book=InventoryBook(), strategy_version="ETORO5", model_version=None,
        risk_config=CONFIG, equity_reference=lambda: (100_000, NOW),
    )
    monkeypatch.setattr(store, "append", lambda event: False)
    with pytest.raises(RuntimeError, match="Durable BUY risk reservation"):
        executor.schedule(
            intent("AAPL", 300),
            snapshot=MarketSnapshot("AAPL", 99, 100, 100, NOW),
        )
    assert runner.items == []
    assert executor.halted_reason == "open_submission_dispatch_unknown"
    assert executor._open_reservations
    assert not executor.new_risk_allowed


def test_existing_broker_breach_is_reported_without_liquidation(tmp_path):
    from tests.v3.test_runtime_equity_authority import runtime_for_test

    runtime = runtime_for_test(tmp_path)
    inventory = runtime.book.apply_entry_fill(
        inventory_id="lrcx-legacy", symbol="LRCX", position_id="lrcx-p1",
        units=55.8636, price=100.0, account_notional=5586.36,
        fee=0.0, filled_at=NOW,
    )
    runtime._observe_risk_limit_breaches(equity=100_000, now=NOW)
    events = [e for e in runtime.trade_journal.events
              if e[0] == "v3_risk_limit_breach"]
    assert len(events) == 1
    assert events[0][1]["symbols_over_cap"] == ("LRCX",)
    assert not events[0][1]["automatic_close_requested"]
    assert runtime.book.active_for_symbol("LRCX") is not None
    runtime._observe_risk_limit_breaches(equity=100_000, now=NOW)
    assert len([e for e in runtime.trade_journal.events
                if e[0] == "v3_risk_limit_breach"]) == 1

    runtime.book.apply_exit_fill(
        position_id="lrcx-p1", exit_price=100.0, units=30.0,
        fee=0.0, filled_at=NOW,
    )
    runtime._observe_risk_limit_breaches(equity=100_000, now=NOW)
    assert [e[0] for e in runtime.trade_journal.events
            if e[0] in ("v3_risk_limit_breach", "v3_risk_limit_recovered")
            ] == ["v3_risk_limit_breach", "v3_risk_limit_recovered"]
