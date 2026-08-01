from dataclasses import replace

from app.execution.breakeven_profile import BreakevenProfileName
from app.execution.scoring.managed_outcome_model_contract import (
    MANAGED_SELECTION_POLICY_VERSION,
)
from app.instruments.models import AssetClass
from app.strategies.balanced_strategy_config import BalancedStrategyConfig


def test_corrected_baseline_retains_current_live_equity_thresholds():
    profile = BalancedStrategyConfig()

    assert profile.breakeven_profile_name is (BreakevenProfileName.CORRECTED_BASELINE_V1)
    assert profile.equity_eu.risk.breakeven_trigger_percent == 0.55
    assert profile.equity_us.risk.breakeven_trigger_percent == 0.60


def test_delayed_variant_uses_eu_065_and_us_070_only():
    baseline = BalancedStrategyConfig()
    variant = BalancedStrategyConfig(
        breakeven_profile_name=(BreakevenProfileName.DELAYED_EQUITY_TRIGGER_V1)
    )

    assert variant.equity_eu.risk.breakeven_trigger_percent == 0.65
    assert variant.equity_us.risk.breakeven_trigger_percent == 0.70
    assert variant.crypto.risk.breakeven_trigger_percent == 0.20

    for baseline_config, variant_config in (
        (baseline.equity_eu, variant.equity_eu),
        (baseline.equity_us, variant.equity_us),
        (baseline.crypto, variant.crypto),
    ):
        assert variant_config == replace(
            baseline_config,
            risk=replace(
                baseline_config.risk,
                breakeven_trigger_percent=(variant_config.risk.breakeven_trigger_percent),
            ),
        )


def test_breakeven_profiles_do_not_change_managed_selection_or_top_n():
    baseline = BalancedStrategyConfig()
    variant = BalancedStrategyConfig(
        breakeven_profile_name=(BreakevenProfileName.DELAYED_EQUITY_TRIGGER_V1)
    )

    assert MANAGED_SELECTION_POLICY_VERSION == "managed_edge_v1"
    assert baseline.candidate_selection_configs == (variant.candidate_selection_configs)
    assert {
        asset_class: config.top_n
        for asset_class, config in variant.candidate_selection_configs.items()
    } == {
        AssetClass.CRYPTO: 2,
        AssetClass.EQUITY_US: 1,
        AssetClass.EQUITY_EU: 1,
    }
