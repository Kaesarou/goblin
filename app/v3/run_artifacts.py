from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.journal.serialization import serialize_value

V3_REPLAY_CHECKPOINT_SCHEMA_VERSION = 2
V3_RUN_QC_SCHEMA_VERSION = 2


def write_runtime_checkpoint(
    path: str | Path,
    *,
    runtime,
    phase: str,
    asof: datetime | None = None,
) -> None:
    actual_asof = asof or datetime.now(UTC)
    config_payload = serialize_value(runtime.config)
    canonical_config = json.dumps(
        config_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = {
        "schema_version": V3_REPLAY_CHECKPOINT_SCHEMA_VERSION,
        "run_id": runtime.run_id,
        "phase": phase,
        "asof": actual_asof,
        "strategy": runtime.config.strategy.name,
        "config_sha256": hashlib.sha256(canonical_config).hexdigest(),
        "config": config_payload,
        "account_equity": runtime._current_run_equity,
        "exit_equity_reference": runtime.runtime_state_store.export_broker_equity(),
        "inventories": list(runtime.book.inventories),
        "inventory_runtime_state": (
            runtime.runtime_state_store.export_inventory_runtime_state(runtime.book)
        ),
        "feature_state": runtime.runtime_state_store.export_feature_state(
            runtime.feature_engine
        ),
        "pending_close_confirmations": (
            runtime.executor.pending_close_confirmation_snapshot()
        ),
        "runtime_metrics": runtime._heartbeat_metrics(),
    }
    _write_gzip_json(Path(path), payload)


def write_run_qc(
    path: str | Path,
    *,
    run_paths,
    runtime,
    trade_journal,
    market_journal,
    candle_journal,
    status: str,
) -> None:
    payload = {
        "schema_version": V3_RUN_QC_SCHEMA_VERSION,
        "run_id": runtime.run_id,
        "generated_at": datetime.now(UTC),
        "status": status,
        "streams": {
            "trades": _journal_qc(trade_journal.trade_journal),
            "errors": _journal_qc(trade_journal.errors_journal),
            "market": _raw_journal_qc(market_journal),
            "candles": _raw_journal_qc(candle_journal),
        },
        "artifacts": {
            "manifest": _file_qc(run_paths.manifest),
            "research": _file_qc(run_paths.research),
            "research_summary": _file_qc(run_paths.research_summary),
            "state_start": _file_qc(run_paths.state_start),
            "state_end": _file_qc(run_paths.state_end),
        },
        "runtime": runtime._heartbeat_metrics(),
        "broker_confirmation": runtime.executor.confirmation_metrics(),
        "replay_contract": {
            "m1_candles_exhaustive_during_market_sessions": True,
            "overnight_carried_candles": False,
            "market_quotes_sampled_seconds": 10.0,
            "inventory_ledger_retained": True,
            "causal_state_checkpointed": True,
            "sqlite_required_for_offline_replay": False,
        },
    }
    _write_json(Path(path), payload)


def _journal_qc(journal) -> dict[str, Any]:
    return {
        **_file_qc(journal.path),
        "written_count": journal.written_count,
        "failed_count": journal.failed_count,
        "suppressed_count": journal.suppressed_count,
        "budget_reason": journal.budget_reason,
    }


def _raw_journal_qc(raw_journal) -> dict[str, Any]:
    return {
        **_journal_qc(raw_journal.journal),
        "sampled_out_count": raw_journal.sampled_out_count,
        "budget_suppressed_count": raw_journal.budget_suppressed_count,
        "budget_exhausted": raw_journal.budget_exhausted,
        "budget_reason": raw_journal.budget_reason,
    }


def _file_qc(path: str | Path) -> dict[str, Any]:
    actual = Path(path)
    return {
        "path": str(actual),
        "exists": actual.exists(),
        "bytes": actual.stat().st_size if actual.exists() else 0,
    }


def _write_gzip_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as handle:
        json.dump(
            serialize_value(payload),
            handle,
            sort_keys=True,
            separators=(",", ":"),
        )
    temporary.replace(path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(serialize_value(payload), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)
