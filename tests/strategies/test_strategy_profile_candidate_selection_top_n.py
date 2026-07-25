import pytest

from app.instruments.models import AssetClass
from app.strategies.balanced_strategy_config import BalancedStrategyConfig


def test_strategy_profile_resolves_asset_specific_selection_configs():
    profile = BalancedStrategyConfig()
    crypto = profile.candidate_selection_config_for_asset_class(AssetClass.CRYPTO)
    us = profile.candidate_selection_config_for_asset_class(AssetClass.EQUITY_US)
    eu = profile.candidate_selection_config_for_asset_class(AssetClass.EQUITY_EU)

    assert (
        crypto.top_n,
        crypto.minimum_tp_probability,
        crypto.maximum_touch_probability,
    ) == (2, 0.10, 0.50)
    assert (
        us.top_n,
        us.minimum_tp_probability,
        us.maximum_touch_probability,
    ) == (2, 0.10, 0.50)
    assert (
        eu.top_n,
        eu.minimum_tp_probability,
        eu.maximum_touch_probability,
    ) == (1, 0.10, 0.50)
    assert not hasattr(us, 'dynamic_min_score')


def test_strategy_profile_rejects_invalid_asset_class():
    with pytest.raises(ValueError, match='Unsupported asset class'):
        BalancedStrategyConfig().instrument_config_for_asset_class('BROKEN')
