from datetime import datetime, timedelta, timezone

from app.instruments.models import AssetClass
from app.market.models import Candle
from app.v3.book import InventoryBook
from app.v3.features import OnlineFeatureEngine
from app.v3.state_store import V3_RUNTIME_STATE_VERSION, V3RuntimeStateStore

UTC = timezone.utc
NOW = datetime(2026, 8, 25, 7, 0, tzinfo=UTC)


def _candle(minute: int, price: float) -> Candle:
    opened = NOW + timedelta(minutes=minute)
    return Candle(
        symbol="AIR.PA",
        timeframe_seconds=60,
        open=price,
        high=price * 1.002,
        low=price * 0.998,
        close=price,
        volume=None,
        opened_at=opened,
        closed_at=opened + timedelta(minutes=1),
        sample_count=6,
    )


def test_feature_state_roundtrip_preserves_causal_accumulators(tmp_path):
    engine = OnlineFeatureEngine({"AIR.PA": AssetClass.EQUITY_EU})
    for minute in range(75):
        engine.update(_candle(minute, 100.0 + minute / 100.0))
    store = V3RuntimeStateStore(tmp_path / "state.sqlite")
    store.save_feature_engine(engine)

    restored = OnlineFeatureEngine({"AIR.PA": AssetClass.EQUITY_EU})
    assert store.restore_feature_engine(restored) == ("AIR.PA",)
    original = engine.states["AIR.PA"]
    actual = restored.states["AIR.PA"]
    assert [item.value for item in actual.strategy_emas] == [
        item.value for item in original.strategy_emas
    ]
    assert actual.volatility_1m.value == original.volatility_1m.value
    assert actual.volatility_1m.numerator == original.volatility_1m.numerator
    assert actual.volatility_1m.denominator == original.volatility_1m.denominator
    assert tuple(actual.closes) == tuple(original.closes)
    assert actual.current_hour == original.current_hour
    assert actual.last_opened_at == original.last_opened_at


def test_inventory_runtime_state_roundtrip_and_post_fill_reset_wins(tmp_path):
    store = V3RuntimeStateStore(tmp_path / "state.sqlite")
    book = InventoryBook()
    book.apply_entry_fill(
        inventory_id="inv-1",
        symbol="AIR.PA",
        position_id="p1",
        units=1.0,
        price=100.0,
        fee=0.0,
        filled_at=NOW,
    )
    book.observe_candle(symbol="AIR.PA", high=105.0, low=95.0, close=102.0)
    store.save_inventory_book(book, asof=NOW + timedelta(minutes=1))

    rebuilt = InventoryBook()
    rebuilt.apply_entry_fill(
        inventory_id="inv-1",
        symbol="AIR.PA",
        position_id="p1",
        units=1.0,
        price=100.0,
        fee=0.0,
        filled_at=NOW,
    )
    assert store.restore_inventory_book(rebuilt) == ("inv-1",)
    restored = rebuilt.active_for_symbol("AIR.PA")
    assert restored.trailing_min_since_open == 95.0
    assert restored.trailing_max_since_open == 105.0

    # A later fill resets trailing state. The older runtime snapshot must not
    # resurrect extrema that predate that fill.
    later = NOW + timedelta(minutes=2)
    rebuilt.apply_entry_fill(
        inventory_id="inv-1",
        symbol="AIR.PA",
        position_id="p2",
        units=1.0,
        price=96.0,
        fee=0.0,
        filled_at=later,
    )
    assert store.restore_inventory_book(rebuilt) == ()
    after_fill = rebuilt.active_for_symbol("AIR.PA")
    assert after_fill.trailing_min_since_open is None
    assert after_fill.trailing_max_since_open is None


def test_state_exports_make_causal_restart_state_available_to_run_artifacts(tmp_path):
    engine = OnlineFeatureEngine({"AIR.PA": AssetClass.EQUITY_EU})
    for minute in range(5):
        engine.update(_candle(minute, 100.0 + minute / 100.0))

    book = InventoryBook()
    book.apply_entry_fill(
        inventory_id="inv-1",
        symbol="AIR.PA",
        position_id="p1",
        units=1.0,
        price=100.0,
        fee=0.0,
        filled_at=NOW,
    )
    book.observe_candle(symbol="AIR.PA", high=103.0, low=97.0, close=101.0)

    store = V3RuntimeStateStore(tmp_path / "state.sqlite")
    feature_state = store.export_feature_state(engine)
    inventory_state = store.export_inventory_runtime_state(book)

    assert feature_state["AIR.PA"]["state_version"] == V3_RUNTIME_STATE_VERSION
    assert feature_state["AIR.PA"]["state"]["last_opened_at"] is not None
    assert inventory_state["inv-1"]["state_version"] == V3_RUNTIME_STATE_VERSION
    assert inventory_state["inv-1"]["state"]["trailing_min_since_open"] == 97.0
    assert inventory_state["inv-1"]["state"]["trailing_max_since_open"] == 103.0
