from dataclasses import dataclass, field, replace

from app.execution.breakeven_profile import (
    BreakevenProfileName,
    resolve_breakeven_profile,
)
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
    after_initial_stop_minutes=45,
    after_protected_breakeven_minutes=15,
    after_protected_trailing_minutes=15,
    after_stale_exit_minutes=15,
    after_session_force_close_minutes=15,
    after_manual_or_broker_close_minutes=15,
    after_unknown_confirmed_close_minutes=15,
    initial_stop_symbol_lock_minutes=15,
)

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
        AssetClass.CRYPTO: CandidateSelectionConfig(top_n=2),
        AssetClass.EQUITY_US: CandidateSelectionConfig(top_n=1),
        AssetClass.EQUITY_EU: CandidateSelectionConfig(top_n=1),
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
    breakeven_profile_name: BreakevenProfileName = (
        BreakevenProfileName.CORRECTED_BASELINE_V1
    )

    def __post_init__(self) -> None:
        profile = resolve_breakeven_profile(self.breakeven_profile_name)
        object.__setattr__(
            self,
            'crypto',
            self._with_breakeven_trigger(
                self.crypto,
                profile.trigger_percent_for(AssetClass.CRYPTO),
            ),
        )
        object.__setattr__(
            self,
            'equity_eu',
            self._with_breakeven_trigger(
                self.equity_eu,
                profile.trigger_percent_for(AssetClass.EQUITY_EU),
            ),
        )
        object.__setattr__(
            self,
            'equity_us',
            self._with_breakeven_trigger(
                self.equity_us,
                profile.trigger_percent_for(AssetClass.EQUITY_US),
            ),
        )

    @staticmethod
    def _with_breakeven_trigger(
        config: InstrumentConfig,
        trigger_percent: float,
    ) -> InstrumentConfig:
        return replace(
            config,
            risk=replace(
                config.risk,
                breakeven_trigger_percent=trigger_percent,
            ),
        )
