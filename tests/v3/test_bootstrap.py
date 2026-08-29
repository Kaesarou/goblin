from datetime import datetime, timezone

import pytest

from app.brokers.etoro.get_rate_governor import (
    ETORO_GET_429_FALLBACK_SECONDS,
    ETORO_GET_MAX_REQUESTS_PER_WINDOW,
    ETORO_GET_RATE_WINDOW_SECONDS,
)
from app.config.settings import Settings
from app.instruments.instrument_registry import InstrumentRegistry
from app.journal.run_paths import build_run_journal_paths
from app.main import _assert_v3_execution_mode
from app.v3.config import RecoverabilityConfig, etoro5_research_config, rr5_research_config
from app.v3.live_execution import (
    BROKER_RECONCILIATION_INTERVAL_SECONDS,
    CONFIRMATION_STALE_HALT_SECONDS,
    POINT_M_DUST_NOTIONAL_USD,
)
from app.v3.manifest import build_v3_run_manifest
from app.v3.recoverability import RecoverabilityScorer


def test_v3_bootstrap_refuses_live_capital_but_allows_validation_modes():
    _assert_v3_execution_mode("paper")
    _assert_v3_execution_mode("etoro_demo")
    with pytest.raises(RuntimeError, match="not prospectively validated"):
        _assert_v3_execution_mode("etoro_live")


def test_v3_manifest_declares_authority_and_replayable_log_budget(tmp_path):
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

    assert manifest["schema_version"] == 19
    assert manifest["strategy"]["name"] == "INVENTORY_RR5_V1"
    assert manifest["strategy"]["direction_model"] is None
    assert manifest["strategy"]["wait_confirmation"] is None
    assert manifest["models"]["recoverability"]["enabled"] is False
    assert manifest["risk"]["live_authority"]["etoro_live_allowed"] is False
    assert manifest["risk"]["hedge_execution_enabled"] is False
    broker_execution = manifest["runtime"]["broker_execution"]
    assert broker_execution["confirmed_open_units_required"] is True
    assert broker_execution["open_units_authority"] == "etoro_order_openingData.units"
    assert broker_execution["close_confirmation_stale_halt_seconds"] == (
        CONFIRMATION_STALE_HALT_SECONDS
    )
    reconciliation = broker_execution["broker_units_reconciliation"]
    assert reconciliation == {
        "startup": True,
        "periodic_interval_seconds": BROKER_RECONCILIATION_INTERVAL_SECONDS,
        "query_lane": "shared_serial_with_close_confirmation",
        "close_confirmation_priority": True,
        "mismatch_policy": "halt_new_risk_reduce_only_allowed",
        "query_failure_policy": "halt_new_risk_reduce_only_allowed",
        "pending_reduction_policy": (
            "halt_new_risk_until_economic_fill_confirmed"
        ),
        "stale_snapshot_policy": "discard_and_retry",
    }
    assert broker_execution["etoro_get_rate_governor"] == {
        "max_requests_per_window": ETORO_GET_MAX_REQUESTS_PER_WINDOW,
        "window_seconds": ETORO_GET_RATE_WINDOW_SECONDS,
        "global_429_cooldown": True,
        "honor_retry_after": True,
        "fallback_429_cooldown_seconds": ETORO_GET_429_FALLBACK_SECONDS,
        "post_close_mutations_governed": False,
    }
    budget = manifest["runtime"]["journals"]["v3_log_budget"]
    assert budget["raw_market"] == "sampled_10s_per_symbol_tick_processing_in_memory"
    assert budget["raw_market_max_bytes_per_run"] == 1024 * 1024 * 1024
    assert budget["raw_candles"] == "one_per_finalized_candle_with_decision_quote"
    assert budget["silent_decisions"] == "heartbeat_aggregates_only"
    assert budget["trade_budget_activation"] == (
        "unique_in_stream_marker_plus_heartbeat_metrics"
    )
    assert budget["causal_restart_state"] == (
        "sqlite_restart_cache_plus_start_end_run_checkpoints"
    )
    assert manifest["analysis_sources"]["decision_quote_retained_with_candle"] is True
    assert manifest["analysis_sources"]["v3_causal_state_retained_in_run_checkpoints"] is True
    assert manifest["analysis_sources"]["sqlite_required_for_offline_replay"] is False
    assert manifest["files"]["state_start"].endswith("state_start.json.gz")
    assert manifest["files"]["state_end"].endswith("state_end.json.gz")
    assert manifest["files"]["run_qc"].endswith("run_qc.json")


def test_etoro5_strategy_contract_remains_frozen():
    config = etoro5_research_config()

    assert config.strategy.name == "INVENTORY_RR5_ETORO5_V1"
    assert config.risk.max_inventories == 5
    assert config.risk.max_entry_fills == 5
    assert config.strategy.close_qty_pct == pytest.approx(0.84)
    assert config.risk.max_symbol_exposure_pct == pytest.approx(0.04)
    assert config.risk.max_portfolio_exposure_pct == pytest.approx(0.15)
    assert POINT_M_DUST_NOTIONAL_USD == 10.0
