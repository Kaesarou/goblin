import gzip
import json
from types import SimpleNamespace

import pytest

from app.brokers.etoro.account_equity_mapper import ACCOUNT_EQUITY_SOURCE
from app.journal.run_paths import build_run_journal_paths
from app.v3.manifest import build_v3_run_manifest, finalize_run_manifest, write_run_manifest
from app.v3.run_artifacts import write_run_qc, write_runtime_checkpoint
from tests.v3.test_action_scoped_close_lifecycle import make_executor, submit_close
from tests.v3.test_runtime import NOW
from tests.v3.test_runtime_equity_authority import runtime_for_test


def test_failed_run_checkpoints_qc_manifest_and_heartbeat_share_authority(tmp_path):
    runtime = runtime_for_test(tmp_path)
    executor = make_executor(tmp_path)
    submit_close(executor, "A")
    executor.broker.units = 0.16
    executor.verify_known_broker_legs()
    runtime.executor, runtime.book = executor, executor.book
    runtime.runtime_state_store.save_broker_equity(
        value=100_000, observed_at=NOW, source=ACCOUNT_EQUITY_SOURCE,
    )
    runtime._restore_exit_planning_equity_reference()
    paths = build_run_journal_paths(journal_path=tmp_path / "logs/trades.jsonl", run_id=runtime.run_id)
    manifest = build_v3_run_manifest(
        settings=runtime.settings, config=runtime.config,
        recoverability_scorer=runtime.planner.recoverability_scorer,
        instrument_registry=runtime.instrument_registry, symbols=runtime.symbols,
        run_id=runtime.run_id, started_at=NOW, run_paths=paths,
        latest_payload_schema_path=tmp_path / "payload-schema.json", removed_runs=(),
    )
    assert manifest["schema_version"] == 20
    contracts = manifest["runtime"]["contracts"]
    assert contracts["v3_runtime"] == "inventory_runtime_v3_6"
    assert contracts["v3_runtime_state"] == "v3_runtime_state_v1"
    assert contracts["broker_equity_reference"] == "v3_broker_equity_reference_v1"
    assert contracts["close_retry_scheduler"] == "v3_close_retry_scheduler_v1"
    assert contracts["replay_checkpoint"] == contracts["run_qc"] == 2
    assert manifest["runtime"]["account_equity"]["fallback"] is None
    write_run_manifest(paths.manifest, manifest)
    write_runtime_checkpoint(paths.state_start, runtime=runtime, phase="start")
    with gzip.open(paths.state_start, "rt") as handle:
        start = json.load(handle)
    assert start["account_equity"] is None
    assert start["exit_equity_reference"]["value"] == 100_000
    assert start["runtime_metrics"]["executor_new_risk_allowed"]
    assert not start["runtime_metrics"]["v3_new_risk_allowed"]

    def fail(*args, **kwargs):
        raise RuntimeError("test runtime failure")

    runtime._started = True
    runtime._refresh_sessions = fail
    runtime.stop = lambda: None
    with pytest.raises(RuntimeError, match="test runtime failure"):
        runtime.run()
    write_runtime_checkpoint(paths.state_end, runtime=runtime, phase="end")
    journal = SimpleNamespace(path=paths.trades, written_count=0, failed_count=0,
                              suppressed_count=0, budget_reason=None)
    raw = SimpleNamespace(journal=journal, sampled_out_count=0, budget_suppressed_count=0,
                          budget_exhausted=False, budget_reason=None)
    write_run_qc(paths.run_qc, run_paths=paths, runtime=runtime,
                 trade_journal=SimpleNamespace(trade_journal=journal, errors_journal=journal),
                 market_journal=raw, candle_journal=raw, status="failed")
    finalize_run_manifest(paths.manifest, status="failed", runtime_metrics=runtime._heartbeat_metrics())
    with gzip.open(paths.state_end, "rt") as handle:
        end = json.load(handle)
    qc = json.loads(paths.run_qc.read_text())
    manifest = json.loads(paths.manifest.read_text())
    assert end["schema_version"] == qc["schema_version"] == 2
    assert qc["status"] == manifest["status"] == "failed"
    assert qc["replay_contract"]["overnight_carried_candles"] is False
    snapshots = [end["runtime_metrics"], qc["runtime"], manifest["v3_result"], runtime._heartbeat_metrics()]
    for snapshot in snapshots:
        assert snapshot["stop_reason"] == "error"
        assert snapshot["broker_confirmation"]["active_close_mutation_count"] == 0
        assert snapshot["broker_confirmation"]["pending_economic_confirmation_count"] == 1
        pending = snapshot["pending_close_confirmations"][0]
        assert pending["quantity_resolved"] and pending["attribution_confident"]
        assert pending["economics_pending"] and not pending["mutation_active"]
        assert pending["next_attempt_at"] is not None
        assert pending["attempt_count"] == 0
        assert not snapshot["current_run_equity_available"]
        assert not snapshot["v3_new_risk_allowed"]
        assert "current_run_equity_available" in snapshot["new_risk_blockers"]["AAPL"]
