from dataclasses import dataclass, field, replace

from app.execution.candidate_selector import CandidateSelectionConfig
from app.instruments.base_configs import (
    CRYPTO_CONFIG,
    EQUITY_EU_CONFIG,
    EQUITY_US_CONFIG,
)
from app.instruments.models import AssetClass, InstrumentConfig
from app.risk.trade_cooldown import TradeCooldownConfig
from app.strategies.models import StrategyProfileConfig


BALANCED_TRADE_COOLDOWN = TradeCooldownConfig(
    after_take_profit_minutes=30,
    after_stop_loss_minutes=45,
    after_manual_close_minutes=15,
    after_unknown_close_minutes=15,
    stop_loss_symbol_lock_minutes=15,
)

PR5E_MINIMUM_TP_PROBABILITY = 0.125
PR5E_MAXIMUM_TOUCH_PROBABILITY = 0.40

BALANCED_CRYPTO_CONFIG = replace(
    CRYPTO_CONFIG,
    risk=replace(
        CRYPTO_CONFIG.risk,
        trade_cooldown=BALANCED_TRADE_COOLDOWN,
    ),
)
BALANCED_EQUITY_US_CONFIG = replace(
    EQUITY_US_CONFIG,
    risk=replace(
        EQUITY_US_CONFIG.risk,
        trade_cooldown=BALANCED_TRADE_COOLDOWN,
    ),
)
BALANCED_EQUITY_EU_CONFIG = replace(
    EQUITY_EU_CONFIG,
    risk=replace(
        EQUITY_EU_CONFIG.risk,
        trade_cooldown=BALANCED_TRADE_COOLDOWN,
    ),
)


def _selection_configs() -> dict[AssetClass, CandidateSelectionConfig]:
    return {
        AssetClass.CRYPTO: CandidateSelectionConfig(
            top_n=2,
            minimum_tp_probability=PR5E_MINIMUM_TP_PROBABILITY,
            maximum_touch_probability=PR5E_MAXIMUM_TOUCH_PROBABILITY,
        ),
        AssetClass.EQUITY_US: CandidateSelectionConfig(
            top_n=1,
            minimum_tp_probability=PR5E_MINIMUM_TP_PROBABILITY,
            maximum_touch_probability=PR5E_MAXIMUM_TOUCH_PROBABILITY,
        ),
        AssetClass.EQUITY_EU: CandidateSelectionConfig(
            top_n=1,
            minimum_tp_probability=PR5E_MINIMUM_TP_PROBABILITY,
            maximum_touch_probability=PR5E_MAXIMUM_TOUCH_PROBABILITY,
        ),
    }


@dataclass(frozen=True)
class BalancedStrategyConfig(StrategyProfileConfig):
    name: str = 'balanced'
    crypto: InstrumentConfig = BALANCED_CRYPTO_CONFIG
    equity_us: InstrumentConfig = BALANCED_EQUITY_US_CONFIG
    equity_eu: InstrumentConfig = BALANCED_EQUITY_EU_CONFIG
    candidate_selection_configs: dict[
        AssetClass,
        CandidateSelectionConfig,
    ] = field(default_factory=_selection_configs)
