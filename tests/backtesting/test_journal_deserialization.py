from datetime import UTC, datetime

from app.backtesting.journal_deserialization import (
    trade_candidate_from_journal,
)
from app.instruments.models import AssetClass


def test_candidate_deserialization_restores_typed_price_and_context_contracts():
    timestamp = "2026-07-31T15:00:00+00:00"
    raw = {
        "symbol": "AMD",
        "snapshot": {
            "symbol": "AMD",
            "bid": 99.9,
            "ask": 100.1,
            "last": 99.9,
            "timestamp": timestamp,
            "received_at": timestamp,
            "price_source": "broker_last",
            "timestamp_source": "broker",
        },
        "candle": {
            "symbol": "AMD",
            "timeframe_seconds": 60,
            "open": 99.8,
            "high": 100.0,
            "low": 99.7,
            "close": 99.9,
            "volume": None,
            "opened_at": "2026-07-31T14:59:00+00:00",
            "closed_at": timestamp,
        },
        "signal": {
            "action": "SELL",
            "setup_quality": 0.8,
            "reason": "test",
            "metadata": {"atr_percent": 0.3},
        },
        "rank_reason": "test",
        "session_key": ("EQUITY_US:2026-07-31T15:30:00+02:00:2026-07-31T22:00:00+02:00"),
        "market_context": {
            "version": "market_context_v2",
            "as_of": timestamp,
            "asset_class": "EQUITY_US",
            "regime": "risk_off",
            "alignment": "aligned",
            "benchmark": {
                "symbol": "SPX500",
                "available": True,
                "direction": "bearish",
                "session_return_percent": -0.2,
                "momentum_percent": -0.1,
                "spread_percent": 0.02,
                "snapshot_age_seconds": 1.0,
            },
            "breadth": {
                "available": True,
                "direction": "bearish",
                "eligible_symbols": 50,
                "valid_symbols": 50,
                "coverage_ratio": 1.0,
                "advancing_count": 10,
                "declining_count": 35,
                "unchanged_count": 5,
                "advancing_ratio": 0.2,
                "median_session_return_percent": -0.1,
            },
            "sector": {
                "sector": None,
                "available": False,
                "direction": "unknown",
                "member_count": 0,
                "valid_member_count": 0,
                "advancing_ratio": None,
                "median_session_return_percent": None,
                "benchmark_symbol": None,
                "benchmark_return_percent": None,
            },
            "symbol_session_return_percent": -0.3,
            "symbol_relative_strength_percent": -0.1,
            "reasons": ["benchmark_available"],
        },
        "multi_timeframe_context": None,
        "entry_decision_config": {
            "minimum_extension_to_tp_ratio": 0.2,
            "minimum_structural_retest_score": 25.0,
            "maximum_retest_candles": 5,
        },
    }

    candidate = trade_candidate_from_journal(raw)

    assert candidate.snapshot.timestamp == datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    assert candidate.snapshot.executable_entry_price("SELL") == 99.9
    assert candidate.snapshot.executable_exit_price("SELL") == 100.1
    assert candidate.market_context is not None
    assert candidate.market_context.asset_class is AssetClass.EQUITY_US
    assert candidate.market_context.benchmark.momentum_percent == -0.1
