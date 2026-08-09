from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from typing import Any

from app.execution.strategy_segment import StrategySegment
from app.execution.trade_candidate import TradeCandidate
from app.instruments.models import AssetClass, EntryDecisionConfig
from app.market.market_context import (
    BenchmarkContext,
    BreadthContext,
    CandidateMarketContext,
    ContextAlignment,
    MarketDirection,
    MarketRegime,
    SectorContext,
)
from app.market.models import (
    Candle,
    MarketSnapshot,
    PriceSource,
    TimestampSource,
)
from app.market.multi_timeframe import (
    MultiTimeframeContext,
    OpeningRangeFeatures,
    OpeningRangeWindow,
    TimeframeFeatures,
)
from app.market.relative_spread import SpreadContext
from app.market.timeframes import (
    MultiTimeframeAlignment,
    OpeningRangeStatus,
    SamplingQuality,
    TimeframeDirection,
    TimeframeMaturity,
)
from app.strategies.signals import Signal


def trade_candidate_from_journal(value: dict[str, Any]) -> TradeCandidate:
    candidate_fields = {item.name for item in fields(TradeCandidate)}
    scalar_values = {
        key: item
        for key, item in value.items()
        if key in candidate_fields
        and key
        not in {
            "snapshot",
            "candle",
            "signal",
            "market_context",
            "multi_timeframe_context",
            "entry_decision_config",
            "segment",
        }
    }
    return TradeCandidate(
        **scalar_values,
        snapshot=market_snapshot_from_journal(value["snapshot"]),
        candle=candle_from_journal(value["candle"]),
        signal=signal_from_journal(value["signal"]),
        market_context=candidate_market_context_from_journal(value.get("market_context")),
        multi_timeframe_context=multi_timeframe_context_from_journal(
            value.get("multi_timeframe_context")
        ),
        entry_decision_config=(
            EntryDecisionConfig(**value["entry_decision_config"])
            if value.get("entry_decision_config")
            else None
        ),
        segment=(
            StrategySegment(value["segment"])
            if value.get("segment") is not None
            else None
        ),
    )


def market_snapshot_from_journal(value: dict[str, Any]) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=str(value["symbol"]),
        bid=float(value["bid"]),
        ask=float(value["ask"]),
        last=float(value["last"]),
        timestamp=parse_datetime(value["timestamp"]),
        received_at=(parse_datetime(value["received_at"]) if value.get("received_at") else None),
        price_source=PriceSource(value.get("price_source", PriceSource.BROKER_LAST)),
        timestamp_source=TimestampSource(value.get("timestamp_source", TimestampSource.BROKER)),
    )


def candle_from_journal(value: dict[str, Any]) -> Candle:
    return Candle(
        symbol=str(value["symbol"]),
        timeframe_seconds=int(value["timeframe_seconds"]),
        open=float(value["open"]),
        high=float(value["high"]),
        low=float(value["low"]),
        close=float(value["close"]),
        volume=(None if value.get("volume") is None else float(value["volume"])),
        opened_at=parse_datetime(value["opened_at"]),
        closed_at=parse_datetime(value["closed_at"]),
        sample_count=int(value.get("sample_count", 0)),
        carried_forward=bool(value.get("carried_forward", False)),
        source_price_age_seconds=(
            None
            if value.get("source_price_age_seconds") is None
            else float(value["source_price_age_seconds"])
        ),
        quality_degraded=bool(value.get("quality_degraded", False)),
    )


def signal_from_journal(value: dict[str, Any]) -> Signal:
    return Signal(
        action=str(value["action"]),
        setup_quality=float(value["setup_quality"]),
        reason=str(value["reason"]),
        metadata=dict(value.get("metadata") or {}),
    )


def candidate_market_context_from_journal(
    value: dict[str, Any] | None,
) -> CandidateMarketContext | None:
    if not value:
        return None
    benchmark = value["benchmark"]
    breadth = value["breadth"]
    sector = value["sector"]
    return CandidateMarketContext(
        version=str(value["version"]),
        as_of=parse_datetime(value["as_of"]),
        asset_class=AssetClass(value["asset_class"]),
        regime=MarketRegime(value["regime"]),
        alignment=ContextAlignment(value["alignment"]),
        benchmark=BenchmarkContext(
            symbol=benchmark.get("symbol"),
            available=bool(benchmark["available"]),
            direction=MarketDirection(benchmark["direction"]),
            session_return_percent=_optional_float(benchmark.get("session_return_percent")),
            momentum_percent=_optional_float(benchmark.get("momentum_percent")),
            spread_percent=_optional_float(benchmark.get("spread_percent")),
            snapshot_age_seconds=_optional_float(benchmark.get("snapshot_age_seconds")),
        ),
        breadth=BreadthContext(
            available=bool(breadth["available"]),
            direction=MarketDirection(breadth["direction"]),
            eligible_symbols=int(breadth["eligible_symbols"]),
            valid_symbols=int(breadth["valid_symbols"]),
            coverage_ratio=float(breadth["coverage_ratio"]),
            advancing_count=int(breadth["advancing_count"]),
            declining_count=int(breadth["declining_count"]),
            unchanged_count=int(breadth["unchanged_count"]),
            advancing_ratio=float(breadth["advancing_ratio"]),
            median_session_return_percent=_optional_float(
                breadth.get("median_session_return_percent")
            ),
        ),
        sector=SectorContext(
            sector=sector.get("sector"),
            available=bool(sector["available"]),
            direction=MarketDirection(sector["direction"]),
            member_count=int(sector["member_count"]),
            valid_member_count=int(sector["valid_member_count"]),
            advancing_ratio=_optional_float(sector.get("advancing_ratio")),
            median_session_return_percent=_optional_float(
                sector.get("median_session_return_percent")
            ),
            benchmark_symbol=sector.get("benchmark_symbol"),
            benchmark_return_percent=_optional_float(sector.get("benchmark_return_percent")),
        ),
        symbol_session_return_percent=_optional_float(value.get("symbol_session_return_percent")),
        symbol_relative_strength_percent=_optional_float(
            value.get("symbol_relative_strength_percent")
        ),
        reasons=tuple(str(item) for item in value.get("reasons", [])),
        spread=_spread_context_from_journal(value.get("spread")),
    )


def _spread_context_from_journal(
    value: dict[str, Any] | None,
) -> SpreadContext | None:
    if value is None:
        return None
    return SpreadContext(
        version=str(value["version"]),
        available=bool(value["available"]),
        current_percent=_optional_float(value.get("current_percent")),
        reference_median_percent=_optional_float(
            value.get("reference_median_percent")
        ),
        relative_to_median=_optional_float(value.get("relative_to_median")),
        reference_percentile=_optional_float(
            value.get("reference_percentile")
        ),
        recent_change_ratio=_optional_float(value.get("recent_change_ratio")),
        reference_observations=int(value.get("reference_observations", 0)),
    )


def multi_timeframe_context_from_journal(
    value: dict[str, Any] | None,
) -> MultiTimeframeContext | None:
    if not value:
        return None
    features = {
        key: _timeframe_features_from_journal(item)
        for key, item in (value.get("features_by_timeframe") or {}).items()
    }
    opening = value.get("opening_ranges") or {}
    windows = {
        str(key): _opening_range_from_journal(item)
        for key, item in (opening.get("windows") or {}).items()
    }
    return MultiTimeframeContext(
        model_version=str(value["model_version"]),
        as_of=parse_datetime(value["as_of"]),
        side=str(value["side"]),
        features_by_timeframe=features,
        maturity_by_timeframe={
            str(key): TimeframeMaturity(item)
            for key, item in (value.get("maturity_by_timeframe") or {}).items()
        },
        opening_ranges=OpeningRangeFeatures(
            session_key=opening.get("session_key"),
            windows=windows,
        ),
        ready_aligned_timeframes=tuple(value.get("ready_aligned_timeframes", [])),
        ready_opposed_timeframes=tuple(value.get("ready_opposed_timeframes", [])),
        inclusive_aligned_timeframes=tuple(value.get("inclusive_aligned_timeframes", [])),
        inclusive_opposed_timeframes=tuple(value.get("inclusive_opposed_timeframes", [])),
        unavailable_timeframes=tuple(value.get("unavailable_timeframes", [])),
        ready_alignment=MultiTimeframeAlignment(value["ready_alignment"]),
        alignment_including_provisional=MultiTimeframeAlignment(
            value["alignment_including_provisional"]
        ),
    )


def parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timeframe_features_from_journal(value: dict[str, Any]) -> TimeframeFeatures:
    valid_fields = {item.name for item in fields(TimeframeFeatures)}
    values = {key: item for key, item in value.items() if key in valid_fields}
    values.update(
        maturity=TimeframeMaturity(value["maturity"]),
        as_of=parse_datetime(value["as_of"]),
        latest_bar_closed_at=parse_datetime(value["latest_bar_closed_at"]),
        direction=TimeframeDirection(value["direction"]),
        sampling_quality=SamplingQuality(value["sampling_quality"]),
    )
    return TimeframeFeatures(**values)


def _opening_range_from_journal(value: dict[str, Any]) -> OpeningRangeWindow:
    valid_fields = {item.name for item in fields(OpeningRangeWindow)}
    values = {key: item for key, item in value.items() if key in valid_fields}
    values["status"] = OpeningRangeStatus(value["status"])
    return OpeningRangeWindow(**values)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
