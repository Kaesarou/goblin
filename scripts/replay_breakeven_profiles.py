from __future__ import annotations

import argparse
import bisect
import gzip
import hashlib
import heapq
import io
import json
import os
import statistics
import zipfile
from collections import Counter, deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.backtesting.journal_deserialization import (
    market_snapshot_from_journal,
    parse_datetime,
    trade_candidate_from_journal,
)
from app.backtesting.stateful_managed_replay import (
    ReplayCandidateBatch,
    StatefulManagedReplay,
)
from app.config.settings import Settings
from app.execution.breakeven_profile import BreakevenProfileName
from app.execution.position_close_reason import POSITION_CLOSE_TAXONOMY_VERSION
from app.execution.position_lifecycle_engine import (
    POSITION_LIFECYCLE_CONTRACT_VERSION,
)
from app.execution.position_models import POSITION_ECONOMICS_CONTRACT_VERSION
from app.execution.scoring.frozen_logistic import FrozenOutcomeProbabilityModel
from app.execution.scoring.managed_outcome import FrozenManagedOutcomeModel
from app.execution.scoring.managed_outcome_model_contract import (
    MANAGED_SELECTION_POLICY_VERSION,
)
from app.instruments.models import AssetClass
from app.journal.serialization import serialize_value
from app.market.models import EXECUTABLE_PRICE_CONTRACT_VERSION, MarketSnapshot
from app.risk.trade_cooldown import TRADE_COOLDOWN_CONTRACT_VERSION
from app.risk.trade_cost_model import TRADE_COST_CONTRACT_VERSION
from app.strategies.balanced_strategy_config import BalancedStrategyConfig

REPLAY_REPORT_SCHEMA_VERSION = 1


class SegmentedReader(io.RawIOBase):
    def __init__(self, paths: list[Path]):
        self.paths = paths
        self.sizes = [path.stat().st_size for path in paths]
        self.starts: list[int] = []
        total = 0
        for size in self.sizes:
            self.starts.append(total)
            total += size
        self.total_size = total
        self.position = 0
        self.handles = [path.open("rb") for path in paths]

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = self.position + offset
        elif whence == os.SEEK_END:
            target = self.total_size + offset
        else:
            raise ValueError(f"Unsupported seek mode: {whence}")
        if target < 0:
            raise ValueError("Negative segmented archive offset.")
        self.position = min(target, self.total_size)
        return self.position

    def read(self, size: int = -1) -> bytes:
        if self.position >= self.total_size:
            return b""
        requested = (
            self.total_size - self.position
            if size is None or size < 0
            else min(size, self.total_size - self.position)
        )
        chunks: list[bytes] = []
        remaining = requested
        while remaining > 0:
            index = max(
                0,
                min(
                    bisect.bisect_right(self.starts, self.position) - 1,
                    len(self.paths) - 1,
                ),
            )
            local_offset = self.position - self.starts[index]
            available = self.sizes[index] - local_offset
            take = min(remaining, available)
            handle = self.handles[index]
            handle.seek(local_offset)
            chunk = handle.read(take)
            if not chunk:
                break
            chunks.append(chunk)
            self.position += len(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        for handle in self.handles:
            handle.close()
        super().close()


@dataclass(frozen=True)
class ReplaySource:
    run_id: str
    parts: tuple[Path, ...]

    @property
    def trades_member(self) -> str:
        return f"logs/runs/{self.run_id}/trades.jsonl.gz"

    @property
    def market_member(self) -> str:
        return f"logs/runs/{self.run_id}/market.jsonl.gz"

    @property
    def manifest_member(self) -> str:
        return f"logs/runs/{self.run_id}/manifest.json"


@dataclass(frozen=True)
class ReplayDayPlan:
    day: date
    incomplete: bool
    sources: tuple[ReplaySource, ...]


@dataclass
class _BatchAccumulator:
    occurred_at: datetime
    equity: float | None = None
    candidates: dict[str, Any] = field(default_factory=dict)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Replay MANAGED_EDGE_V1 with corrected executable prices and "
            "compare the baseline and delayed equity breakeven profiles."
        )
    )
    parser.add_argument("plan", type=Path)
    parser.add_argument("output_json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    parser.add_argument("--validate-archives", action="store_true")
    args = parser.parse_args()

    plan_path = args.plan.resolve()
    raw_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    timezone_name = str(raw_plan.get("timezone") or "Europe/Paris")
    timezone = ZoneInfo(timezone_name)
    days = _parse_plan(raw_plan, base_directory=plan_path.parent)

    if args.validate_archives:
        _validate_unique_archives(days)

    source_provenance = _source_provenance(days)
    managed_model = FrozenManagedOutcomeModel.load()
    outcome_model = FrozenOutcomeProbabilityModel.load()
    day_reports: list[dict[str, Any]] = []

    for day_plan in days:
        print(f"replay-start day={day_plan.day.isoformat()}")
        batches, manifests = _load_candidate_batches(day_plan, timezone)
        settings = _build_settings(
            batches=batches,
            manifests=manifests,
            timezone_name=timezone_name,
        )
        scenarios = {
            "corrected_baseline_v1": StatefulManagedReplay(
                settings=settings,
                strategy_profile=BalancedStrategyConfig(
                    breakeven_profile_name=(BreakevenProfileName.CORRECTED_BASELINE_V1)
                ),
                scenario_name="corrected_baseline_v1",
            ),
            "delayed_equity_trigger_v1": StatefulManagedReplay(
                settings=settings,
                strategy_profile=BalancedStrategyConfig(
                    breakeven_profile_name=(BreakevenProfileName.DELAYED_EQUITY_TRIGGER_V1)
                ),
                scenario_name="delayed_equity_trigger_v1",
            ),
        }
        _run_day(
            day_plan=day_plan,
            batches=batches,
            scenarios=scenarios,
            timezone=timezone,
        )
        results = {name: replay.result() for name, replay in scenarios.items()}
        day_reports.append(
            {
                "date": day_plan.day.isoformat(),
                "incomplete": day_plan.incomplete,
                "sources": [source.run_id for source in day_plan.sources],
                "candidate_batches": len(batches),
                "candidate_universe": sum(len(batch.candidates) for batch in batches),
                "scenarios": results,
                "comparison": _compare_day(results),
            }
        )
        print(
            "replay-complete "
            f"day={day_plan.day.isoformat()} "
            f"baseline_net={results['corrected_baseline_v1']['pnl']['realized_net']} "
            f"variant_net={results['delayed_equity_trigger_v1']['pnl']['realized_net']}"
        )

    aggregates = {
        scenario: _aggregate_scenario(day_reports, scenario)
        for scenario in (
            "corrected_baseline_v1",
            "delayed_equity_trigger_v1",
        )
    }
    report = {
        "schema_version": REPLAY_REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone(),
        "methodology": {
            "archive_crc_validation": ("passed" if args.validate_archives else "not_requested"),
            "selection_policy": MANAGED_SELECTION_POLICY_VERSION,
            "candidate_universe": (
                "candidate_economics plus candidates rejected by the live "
                "cooldown, deduplicated by decision candle and candidate id"
            ),
            "entry_price": "BUY ask; SELL bid; executable estimate",
            "exit_price": "BUY bid; SELL ask; executable estimate",
            "post_trade_costs": "explicit fees only; no second spread deduction",
            "broker_fills": "unavailable for counterfactual replay",
            "position_lifecycle": POSITION_LIFECYCLE_CONTRACT_VERSION,
            "portfolio_order": (
                "cooldown -> current economics/router/models -> MANAGED gates "
                "-> top-N -> risk/capacity with no backfill -> order estimate"
            ),
            "incomplete_day_policy": (
                "leave positions open and report executable mark-to-market at "
                "the final recorded tick"
            ),
        },
        "contracts": {
            "executable_prices": EXECUTABLE_PRICE_CONTRACT_VERSION,
            "position_economics": POSITION_ECONOMICS_CONTRACT_VERSION,
            "trade_costs": TRADE_COST_CONTRACT_VERSION,
            "position_close_taxonomy": POSITION_CLOSE_TAXONOMY_VERSION,
            "trade_cooldown": TRADE_COOLDOWN_CONTRACT_VERSION,
        },
        "models": {
            "outcome_probability_version": outcome_model.version,
            "outcome_probability_sha256": outcome_model.artifact_sha256,
            "managed_outcome_version": managed_model.version,
            "managed_outcome_sha256": managed_model.artifact_sha256,
            "managed_outcome_provenance": dict(managed_model.provenance),
        },
        "source_provenance": source_provenance,
        "days": day_reports,
        "aggregates": aggregates,
        "comparison": _compare_aggregates(aggregates, day_reports),
    }
    serializable = serialize_value(report)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(serializable, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if args.output_markdown is not None:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(
            _render_markdown(serializable),
            encoding="utf-8",
        )


def _parse_plan(raw: dict[str, Any], *, base_directory: Path) -> list[ReplayDayPlan]:
    result: list[ReplayDayPlan] = []
    for raw_day in raw["days"]:
        sources = []
        for raw_source in raw_day["sources"]:
            parts = tuple(_resolve_path(item, base_directory) for item in raw_source["parts"])
            if not all(path.is_file() for path in parts):
                missing = [str(path) for path in parts if not path.is_file()]
                raise FileNotFoundError(f"Missing replay archive parts: {missing}")
            sources.append(ReplaySource(run_id=str(raw_source["run_id"]), parts=parts))
        result.append(
            ReplayDayPlan(
                day=date.fromisoformat(raw_day["date"]),
                incomplete=bool(raw_day.get("incomplete", False)),
                sources=tuple(sources),
            )
        )
    return result


def _resolve_path(value: str, base_directory: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base_directory / path).resolve()


def _open_archive(source: ReplaySource) -> tuple[SegmentedReader, zipfile.ZipFile]:
    reader = SegmentedReader(list(source.parts))
    return reader, zipfile.ZipFile(reader)


def _load_candidate_batches(
    day_plan: ReplayDayPlan,
    timezone: ZoneInfo,
) -> tuple[list[ReplayCandidateBatch], list[dict[str, Any]]]:
    groups: dict[str, _BatchAccumulator] = {}
    equity_points: list[tuple[datetime, float]] = []
    manifests: list[dict[str, Any]] = []
    for source in day_plan.sources:
        reader, archive = _open_archive(source)
        try:
            _require_members(
                archive,
                (source.trades_member, source.market_member, source.manifest_member),
            )
            manifests.append(json.loads(archive.read(source.manifest_member)))
            with (
                archive.open(source.trades_member) as zipped,
                gzip.GzipFile(fileobj=zipped) as stream,
            ):
                for raw_line in stream:
                    if (
                        b"candidate_economics" not in raw_line
                        and b"cooldown_blocked" not in raw_line
                    ):
                        continue
                    event = json.loads(raw_line)
                    event_type = event.get("event_type")
                    occurred_at = parse_datetime(event["timestamp"])
                    if occurred_at.astimezone(timezone).date() != day_plan.day:
                        continue
                    payload = event.get("payload") or {}
                    if event_type == "candidate_economics":
                        equity = float(payload["equity"])
                        equity_points.append((occurred_at, equity))
                        raw_candidates = [
                            item["candidate"] for item in payload.get("evaluated_candidates", [])
                        ]
                    elif event_type == "cooldown_blocked":
                        equity = None
                        raw_candidate = payload.get("candidate")
                        if raw_candidate is None:
                            raw_candidate = (payload.get("evaluated_candidate") or {}).get(
                                "candidate"
                            )
                        raw_candidates = [raw_candidate] if raw_candidate else []
                    else:
                        continue
                    for raw_candidate in raw_candidates:
                        candidate = trade_candidate_from_journal(raw_candidate)
                        key = candidate.candle.closed_at.isoformat()
                        group = groups.setdefault(
                            key,
                            _BatchAccumulator(occurred_at=occurred_at),
                        )
                        group.occurred_at = max(group.occurred_at, occurred_at)
                        if equity is not None:
                            group.equity = equity
                        candidate_key = "|".join(
                            (
                                candidate.candidate_id,
                                candidate.pending_entry_id or "",
                                candidate.symbol,
                                candidate.signal.action,
                            )
                        )
                        group.candidates[candidate_key] = candidate
        finally:
            archive.close()
            reader.close()

    equity_points.sort(key=lambda item: item[0])
    batches = []
    for group in sorted(groups.values(), key=lambda item: item.occurred_at):
        equity = group.equity
        if equity is None:
            equity = _nearest_equity(equity_points, group.occurred_at)
        batches.append(
            ReplayCandidateBatch(
                occurred_at=group.occurred_at,
                account_equity=equity,
                candidates=tuple(group.candidates.values()),
            )
        )
    return batches, manifests


def _nearest_equity(
    points: list[tuple[datetime, float]],
    occurred_at: datetime,
) -> float:
    if not points:
        raise RuntimeError("A cooldown-only candidate has no account equity source.")
    preceding = [item for item in points if item[0] <= occurred_at]
    if preceding:
        return preceding[-1][1]
    return points[0][1]


def _build_settings(
    *,
    batches: list[ReplayCandidateBatch],
    manifests: list[dict[str, Any]],
    timezone_name: str,
) -> Settings:
    symbols: dict[AssetClass, set[str]] = {asset: set() for asset in AssetClass}
    for batch in batches:
        for candidate in batch.candidates:
            asset = (
                candidate.market_context.asset_class
                if candidate.market_context is not None
                else AssetClass(candidate.session_key.split(":", maxsplit=1)[0])
            )
            symbols[asset].add(candidate.symbol)
    raw_settings = (manifests[-1].get("runtime") or {}).get("settings") or {}
    watchlist = sorted(set().union(*symbols.values()))
    return Settings(
        WATCHLIST=",".join(watchlist),
        CRYPTO_SYMBOLS=",".join(sorted(symbols[AssetClass.CRYPTO])),
        EQUITY_US_SYMBOLS=",".join(sorted(symbols[AssetClass.EQUITY_US])),
        EQUITY_EU_SYMBOLS=",".join(sorted(symbols[AssetClass.EQUITY_EU])),
        MAX_OPEN_POSITIONS=int(raw_settings.get("MAX_OPEN_POSITIONS", 1)),
        MAX_OPEN_POSITIONS_PER_SYMBOL=int(raw_settings.get("MAX_OPEN_POSITIONS_PER_SYMBOL", 1)),
        MAX_TRADES_PER_SESSION=int(raw_settings.get("MAX_TRADES_PER_SESSION", 3)),
        TRADING_SESSION_TIMEZONE=str(raw_settings.get("TRADING_SESSION_TIMEZONE", timezone_name)),
        TRADING_SESSIONS_CRYPTO=str(raw_settings.get("TRADING_SESSIONS_CRYPTO", "")),
        TRADING_SESSIONS_EQUITY_US=str(
            raw_settings.get("TRADING_SESSIONS_EQUITY_US", "15:30-22:00")
        ),
        TRADING_SESSIONS_EQUITY_EU=str(
            raw_settings.get("TRADING_SESSIONS_EQUITY_EU", "09:00-17:30")
        ),
    )


def _run_day(
    *,
    day_plan: ReplayDayPlan,
    batches: list[ReplayCandidateBatch],
    scenarios: dict[str, StatefulManagedReplay],
    timezone: ZoneInfo,
) -> None:
    batch_index = 0
    streams = [
        _market_events(source, day_plan.day, timezone, index)
        for index, source in enumerate(day_plan.sources)
    ]
    recent_keys: deque[tuple[Any, ...]] = deque()
    recent_set: set[tuple[Any, ...]] = set()
    processed = 0
    for occurred_at, _, sequence, snapshot in heapq.merge(*streams):
        while batch_index < len(batches) and batches[batch_index].occurred_at <= occurred_at:
            batch = batches[batch_index]
            for replay in scenarios.values():
                replay.on_candidate_batch(batch)
            batch_index += 1
        key = (
            snapshot.symbol,
            snapshot.timestamp,
            snapshot.bid,
            snapshot.ask,
            snapshot.last,
        )
        if key in recent_set:
            continue
        recent_keys.append(key)
        recent_set.add(key)
        if len(recent_keys) > 20_000:
            recent_set.discard(recent_keys.popleft())
        for replay in scenarios.values():
            replay.on_market_snapshot(
                snapshot=snapshot,
                occurred_at=occurred_at,
            )
        processed += 1
        if processed % 250_000 == 0:
            print(
                f"replay-progress day={day_plan.day.isoformat()} "
                f"market_events={processed} source_sequence={sequence}"
            )
    while batch_index < len(batches):
        batch = batches[batch_index]
        for replay in scenarios.values():
            replay.on_candidate_batch(batch)
        batch_index += 1


def _market_events(
    source: ReplaySource,
    target_day: date,
    timezone: ZoneInfo,
    source_index: int,
) -> Iterator[tuple[datetime, int, int, MarketSnapshot]]:
    reader, archive = _open_archive(source)
    try:
        with (
            archive.open(source.market_member) as zipped,
            gzip.GzipFile(fileobj=zipped) as stream,
        ):
            for raw_line in stream:
                if b"market_price_changed" not in raw_line:
                    continue
                event = json.loads(raw_line)
                if event.get("event_type") != "market_price_changed":
                    continue
                occurred_at = parse_datetime(event["timestamp"])
                if occurred_at.astimezone(timezone).date() != target_day:
                    continue
                payload = event.get("payload") or {}
                yield (
                    occurred_at,
                    source_index,
                    int(event.get("sequence", 0)),
                    market_snapshot_from_journal(payload["snapshot"]),
                )
    finally:
        archive.close()
        reader.close()


def _require_members(archive: zipfile.ZipFile, members: tuple[str, ...]) -> None:
    available = set(archive.namelist())
    missing = [member for member in members if member not in available]
    if missing:
        raise ValueError(f"Archive is missing replay members: {missing}")


def _validate_unique_archives(days: list[ReplayDayPlan]) -> None:
    archives: dict[tuple[Path, ...], ReplaySource] = {}
    for day in days:
        for source in day.sources:
            archives.setdefault(source.parts, source)
    for index, source in enumerate(archives.values(), start=1):
        print(f"crc-start archive={index}/{len(archives)}")
        reader, archive = _open_archive(source)
        try:
            for item in archive.infolist():
                if item.is_dir():
                    continue
                with archive.open(item) as member:
                    while member.read(1024 * 1024):
                        pass
        finally:
            archive.close()
            reader.close()
        print(f"crc-ok archive={index}/{len(archives)}")


def _source_provenance(days: list[ReplayDayPlan]) -> dict[str, Any]:
    unique_parts = sorted({path for day in days for source in day.sources for path in source.parts})
    part_hashes = {
        path.name: {
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        for path in unique_parts
    }
    runs: dict[str, dict[str, Any]] = {}
    for day in days:
        for source in day.sources:
            if source.run_id in runs:
                continue
            reader, archive = _open_archive(source)
            try:
                manifest = json.loads(archive.read(source.manifest_member))
                member = archive.getinfo(source.market_member)
            finally:
                archive.close()
                reader.close()
            runs[source.run_id] = {
                "parts": [path.name for path in source.parts],
                "git_commit": (manifest.get("code") or {}).get("git_commit"),
                "started_at": manifest.get("started_at"),
                "ended_at": manifest.get("ended_at"),
                "status": manifest.get("status"),
                "market_member_crc32": f"{member.CRC:08x}",
                "market_member_uncompressed_bytes": member.file_size,
            }
    return {"parts": part_hashes, "runs": runs}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _compare_day(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    baseline = results["corrected_baseline_v1"]
    variant = results["delayed_equity_trigger_v1"]
    return {
        "realized_net_delta": round(
            variant["pnl"]["realized_net"] - baseline["pnl"]["realized_net"],
            4,
        ),
        "realized_plus_mtm_delta": round(
            variant["pnl"]["realized_plus_mark_to_market_net"]
            - baseline["pnl"]["realized_plus_mark_to_market_net"],
            4,
        ),
        "max_drawdown_delta": round(
            variant["pnl"]["max_realized_drawdown"] - baseline["pnl"]["max_realized_drawdown"],
            4,
        ),
        "max_intraday_equity_drawdown_delta": round(
            variant["pnl"]["max_intraday_equity_drawdown"]
            - baseline["pnl"]["max_intraday_equity_drawdown"],
            4,
        ),
        "trade_count_delta": (variant["trades"]["count"] - baseline["trades"]["count"]),
        "matched_exit_changes": _matched_exit_changes(baseline, variant),
    }


def _matched_exit_changes(
    baseline: dict[str, Any],
    variant: dict[str, Any],
) -> list[dict[str, Any]]:
    baseline_by_candidate = {item["candidate_id"]: item for item in baseline["trades"]["details"]}
    variant_by_candidate = {item["candidate_id"]: item for item in variant["trades"]["details"]}
    changes = []
    for candidate_id in sorted(baseline_by_candidate.keys() & variant_by_candidate):
        old = baseline_by_candidate[candidate_id]
        new = variant_by_candidate[candidate_id]
        if old["closed_at"] == new["closed_at"] and old["close_reason"] == new["close_reason"]:
            continue
        old_at = parse_datetime(old["closed_at"])
        new_at = parse_datetime(new["closed_at"])
        changes.append(
            {
                "candidate_id": candidate_id,
                "symbol": old["symbol"],
                "side": old["side"],
                "baseline_reason": old["close_reason"],
                "variant_reason": new["close_reason"],
                "baseline_closed_at": old["closed_at"],
                "variant_closed_at": new["closed_at"],
                "exit_delay_seconds": round(
                    (new_at - old_at).total_seconds(),
                    3,
                ),
                "net_pnl_delta": round(new["net_pnl"] - old["net_pnl"], 4),
            }
        )
    return changes


def _aggregate_scenario(
    day_reports: list[dict[str, Any]],
    scenario: str,
) -> dict[str, Any]:
    results = [day["scenarios"][scenario] for day in day_reports]
    details = [item for result in results for item in result["trades"]["details"]]
    realized = sum(result["pnl"]["realized_net"] for result in results)
    realized_gross = sum(result["pnl"]["realized_gross"] for result in results)
    explicit_costs = sum(result["pnl"]["explicit_costs_deducted"] for result in results)
    mark = sum(result["pnl"]["mark_to_market_net"] for result in results)
    return {
        "realized_gross_total": round(realized_gross, 4),
        "explicit_costs_deducted_total": round(explicit_costs, 4),
        "realized_net_total": round(realized, 4),
        "mark_to_market_net_total": round(mark, 4),
        "realized_plus_mark_to_market_net_total": round(realized + mark, 4),
        "net_by_day": {
            day["date"]: day["scenarios"][scenario]["pnl"]["realized_net"] for day in day_reports
        },
        "net_by_asset_class": _aggregate_trade_dimension(details, "asset_class"),
        "net_by_side": _aggregate_trade_dimension(details, "side"),
        "max_realized_drawdown": _drawdown(details),
        "max_intraday_equity_drawdown": max(
            (
                result["pnl"]["max_intraday_equity_drawdown"]
                for result in results
            ),
            default=0.0,
        ),
        "trade_count": len(details),
        "by_close_reason": dict(Counter(item["close_reason"] for item in details)),
        "average_duration_seconds": _mean(details, "duration_seconds"),
        "average_mfe_percent": _mean(details, "mfe_percent"),
        "average_mae_percent": _mean(details, "mae_percent"),
        "capital_immobilized_usd_minutes": round(
            sum(result["portfolio"]["capital_immobilized_usd_minutes"] for result in results),
            4,
        ),
        "peak_simultaneous_positions": max(
            (result["portfolio"]["peak_simultaneous_positions"] for result in results),
            default=0,
        ),
        "prevented_by_capacity": sum(
            result["constraints"].get("prevented_by_capacity", 0) for result in results
        ),
        "prevented_by_cooldown": sum(
            result["constraints"].get("prevented_by_cooldown", 0) for result in results
        ),
        "pending_trades_opened": sum(
            result["constraints"].get("pending_trades_opened", 0) for result in results
        ),
        "tp_after_breakeven": sum(
            result["protected_exit_counterfactuals"]["tp_after_breakeven"] for result in results
        ),
        "sl_after_breakeven": sum(
            result["protected_exit_counterfactuals"]["sl_after_breakeven"] for result in results
        ),
        "counterfactual_incremental_net_if_held": round(
            sum(
                result["protected_exit_counterfactuals"]["incremental_net_if_held"]
                for result in results
            ),
            4,
        ),
        "largest_trade_net_pnl": (
            round(max((item["net_pnl"] for item in details), default=0.0), 4)
        ),
        "largest_trade_share_of_positive_pnl": _largest_trade_share(details),
        "mean_managed_time_expected_return_contribution": _mean(
            details,
            "managed_time_expected_return_contribution",
        ),
        "managed_time_contribution_by_asset_class": _aggregate_mean_dimension(
            details,
            "asset_class",
            "managed_time_expected_return_contribution",
        ),
        "managed_time_contribution_by_side": _aggregate_mean_dimension(
            details,
            "side",
            "managed_time_expected_return_contribution",
        ),
    }


def _compare_aggregates(
    aggregates: dict[str, dict[str, Any]],
    day_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    baseline = aggregates["corrected_baseline_v1"]
    variant = aggregates["delayed_equity_trigger_v1"]
    complete_days = [day for day in day_reports if not day["incomplete"]]
    baseline_complete = sum(
        day["scenarios"]["corrected_baseline_v1"]["pnl"]["realized_net"] for day in complete_days
    )
    variant_complete = sum(
        day["scenarios"]["delayed_equity_trigger_v1"]["pnl"]["realized_net"]
        for day in complete_days
    )
    baseline_capital = baseline["capital_immobilized_usd_minutes"]
    return {
        "realized_net_delta": round(
            variant["realized_net_total"] - baseline["realized_net_total"],
            4,
        ),
        "realized_plus_mark_to_market_net_delta": round(
            variant["realized_plus_mark_to_market_net_total"]
            - baseline["realized_plus_mark_to_market_net_total"],
            4,
        ),
        "max_drawdown_delta": round(
            variant["max_realized_drawdown"] - baseline["max_realized_drawdown"],
            4,
        ),
        "max_intraday_equity_drawdown_delta": round(
            variant["max_intraday_equity_drawdown"]
            - baseline["max_intraday_equity_drawdown"],
            4,
        ),
        "capital_immobilized_delta": round(
            variant["capital_immobilized_usd_minutes"] - baseline_capital,
            4,
        ),
        "capital_immobilized_percent_delta": round(
            (variant["capital_immobilized_usd_minutes"] - baseline_capital)
            / baseline_capital
            * 100,
            4,
        )
        if baseline_capital > 0
        else None,
        "prevented_by_capacity_delta": (
            variant["prevented_by_capacity"] - baseline["prevented_by_capacity"]
        ),
        "prevented_by_cooldown_delta": (
            variant["prevented_by_cooldown"] - baseline["prevented_by_cooldown"]
        ),
        "positive_day_count_baseline": sum(value > 0 for value in baseline["net_by_day"].values()),
        "positive_day_count_variant": sum(value > 0 for value in variant["net_by_day"].values()),
        "complete_day_count": len(complete_days),
        "complete_days_realized_net_baseline": round(baseline_complete, 4),
        "complete_days_realized_net_variant": round(variant_complete, 4),
        "complete_days_realized_net_delta": round(
            variant_complete - baseline_complete,
            4,
        ),
        "complete_positive_day_count_baseline": sum(
            day["scenarios"]["corrected_baseline_v1"]["pnl"]["realized_net"] > 0
            for day in complete_days
        ),
        "complete_positive_day_count_variant": sum(
            day["scenarios"]["delayed_equity_trigger_v1"]["pnl"]["realized_net"] > 0
            for day in complete_days
        ),
        "daily_deltas": {
            day["date"]: day["comparison"]["realized_net_delta"] for day in day_reports
        },
    }


def _aggregate_trade_dimension(
    details: list[dict[str, Any]],
    key: str,
) -> dict[str, dict[str, Any]]:
    values = sorted({str(item[key]) for item in details})
    return {
        value: {
            "count": len(items),
            "net_pnl": round(sum(item["net_pnl"] for item in items), 4),
        }
        for value in values
        for items in [[item for item in details if str(item[key]) == value]]
    }


def _aggregate_mean_dimension(
    details: list[dict[str, Any]],
    dimension: str,
    metric: str,
) -> dict[str, dict[str, Any]]:
    values = sorted({str(item[dimension]) for item in details})
    return {
        value: {
            "count": len(items),
            "mean": round(statistics.mean(item[metric] for item in items), 4),
            "minimum": round(min(item[metric] for item in items), 4),
            "maximum": round(max(item[metric] for item in items), 4),
        }
        for value in values
        for items in [[item for item in details if str(item[dimension]) == value]]
    }


def _drawdown(details: list[dict[str, Any]]) -> float:
    cumulative = 0.0
    peak = 0.0
    maximum = 0.0
    for item in sorted(details, key=lambda value: value["closed_at"]):
        cumulative += item["net_pnl"]
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return round(maximum, 4)


def _mean(details: list[dict[str, Any]], key: str) -> float | None:
    values = [float(item[key]) for item in details]
    return round(statistics.mean(values), 4) if values else None


def _largest_trade_share(details: list[dict[str, Any]]) -> float | None:
    positive = [float(item["net_pnl"]) for item in details if item["net_pnl"] > 0]
    return round(max(positive) / sum(positive), 4) if positive else None


def _render_markdown(report: dict[str, Any]) -> str:
    baseline = report["aggregates"]["corrected_baseline_v1"]
    variant = report["aggregates"]["delayed_equity_trigger_v1"]
    comparison = report["comparison"]
    rows = [
        "# Replay breakeven MANAGED_EDGE_V1",
        "",
        (
            "Replay stateful des 22, 23, 24, 27, 28 et 31 juillet 2026. "
            "Les montants sont dans la devise du compte des journaux et les prix "
            "de sortie sont exécutables (BUY au bid, SELL au ask)."
        ),
        "",
        "## Résultats agrégés",
        "",
        "| Variante | Brut réalisé | Coûts explicites | Net réalisé | MTM net | Net + MTM | Trades | DD réalisé | DD equity intraday |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        (
            f"| Baseline corrigée 0,55/0,60 | "
            f"{baseline['realized_gross_total']:.4f} | "
            f"{baseline['explicit_costs_deducted_total']:.4f} | "
            f"{baseline['realized_net_total']:.4f} | "
            f"{baseline['mark_to_market_net_total']:.4f} | "
            f"{baseline['realized_plus_mark_to_market_net_total']:.4f} | "
            f"{baseline['trade_count']} | {baseline['max_realized_drawdown']:.4f} | "
            f"{baseline['max_intraday_equity_drawdown']:.4f} |"
        ),
        (
            f"| Variante 0,65/0,70 | {variant['realized_gross_total']:.4f} | "
            f"{variant['explicit_costs_deducted_total']:.4f} | "
            f"{variant['realized_net_total']:.4f} | "
            f"{variant['mark_to_market_net_total']:.4f} | "
            f"{variant['realized_plus_mark_to_market_net_total']:.4f} | "
            f"{variant['trade_count']} | {variant['max_realized_drawdown']:.4f} | "
            f"{variant['max_intraday_equity_drawdown']:.4f} |"
        ),
        "",
        (
            f"Delta réalisé variante : **{comparison['realized_net_delta']:+.4f}**. "
            f"Sur les {comparison['complete_day_count']} journées complètes, "
            f"le delta n’est que **{comparison['complete_days_realized_net_delta']:+.4f}** "
            f"({comparison['complete_days_realized_net_baseline']:.4f} contre "
            f"{comparison['complete_days_realized_net_variant']:.4f})."
        ),
        "",
        "## Par journée",
        "",
        "| Date | Complétude | Candidates | Baseline réalisée | Variante réalisée | Delta | Baseline MTM | Variante MTM |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for day in report["days"]:
        old = day["scenarios"]["corrected_baseline_v1"]["pnl"]
        new = day["scenarios"]["delayed_equity_trigger_v1"]["pnl"]
        rows.append(
            f"| {day['date']} | {'incomplète' if day['incomplete'] else 'complète'} | "
            f"{day['candidate_universe']} | "
            f"{old['realized_net']:.4f} | {new['realized_net']:.4f} | "
            f"{day['comparison']['realized_net_delta']:.4f} | "
            f"{old['mark_to_market_net']:.4f} | {new['mark_to_market_net']:.4f} |"
        )
    rows.extend(["", "## Segments", ""])
    rows.extend(_render_segment_table(baseline, variant))
    rows.extend(["", "## Lifecycle et contraintes", ""])
    rows.extend(_render_lifecycle_table(baseline, variant))
    rows.extend(["", "## Sorties modifiées par le seuil", ""])
    rows.extend(_render_exit_changes(report["days"]))
    rows.extend(["", "## Journée incomplète du 28 juillet", ""])
    rows.extend(_render_incomplete_positions(report["days"]))
    rows.extend(
        [
            "",
            "## Méthodologie et limites",
            "",
            (
                f"- Validation CRC des archives du plan : "
                f"`{report['methodology']['archive_crc_validation']}`."
            ),
            (
                f"- Sélection : `{report['methodology']['selection_policy']}` ; "
                "top-N puis contraintes portefeuille, sans repêchage."
            ),
            (
                "- Les fills de clôture broker historiques ne sont pas disponibles : "
                "les entrées et sorties du replay sont des estimations exécutables "
                "bid/ask."
            ),
            (
                "- Le spread est inclus dans les côtés exécutables et n’est pas "
                "retranché une seconde fois ; seuls les coûts explicites estimés "
                "sont déduits."
            ),
            (
                "- Le DD réalisé suit les clôtures confirmées. Le DD equity "
                "intraday marque aussi chaque position ouverte au dernier côté "
                "exécutable et retient le pire drawdown d’une séance."
            ),
            (
                "- Le 28 juillet s’arrête au dernier tick enregistré. Ses positions "
                "restantes sont conservées et valorisées, sans fin de séance inventée."
            ),
            (
                "- Le JSON associé contient chaque trade, MFE/MAE, provenance, "
                "position ouverte, contribution temporelle et contrefactuel."
            ),
            "",
        ]
    )
    return "\n".join(rows)


def _render_segment_table(
    baseline: dict[str, Any],
    variant: dict[str, Any],
) -> list[str]:
    rows = [
        "| Segment | Baseline trades | Baseline net | Variante trades | Variante net |",
        "|---|---:|---:|---:|---:|",
    ]
    labels = (
        ("EU", "net_by_asset_class", "EQUITY_EU"),
        ("US", "net_by_asset_class", "EQUITY_US"),
        ("BUY", "net_by_side", "BUY"),
        ("SELL", "net_by_side", "SELL"),
    )
    for label, dimension, key in labels:
        old = baseline[dimension].get(key, {"count": 0, "net_pnl": 0.0})
        new = variant[dimension].get(key, {"count": 0, "net_pnl": 0.0})
        rows.append(
            f"| {label} | {old['count']} | {old['net_pnl']:.4f} | "
            f"{new['count']} | {new['net_pnl']:.4f} |"
        )
    return rows


def _render_lifecycle_table(
    baseline: dict[str, Any],
    variant: dict[str, Any],
) -> list[str]:
    rows = [
        "| Mesure | Baseline | Variante |",
        "|---|---:|---:|",
    ]
    reasons = sorted(set(baseline["by_close_reason"]) | set(variant["by_close_reason"]))
    for reason in reasons:
        rows.append(
            f"| Sorties `{reason}` | {baseline['by_close_reason'].get(reason, 0)} | "
            f"{variant['by_close_reason'].get(reason, 0)} |"
        )
    measures = (
        ("Durée moyenne (min)", "average_duration_seconds", 1 / 60),
        ("MFE moyen (%)", "average_mfe_percent", 1),
        ("MAE moyen (%)", "average_mae_percent", 1),
        ("Capital-minutes", "capital_immobilized_usd_minutes", 1),
        ("Pic positions simultanées", "peak_simultaneous_positions", 1),
        ("Empêchés par capacité", "prevented_by_capacity", 1),
        ("Empêchés par cooldown", "prevented_by_cooldown", 1),
        ("Pending entries ouvertes", "pending_trades_opened", 1),
        ("TP après sortie breakeven", "tp_after_breakeven", 1),
        ("SL après sortie breakeven", "sl_after_breakeven", 1),
        (
            "Delta net contrefactuel si protections conservées",
            "counterfactual_incremental_net_if_held",
            1,
        ),
        ("Plus gros gain", "largest_trade_net_pnl", 1),
        (
            "Part du plus gros gain dans les gains positifs",
            "largest_trade_share_of_positive_pnl",
            100,
        ),
    )
    for label, key, multiplier in measures:
        old = baseline[key]
        new = variant[key]
        old_value = float(old or 0.0) * multiplier
        new_value = float(new or 0.0) * multiplier
        suffix = "%" if key == "largest_trade_share_of_positive_pnl" else ""
        rows.append(f"| {label} | {old_value:.4f}{suffix} | {new_value:.4f}{suffix} |")
    return rows


def _render_exit_changes(days: list[dict[str, Any]]) -> list[str]:
    changes = [
        (day["date"], change)
        for day in days
        for change in day["comparison"]["matched_exit_changes"]
    ]
    if not changes:
        return ["Aucune sortie commune n’a changé."]
    rows = [
        "| Date | Position | Baseline | Variante | Délai (s) | Delta net |",
        "|---|---|---|---|---:|---:|",
    ]
    for day, change in changes:
        rows.append(
            f"| {day} | {change['symbol']} {change['side']} | "
            f"{change['baseline_reason']} | {change['variant_reason']} | "
            f"{change['exit_delay_seconds']:.3f} | "
            f"{change['net_pnl_delta']:+.4f} |"
        )
    return rows


def _render_incomplete_positions(days: list[dict[str, Any]]) -> list[str]:
    rows = [
        "| Variante | Réalisé | MTM | Positions ouvertes | Détail MTM |",
        "|---|---:|---:|---:|---|",
    ]
    names = (
        ("corrected_baseline_v1", "Baseline"),
        ("delayed_equity_trigger_v1", "Variante"),
    )
    for day in (item for item in days if item["incomplete"]):
        for scenario, label in names:
            result = day["scenarios"][scenario]
            positions = result["mark_to_market"]["positions"]
            detail = (
                ", ".join(
                    f"{item['symbol']} {item['side']}: {item['net_pnl']:+.4f}" for item in positions
                )
                or "aucune"
            )
            rows.append(
                f"| {label} | {result['pnl']['realized_net']:.4f} | "
                f"{result['pnl']['mark_to_market_net']:.4f} | "
                f"{len(positions)} | {detail} |"
            )
    return rows


if __name__ == "__main__":
    main()
