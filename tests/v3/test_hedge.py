from datetime import datetime, timezone

from app.v3.config import HedgeConfig
from app.v3.hedge import PortfolioHedgeManager
from app.v3.models import HedgeState, IntentPurpose, InventoryState, PortfolioState

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def inventory(notional=20_000.0):
    return InventoryState(
        inventory_id="NVDA:1",
        symbol="NVDA",
        opened_at=NOW,
        total_units=100,
        average_entry_price=200,
        entry_fill_count=3,
        last_entry_at=NOW,
        last_entry_price=200,
        total_notional=notional,
        wallet_exposure_pct=notional / 100_000,
    )


def test_default_hedge_config_has_no_runtime_authority():
    portfolio = PortfolioState(
        equity=100_000,
        inventories=(inventory(),),
        symbol_betas={"NVDA": 1.5},
    )
    batch = PortfolioHedgeManager(HedgeConfig()).plan(
        portfolio=portfolio,
        hedge_price=600,
        asof=NOW,
    )
    assert batch.intents == ()
    assert batch.decisions == ()


def test_sell_hedge_reduces_beta_distance_and_never_overhedges():
    cfg = HedgeConfig(
        enabled=True,
        target_beta_exposure_pct=0.05,
        open_above_beta_exposure_pct=0.10,
        max_hedge_notional_pct=0.20,
        min_adjustment_notional_pct=0.001,
    )
    portfolio = PortfolioState(
        equity=100_000,
        inventories=(inventory(),),
        symbol_betas={"NVDA": 1.5},
    )
    batch = PortfolioHedgeManager(cfg).plan(portfolio=portfolio, hedge_price=600, asof=NOW)
    assert len(batch.intents) == 1
    intent = batch.intents[0]
    assert intent.side == "SELL"
    assert intent.purpose == IntentPurpose.HEDGE_OPEN
    before = float(intent.metadata["beta_before"])
    after = float(intent.metadata["beta_after"])
    target = float(intent.metadata["target_beta_notional"])
    assert abs(after - target) < abs(before - target)
    assert float(intent.metadata["desired_hedge_notional"]) <= before


def test_existing_hedge_is_closed_when_beta_falls_below_close_threshold():
    cfg = HedgeConfig(
        enabled=True,
        close_below_beta_exposure_pct=0.06,
        min_adjustment_notional_pct=0.001,
    )
    portfolio = PortfolioState(
        equity=100_000,
        inventories=(inventory(4_000),),
        hedge=HedgeState("SPY", notional=2_000, beta_notional_offset=2_000, opened_at=NOW),
        symbol_betas={"NVDA": 1.0},
    )
    batch = PortfolioHedgeManager(cfg).plan(portfolio=portfolio, hedge_price=600, asof=NOW)
    assert batch.intents[0].purpose == IntentPurpose.HEDGE_CLOSE
    assert batch.intents[0].side == "BUY"
