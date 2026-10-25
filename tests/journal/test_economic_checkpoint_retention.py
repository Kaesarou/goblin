"""Critical broker economic evidence outlives trade-journal soft quota."""
import gzip
import json

from app.journal.jsonl_journal import JsonlJournal


def records(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def test_broker_equity_and_checkpoint_survive_soft_cap_without_unlimiting_disk(tmp_path):
    p = tmp_path / "trades.jsonl.gz"
    journal = JsonlJournal(str(p), stream_name="trades", run_id="week",
                           soft_max_bytes=1, hard_max_bytes=100_000,
                           min_free_bytes=0)
    assert journal.write("v3_market_quality_transition", {"noise": "x" * 64})
    assert journal.write("v3_market_quality_transition", {"noise": "x" * 64})
    assert journal.write("v3_account_equity_snapshot", {
        "equity": 98580.48, "source": "alpaca_broker",
        "observed_at": "2026-10-23T21:00:00+00:00",
    })
    assert journal.write("v3_economic_checkpoint", {
        "equity": 98580.48, "realized_net_pnl_status": "unverified_costs_and_fx",
    })
    assert journal.write("session_heartbeat", {"date": "2026-10-23"})
    assert journal.write("v3_inventory_event", {"action_id": "sell-confirmed"})
    events = records(p)
    assert [x["event_type"] for x in events] == [
        "v3_market_quality_transition", "trade_journal_budget_active",
        "v3_account_equity_snapshot", "v3_economic_checkpoint",
        "session_heartbeat", "v3_inventory_event",
    ]
    assert journal.suppressed_count == 1
    assert journal.budget_metrics()["hard_cap_active"] is False


def test_hard_cap_blocks_economic_checkpoints_too(tmp_path):
    p = tmp_path / "trades.jsonl.gz"
    journal = JsonlJournal(str(p), stream_name="trades", run_id="week",
                           soft_max_bytes=1, hard_max_bytes=1,
                           min_free_bytes=0)
    journal.write("v3_account_equity_snapshot", {"equity": 1})
    journal.write("v3_economic_checkpoint", {"equity": 2})
    assert [e["payload"]["equity"] for e in records(p)] == [1]
    assert journal.budget_reason == "hard_max_bytes"
    assert journal.suppressed_count == 1
