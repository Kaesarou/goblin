from datetime import datetime, timezone

from app.config.settings import Settings
from app.instruments.instrument_registry import InstrumentRegistry
from app.journal.run_manifest import build_run_manifest
from app.market.market_context import MARKET_CONTEXT_VERSION
from app.strategies.balanced_strategy_config import BalancedStrategyConfig


def test_manifest_records_market_context_v3_and_default_benchmarks():
    settings = Settings(
        WATCHLIST='AAPL',
        EQUITY_US_SYMBOLS='AAPL',
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
        run_id='benchmark-test',
        started_at=datetime(2026, 7, 14, 13, 30, tzinfo=timezone.utc),
    )

    assert manifest['models']['market_context'] == (
        MARKET_CONTEXT_VERSION
    ) == 'market_context_v3'
    assert manifest['runtime']['context_benchmarks'] == {
        'CRYPTO': ['CRYPTO10'],
        'EQUITY_US': ['SPX500'],
        'EQUITY_EU': ['FRA40'],
    }
