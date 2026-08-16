from __future__ import annotations

import shutil
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
    )


def rotate_run_journals(
    *,
    runs_root: Path,
    max_runs: int,
    current_run_id: str,
) -> tuple[str, ...]:
    limit = max(1, max_runs)
    if not runs_root.exists():
        return ()
    directories = sorted(
        (
            path
            for path in runs_root.iterdir()
            if path.is_dir() and path.name != current_run_id
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed: list[str] = []
    for path in directories[max(0, limit - 1):]:
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path.name)
    return tuple(removed)
