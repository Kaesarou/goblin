from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunJournalPaths:
    root: Path
    trades: Path
    market: Path
    candles: Path
    errors: Path
    debug_decisions: Path
    research: Path
    research_summary: Path
    etoro_payload_schema: Path
    summary: Path
    partial_summary: Path
    manifest: Path
    state_start: Path
    state_end: Path
    run_qc: Path


def build_run_journal_paths(
    *,
    journal_path: str,
    run_id: str,
) -> RunJournalPaths:
    base = Path(journal_path).parent / 'runs' / run_id
    base.mkdir(parents=True, exist_ok=True)
    return RunJournalPaths(
        root=base,
        trades=base / 'trades.jsonl.gz',
        market=base / 'market.jsonl.gz',
        candles=base / 'candles.jsonl.gz',
        errors=base / 'errors.jsonl.gz',
        debug_decisions=base / 'debug_decisions.jsonl.gz',
        research=base / 'research.jsonl.gz',
        research_summary=base / 'research_summary.json',
        etoro_payload_schema=base / 'etoro_payload_schema.json',
        summary=base / 'summary.json',
        partial_summary=base / 'summary.partial.json',
        manifest=base / 'manifest.json',
        state_start=base / 'state_start.json.gz',
        state_end=base / 'state_end.json.gz',
        run_qc=base / 'run_qc.json',
    )


def rotate_run_journals(
    *,
    runs_root: Path,
    max_runs: int,
    current_run_id: str,
) -> tuple[str, ...]:
    """Keep all run evidence, including when startup fails repeatedly.

    Formerly this function recursively deleted older run directories on *every*
    startup. A crash loop could therefore erase an entire week of candles and
    trade journals before a successful run ever started. Retention by run count
    is unsafe: a run is not a measure of elapsed time or successfully archived
    data. The legacy call remains for compatibility, but it must never purge
    evidence. Any future retention needs a separately verified backup and an
    explicit operator-controlled process outside the trading startup path.
    """
    return ()
