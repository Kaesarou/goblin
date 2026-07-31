import pytest

from app.instruments.models import AssetClass
from app.strategies.balanced_strategy_config import BalancedStrategyConfig


def test_strategy_profile_resolves_asset_specific_selection_configs():
    profile = BalancedStrategyConfig()
    crypto = profile.candidate_selection_config_for_asset_class(
        AssetClass.CRYPTO
    )
    us = profile.candidate_selection_config_for_asset_class(
        AssetClass.EQUITY_US
    )
    eu = profile.candidate_selection_config_for_asset_class(
        AssetClass.EQUITY_EU
    )

    assert crypto.top_n == 2
    assert us.top_n == 1
    assert eu.top_n == 1
    assert not hasattr(us, 'minimum_direction_edge')
    assert not hasattr(us, 'minimum_tp_probability')
    assert not hasattr(us, 'maximum_touch_probability')


def test_strategy_profile_rejects_invalid_asset_class():
    with pytest.raises(ValueError, match='Unsupported asset class'):
        BalancedStrategyConfig().instrument_config_for_asset_class('BROKEN')
