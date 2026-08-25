from dataclasses import replace
from datetime import datetime, timezone

from app.v3.config import RecoverabilityConfig, rr5_research_config
from app.v3.economics import BrokerCostSchedule, EconomicsModel
from app.v3.models import InventoryState, MarketState, PortfolioState
from app.v3.planner import InventoryPlanner
from app.v3.recoverability import RecoverabilityScorer
from app.v3.risk import InventoryRiskPolicy

NOW = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)


def _planner(config):
    return InventoryPlanner(
        config=config,
        recoverability_scorer=RecoverabilityScorer.from_default_artifact(),
        risk_policy=InventoryRiskPolicy(config.risk),
        economics_model=EconomicsModel(config.economics, BrokerCostSchedule()),
    )


def _market(*, features=None):
    return MarketState(
        symbol="AAPL",
        asof=NOW,
        bid=90.0,
        ask=90.1,
        last=90.05,
        ema_lower=95.0,
        ema_upper=105.0,
        volatility_1m=0.001,
        volatility_1h=0.01,
        features=features or {},
    )


def _inventory(*, fills=1):
    return InventoryState(
        inventory_id="AAPL:1",
        symbol="AAPL",
        opened_at=NOW,
        total_units=10.0,
        average_entry_price=100.0,
        entry_fill_count=fills,
        last_entry_at=NOW,
        last_entry_price=100.0,
        total_notional=1_000.0,
        wallet_exposure_pct=0.01,
        last_fill_at=NOW,
    )


def test_initial_entry_does_not_require_recoverability_features():
    config = rr5_research_config()
    batch = _planner(config).plan_symbol(
        market=_market(features={}),
        portfolio=PortfolioState(equity=100_000),
    )
    assert len(batch.intents) == 1
    assert batch.intents[0].purpose.value == "initial_entry"


def test_recoverability_cannot_grant_reentry_after_max_fill_cap():
    config = replace(
        rr5_research_config(),
        recoverability=RecoverabilityConfig(
            enabled=True,
            min_rank_quantile=0.0,
            gate_from_entry_fill_count=2,
        ),
    )
    inventory = _inventory(fills=config.risk.max_entry_fills)
    batch = _planner(config).plan_symbol(
        market=_market(features={}),
        portfolio=PortfolioState(equity=100_000, inventories=(inventory,)),
    )
    assert not batch.intents
    assert any(decision.reason.value == "max_entry_fills" for decision in batch.decisions)
