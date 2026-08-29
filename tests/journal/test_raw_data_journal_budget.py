import gzip
from datetime import datetime, timedelta, timezone

from app.journal.jsonl_journal import JsonlJournal
from app.journal.raw_data_journal import (
    MARKET_RAW_MAX_BYTES,
    MARKET_RAW_MIN_FREE_BYTES,
    MARKET_RAW_SAMPLE_INTERVAL_SECONDS,
    RawDataJournal,
)


def _payload(symbol: str, at: datetime) -> dict:
    return {
        "symbol": symbol,
        "snapshot": {
            "timestamp": at,
            "received_at": at,
            "bid": 100.0,
            "ask": 100.1,
            "last": 100.05,
        },
    }


def _line_count(path) -> int:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def test_market_stream_is_sampled_per_symbol_every_ten_seconds(tmp_path):
    path = tmp_path / "market.jsonl.gz"
    observed = []
    journal = RawDataJournal(
        JsonlJournal(str(path), stream_name="market", compact=True),
        lambda event_type, payload, written: observed.append(
            (event_type, payload["symbol"], written)
        ),
    )
    start = datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)

    assert journal.write("market_price_changed", _payload("AAPL", start))
    assert journal.write(
        "market_price_changed", _payload("AAPL", start + timedelta(seconds=1))
    )
    assert journal.write(
        "market_price_changed", _payload("MSFT", start + timedelta(seconds=1))
    )
    assert journal.write(
        "market_price_changed", _payload("AAPL", start + timedelta(seconds=10))
    )

    assert MARKET_RAW_SAMPLE_INTERVAL_SECONDS == 10.0
    assert journal.journal.written_count == 3
    assert journal.suppressed_count == 1
    assert journal.sampled_out_count == 1
    assert journal.budget_suppressed_count == 0
    assert _line_count(path) == 3
    assert len(observed) == 3


def test_market_stream_has_week_safe_physical_budget_defaults(tmp_path):
    journal = RawDataJournal(
        JsonlJournal(str(tmp_path / "market.jsonl.gz"), stream_name="market"),
        lambda *_: None,
    )

    assert journal.max_bytes == MARKET_RAW_MAX_BYTES == 1024 * 1024 * 1024
    assert journal.min_free_bytes == MARKET_RAW_MIN_FREE_BYTES == 1024 * 1024 * 1024


def test_budget_exhaustion_is_transition_only_and_not_a_write_failure(tmp_path):
    path = tmp_path / "bounded.jsonl.gz"
    states = []
    observed = []
    journal = RawDataJournal(
        JsonlJournal(str(path), stream_name="bounded"),
        lambda *args: observed.append(args),
        sample_interval_seconds=0,
        max_bytes=1,
        min_free_bytes=0,
        state_observer=lambda event_type, payload: states.append(
            (event_type, payload["reason"])
        ),
    )
    start = datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)

    assert journal.write("market_price_changed", _payload("AAPL", start))
    assert journal.write(
        "market_price_changed", _payload("AAPL", start + timedelta(seconds=1))
    )
    assert journal.write(
        "market_price_changed", _payload("AAPL", start + timedelta(seconds=2))
    )

    assert journal.journal.written_count == 1
    assert journal.suppressed_count == 2
    assert journal.sampled_out_count == 0
    assert journal.budget_suppressed_count == 2
    assert journal.budget_exhausted
    assert journal.budget_reason == "max_bytes"
    assert states == [("raw_journal_budget_exhausted", "max_bytes")]
    assert len(observed) == 1


def test_non_market_raw_stream_remains_exhaustive_by_default(tmp_path):
    path = tmp_path / "candles.jsonl.gz"
    journal = RawDataJournal(
        JsonlJournal(str(path), stream_name="candles"),
        lambda *_: None,
    )
    start = datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)

    assert journal.write("candle_finalized", _payload("AAPL", start))
    assert journal.write(
        "candle_finalized", _payload("AAPL", start + timedelta(seconds=1))
    )

    assert journal.journal.written_count == 2
    assert journal.suppressed_count == 0
    assert journal.sampled_out_count == 0
    assert journal.budget_suppressed_count == 0
    assert _line_count(path) == 2
