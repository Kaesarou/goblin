from datetime import datetime, timezone

import pytest

from app.config.settings import Settings
from app.instruments.instrument_registry import InstrumentRegistry
from app.journal.run_paths import build_run_journal_paths
from app.main import _assert_v3_execution_mode
from app.v3.config import RecoverabilityConfig, rr5_research_config
from app.v3.manifest import build_v3_run_manifest
from app.v3.recoverability import RecoverabilityScorer


def test_v3_bootstrap_refuses_live_capital_but_allows_validation_modes():
    _assert_v3_execution_mode("paper")
    _assert_v3_execution_mode("etoro_demo")
    with pytest.raises(RuntimeError, match="not prospectively validated"):
        _assert_v3_execution_mode("etoro_live")


def test_v3_manifest_declares_authority_and_compact_log_budget(tmp_path):
    settings = Settings(
        BROKER="paper",
        WATCHLIST="AAPL",
        EQUITY_US_SYMBOLS="AAPL",
        EQUITY_EU_SYMBOLS="",
        CRYPTO_SYMBOLS="",
        JOURNAL_PATH=str(tmp_path / "logs" / "trades.jsonl"),
        POSITION_STORE_PATH=str(tmp_path / "goblin.sqlite"),
        RUN_MANIFEST_PATH=str(tmp_path / "logs" / "run_manifest.json"),
        DAILY_SUMMARY_PATH=str(tmp_path / "logs" / "daily_summary.json"),
    )
    registry = InstrumentRegistry(settings)
    config = rr5_research_config()
    config = type(config)(
        strategy=config.strategy,
        risk=config.risk,
        recoverability=RecoverabilityConfig(enabled=False),
        economics=config.economics,
        hedge=config.hedge,
    )
    run_id = "run_20260825T120000_000000Z"
    run_paths = build_run_journal_paths(
        journal_path=settings.journal_path,
        run_id=run_id,
    )
    manifest = build_v3_run_manifest(
        settings=settings,
        config=config,
        recoverability_scorer=RecoverabilityScorer.from_default_artifact(),
        instrument_registry=registry,
        symbols=["AAPL"],
        run_id=run_id,
        started_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        run_paths=run_paths,
        latest_payload_schema_path=tmp_path / "logs" / "etoro_payload_schema.json",
        removed_runs=(),
    )

    assert manifest["strategy"]["name"] == "INVENTORY_RR5_V1"
    assert manifest["strategy"]["direction_model"] is None
    assert manifest["strategy"]["wait_confirmation"] is None
    assert manifest["models"]["recoverability"]["enabled"] is False
    assert manifest["risk"]["live_authority"]["etoro_live_allowed"] is False
    assert manifest["risk"]["hedge_execution_enabled"] is False
    budget = manifest["runtime"]["journals"]["v3_log_budget"]
    assert budget["raw_market"] == "sampled_10s_per_symbol_tick_processing_in_memory"
    assert budget["raw_candles"] == "one_per_finalized_candle_with_decision_quote"
    assert budget["silent_decisions"] == "heartbeat_aggregates_only"
    assert budget["causal_restart_state"] == "sqlite_only"
    assert manifest["analysis_sources"]["decision_quote_retained_with_candle"] is True