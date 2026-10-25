"""Regression for real production AnalysisJournal routing around checkpoint budget."""
import gzip
import json
from types import SimpleNamespace

from app.journal.analysis_journal import AnalysisJournal
from app.journal.jsonl_journal import JsonlJournal
from app.runtime.broker_task_runner import BrokerTaskCompletion, BrokerTaskLane
from tests.v3.test_runtime_equity_authority import runtime_for_test


def run_equity_completion(tmp_path, *, soft, hard):
    runtime = runtime_for_test(tmp_path)
    physical = JsonlJournal(
        str(tmp_path / "trades.jsonl.gz"),
        stream_name="trades",
        soft_max_bytes=soft,
        hard_max_bytes=hard,
        min_free_bytes=0,
    )
    runtime.trade_journal = AnalysisJournal(
        trade_journal=physical,
        errors_journal=JsonlJournal(str(tmp_path / "errors.jsonl")),
        summary_path=str(tmp_path / "summary.json"),
        detail_level="normal",
        write_partial_summary=False,
    )
    runtime.maintenance_runner = SimpleNamespace(
        drain=lambda: [BrokerTaskCompletion(
            "equity", "v3_account_equity", BrokerTaskLane.QUERY,
            None, 98_580.48,
        )],
    )
    runtime._drain_broker_tasks()
    with gzip.open(physical.path, "rt", encoding="utf-8") as reader:
        events = [json.loads(line)["event_type"] for line in reader]
    return runtime, physical, events


def test_real_analysis_journal_soft_cap_preserves_checkpoint_without_false_error(tmp_path):
    runtime, physical, events = run_equity_completion(tmp_path, soft=1, hard=100000)
    assert "v3_account_equity_snapshot" in events
    assert "v3_economic_checkpoint" in events
    assert physical.written_count >= 2
    assert runtime.metrics["economic_checkpoint_write_failures"] == 0
    assert runtime._last_economic_checkpoint_bucket is not None


def test_real_analysis_journal_hard_cap_flags_missing_checkpoint(tmp_path):
    runtime, physical, events = run_equity_completion(tmp_path, soft=1, hard=1)
    assert "v3_account_equity_snapshot" in events
    assert "v3_economic_checkpoint" not in events
    assert physical.suppressed_count >= 1
    assert runtime.metrics["economic_checkpoint_write_failures"] == 1
    assert runtime._last_economic_checkpoint_bucket is None
