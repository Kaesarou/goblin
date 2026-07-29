from datetime import UTC, datetime

import pytest

from app.config.settings import Settings
from app.instruments.instrument_registry import InstrumentRegistry
from app.journal.run_manifest import build_run_manifest
from app.strategies.balanced_strategy_config import BalancedStrategyConfig


def test_manifest_records_effective_eu_sell_calibration_weights():
    settings = Settings(
        WATCHLIST='AAPL',
        EQUITY_US_SYMBOLS='AAPL',
        ETORO_API_KEY='secret-api',
        ETORO_USER_KEY='secret-user',
    )
    profile = BalancedStrategyConfig()
    registry = InstrumentRegistry(
        settings,
        instrument_configs=profile.instrument_configs,
    )

    manifest = build_run_manifest(
        settings=settings,
        strategy_profile=profile,
        instrument_registry=registry,
        symbols=['AAPL'],
        run_id='run-calibration-test',
        started_at=datetime(2026, 7, 29, 8, 0, tzinfo=UTC),
    )

    segments = manifest['models'][
        'outcome_probability_direction_segments'
    ]
    assert segments['EQUITY_EU_SELL']['model_weight'] == pytest.approx(0.6)
    assert segments['EQUITY_EU_SELL'][
        'segment_prior_weight'
    ] == pytest.approx(0.4)
    assert segments['EQUITY_US_SELL']['model_weight'] == pytest.approx(0.5)
    assert segments['EQUITY_US_SELL'][
        'segment_prior_weight'
    ] == pytest.approx(0.5)
