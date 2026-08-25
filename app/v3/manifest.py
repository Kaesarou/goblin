from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.journal.raw_data_journal import (
    MARKET_RAW_MAX_BYTES,
    MARKET_RAW_MIN_FREE_BYTES,
    MARKET_RAW_SAMPLE_INTERVAL_SECONDS,
)
from app.journal.serialization import serialize_value
from app.market.data_quality import (
    QUOTE_QUALITY_CONTRACT_VERSION,
    quote_quality_contract_metadata,
)
from app.market.models import EXECUTABLE_PRICE_CONTRACT_VERSION
from app.market_data.models import MARKET_DATA_MODEL_VERSION
from app.research.microstructure import microstructure_contract_metadata
from app.research.payload_schema_observer import payload_schema_contract_metadata
from app.research.pipeline import RESEARCH_FEATURE_SET_SHA256
from app.research.reconstructibility import reconstructibility_contract_metadata
from app.research.research_state import (
    RESEARCH_STATE_SCHEMA_VERSION,
    research_state_contract_metadata,
)
from app.research.summary import (
    RESEARCH_SUMMARY_SCHEMA_VERSION,
    research_summary_contract_metadata,
)
from app.v3.runtime import V3_RUNTIME_CONTRACT_VERSION
from app.v3.state_store import V3_RUNTIME_STATE_VERSION

V3_RUN_MANIFEST_SCHEMA_VERSION = 17
_SENSITIVE_SETTINGS = {"ETORO_API_KEY", "ETORO_USER_KEY"}


def build_run_id(started_at: datetime | None = None) -> str:
    actual = started_at or datetime.now(UTC)
    return actual.strftime("run_%Y%m%dT%H%M%S_%fZ")


def build_v3_run_manifest(
    *,
    settings,
    config,
    recoverability_scorer,
    instrument_registry,
    symbols: list[str],
    run_id: str,
    started_at: datetime,
    run_paths,
    latest_payload_schema_path: Path,
    removed_runs: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    artifact = recoverability_scorer.artifact
    symbol_profiles = {
        symbol: instrument_registry.resolve(symbol)
        for symbol in symbols
    }
    benchmark_symbols = {
        asset_class.value: list(configured)
        for asset_class, configured
        in settings.benchmark_symbols_by_asset_class().items()
    }
    return {
        "schema_version": V3_RUN_MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "status": "running",
        "started_at": started_at,
        "ended_at": None,
        "code": {
            "git_commit": _resolve_git_commit(),
            "source_sha256": _resolve_code_fingerprint(),
            "python_version": platform.python_version(),
        },
        "strategy": {
            "name": config.strategy.name,
            "family": "stateful_inventory_reversion",
            "authority": "prospective_validation_only",
            "direction_model": None,
            "wait_confirmation": None,
            "config": config,
        },
        "models": {
            "market_data": MARKET_DATA_MODEL_VERSION,
            "recoverability": {
                "enabled": bool(config.recoverability.enabled),
                "runtime_role": (
                    "reentry_gate"
                    if config.recoverability.enabled
                    else "packaged_but_disabled"
                ),
                "model_version": artifact.model_version,
                "feature_manifest_version": artifact.feature_manifest_version,
                "features": list(artifact.features),
                "target_recovery_pct": artifact.target_recovery_pct,
                "adverse_barrier_pct": artifact.adverse_barrier_pct,
                "horizon_minutes": artifact.horizon_minutes,
            },
        },
        "risk": {
            "max_entry_fills": config.risk.max_entry_fills,
            "max_symbol_exposure_pct": config.risk.max_symbol_exposure_pct,
            "max_portfolio_exposure_pct": config.risk.max_portfolio_exposure_pct,
            "max_inventories": config.risk.max_inventories,
            "mark_to_market_drawdown_guard": {
                "enabled": False,
                "reason": (
                    "Requires a persistent strategy-PnL/mark-to-market contract; "
                    "paper broker account equity is intentionally static."
                ),
            },
            "live_authority": {
                "etoro_live_allowed": False,
                "paper_allowed": True,
                "etoro_demo_allowed": True,
            },
            "hedge_execution_enabled": False,
        },
        "economics": {
            "policy": config.economics.policy.value,
            "expected_holding_days": config.economics.expected_holding_days,
            "fixed_fee_assumption_per_broker_request_usd": 1.0,
            "fixed_fee_assumption_authority": "research_estimate_not_broker_actual",
            "note": (
                "The eToro5 profile remains a gross-edge prospective experiment. "
                "The fixed fee is logged/estimated but does not become a naive "
                "per-leg net-positive gate because inventory economics are path dependent."
            ),
        },
        "runtime": {
            "contracts": {
                "v3_runtime": V3_RUNTIME_CONTRACT_VERSION,
                "v3_runtime_state": V3_RUNTIME_STATE_VERSION,
                "executable_prices": EXECUTABLE_PRICE_CONTRACT_VERSION,
                "quote_quality": QUOTE_QUALITY_CONTRACT_VERSION,
                "broker_exit_translation": "pro_rata_partial_close_v1",
            },
            "market_data": {
                "mode": "websocket",
                "position_fallback": "reduce_only",
                "raw_market_policy": "sampled_per_symbol",
                "raw_market_sample_interval_seconds": (
                    MARKET_RAW_SAMPLE_INTERVAL_SECONDS
                ),
                "raw_market_max_bytes_per_run": MARKET_RAW_MAX_BYTES,
                "raw_market_min_free_bytes": MARKET_RAW_MIN_FREE_BYTES,
                "raw_candle_policy": (
                    "one_event_per_finalized_m1_candle_with_decision_quote"
                ),
            },
            "broker_execution": {
                "aggregate_profit_exit_fraction": config.strategy.close_qty_pct,
                "partial_close_supported": True,
                "partial_close_request_field": "UnitsToDeduct",
                "allocation": "same_fraction_of_every_active_broker_leg",
                "confirmed_units_required_for_partial_accounting": True,
            },
            "quote_quality_policy": quote_quality_contract_metadata(),
            "watchlist": symbols,
            "context_benchmarks": benchmark_symbols,
            "symbol_profiles": symbol_profiles,
            "settings": _sanitized_settings_snapshot(settings),
            "journals": {
                "run_root": str(run_paths.root),
                "compressed": True,
                "detail_level": settings.journal_detail_level,
                "max_runs": settings.journal_max_runs,
                "removed_runs": list(removed_runs),
                "v3_log_budget": {
                    "raw_market": "sampled_10s_per_symbol_tick_processing_in_memory",
                    "raw_market_max_bytes_per_run": MARKET_RAW_MAX_BYTES,
                    "raw_market_min_free_bytes": MARKET_RAW_MIN_FREE_BYTES,
                    "raw_candles": "one_per_finalized_candle_with_decision_quote",
                    "broker_ledger": "material_events_only",
                    "silent_decisions": "heartbeat_aggregates_only",
                    "decision_details": "material_state_changes_only",
                    "market_quality": "transition_only",
                    "causal_restart_state": "sqlite_only",
                },
            },
            "research": {
                "activation_status": (
                    "active" if settings.research_enabled else "disabled"
                ),
                "strictly_read_only": True,
                "candidate_generator_independent": True,
                "journal_encoding": "compact_jsonl_gzip",
                "combined_feature_set_sha256": RESEARCH_FEATURE_SET_SHA256,
                "research_state": research_state_contract_metadata(),
                "microstructure": microstructure_contract_metadata(),
                "reconstructibility": reconstructibility_contract_metadata(),
                "health_summary": research_summary_contract_metadata(),
                "payload_schema_observer": payload_schema_contract_metadata(),
            },
        },
        "analysis_sources": {
            "run_id": run_id,
            "schemas": {
                "research_state": RESEARCH_STATE_SCHEMA_VERSION,
                "research_summary": RESEARCH_SUMMARY_SCHEMA_VERSION,
            },
            "market_stream": str(run_paths.market),
            "candle_stream": str(run_paths.candles),
            "trade_stream": str(run_paths.trades),
            "error_stream": str(run_paths.errors),
            "research_stream": str(run_paths.research),
            "research_summary": str(run_paths.research_summary),
            "etoro_payload_schema": str(run_paths.etoro_payload_schema),
            "raw_market_retained": "sampled_10s_per_symbol",
            "raw_candles_retained": True,
            "decision_quote_retained_with_candle": True,
            "v3_inventory_events_retained": True,
            "v3_causal_state_retained_in_sqlite": True,
        },
        "files": {
            "manifest": str(run_paths.manifest),
            "latest_manifest": settings.run_manifest_path,
            "summary": str(run_paths.summary),
            "latest_summary": settings.daily_summary_path,
            "partial_summary": str(run_paths.partial_summary),
            "trades": str(run_paths.trades),
            "errors": str(run_paths.errors),
            "market": str(run_paths.market),
            "candles": str(run_paths.candles),
            "debug_decisions": str(run_paths.debug_decisions),
            "research": str(run_paths.research),
            "research_summary": str(run_paths.research_summary),
            "etoro_payload_schema": str(run_paths.etoro_payload_schema),
            "latest_etoro_payload_schema": str(latest_payload_schema_path),
            "state_db": settings.position_store_path,
        },
    }


def write_run_manifest(path: str | Path, manifest: dict[str, Any]) -> None:
    _write_json_atomically(Path(path), manifest)


def finalize_run_manifest(
    path: str | Path,
    *,
    ended_at: datetime | None = None,
    status: str = "completed",
    summary: dict[str, Any] | None = None,
    runtime_metrics: dict[str, Any] | None = None,
) -> None:
    manifest_path = Path(path)
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = status
    manifest["ended_at"] = ended_at or datetime.now(UTC)
    if summary is not None:
        manifest["summary"] = {
            "market_data": summary.get("market_data", {}),
            "errors": summary.get("errors", {}),
        }
    if runtime_metrics is not None:
        manifest["v3_result"] = runtime_metrics
    _write_json_atomically(manifest_path, manifest)


def _sanitized_settings_snapshot(settings) -> dict[str, Any]:
    values = settings.model_dump(by_alias=True)
    return {
        key: value
        for key, value in values.items()
        if key not in _SENSITIVE_SETTINGS
    }


def _resolve_git_commit() -> str | None:
    for variable_name in ("GIT_COMMIT", "GITHUB_SHA", "SOURCE_VERSION"):
        value = os.getenv(variable_name)
        if value and value.strip().lower() not in {"unknown", "local"}:
            return value.strip()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def _resolve_code_fingerprint() -> str | None:
    root = Path(__file__).resolve().parents[1]
    source_files = sorted(path for path in root.rglob("*.py") if path.is_file())
    if not source_files:
        return None
    digest = hashlib.sha256()
    for source_file in source_files:
        relative = source_file.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source_file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_json_atomically(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(serialize_value(value), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)
