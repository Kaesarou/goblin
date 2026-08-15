from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from statistics import pstdev

from app.market.multi_timeframe import TimeframeBar

RESEARCH_STATE_SCHEMA_VERSION = 1
RESEARCH_STATE_CONTRACT_VERSION = 'side_neutral_market_research_state_v1'
RESEARCH_SAMPLING_CADENCE_MINUTES = 5
RESEARCH_CAUSAL_CUTOFF_CONVENTION = (
    'quotes require market_timestamp < state_at and received_at < state_at; '
    'candles require closed_at <= state_at and contain only earlier samples'
)

_RETURN_WINDOWS = (1, 3, 5, 15, 30, 60)
_DISTRIBUTION_WINDOWS = (5, 15, 30, 60)
_RANGE_WINDOWS = (15, 30, 60)
_OPENING_RANGE_WINDOWS = (15, 30)


def _candle_feature_names() -> tuple[str, ...]:
    names: list[str] = [
        'candle_coverage_60m_ratio',
        'candle_degraded_count_60m',
        'candle_carried_forward_count_60m',
    ]
    names.extend(f'return_{minutes}m_percent' for minutes in _RETURN_WINDOWS)
    for minutes in _DISTRIBUTION_WINDOWS:
        names.extend(
            (
                f'realized_volatility_{minutes}m_percent',
                f'atr_{minutes}m_percent',
            )
        )
    for minutes in _RANGE_WINDOWS:
        names.extend(
            (
                f'range_position_{minutes}m_percent',
                f'distance_to_high_{minutes}m_percent',
                f'distance_to_low_{minutes}m_percent',
            )
        )
    names.extend(
        (
            'price_path_efficiency_5m',
            'price_path_efficiency_15m',
            'range_compression_5m_vs_30m',
            'session_return_percent',
        )
    )
    for minutes in _OPENING_RANGE_WINDOWS:
        names.extend(
            (
                f'opening_range_{minutes}m_range_percent',
                f'opening_range_{minutes}m_position_percent',
                f'opening_range_{minutes}m_breakout_above_percent',
                f'opening_range_{minutes}m_breakdown_below_percent',
            )
        )
    return tuple(names)


CANDLE_RESEARCH_FEATURE_NAMES = _candle_feature_names()

MARKET_CONTEXT_RESEARCH_FEATURE_NAMES = (
    'market_regime',
    'context_latest_symbol_timestamp',
    'symbol_session_return_percent',
    'symbol_relative_strength_percent',
    'benchmark_symbol',
    'benchmark_available',
    'benchmark_session_return_percent',
    'benchmark_momentum_percent',
    'benchmark_spread_percent',
    'benchmark_snapshot_age_seconds',
    'breadth_available',
    'breadth_valid_symbols',
    'breadth_coverage_ratio',
    'breadth_advancing_ratio',
    'breadth_median_session_return_percent',
    'sector',
    'sector_available',
    'sector_valid_member_count',
    'sector_advancing_ratio',
    'sector_median_session_return_percent',
    'relative_spread_available',
    'relative_spread_ratio',
    'relative_spread_percentile',
    'relative_spread_recent_change',
)

RESEARCH_BASE_FEATURE_NAMES = (
    *CANDLE_RESEARCH_FEATURE_NAMES,
    *MARKET_CONTEXT_RESEARCH_FEATURE_NAMES,
)
RESEARCH_BASE_FEATURE_SET_SHA256 = hashlib.sha256(
    json.dumps(
        RESEARCH_BASE_FEATURE_NAMES,
        separators=(',', ':'),
    ).encode('utf-8')
).hexdigest()


def build_candle_research_features(
    *,
    bars: list[TimeframeBar],
    state_at: datetime,
    session_key: str,
    session_start_time: datetime | None,
) -> dict[str, int | float | None]:
    cutoff = _as_utc(state_at)
    relevant = sorted(
        (
            bar
            for bar in bars
            if bar.session_key == session_key
            and _as_utc(bar.candle.closed_at) <= cutoff
            and bar.is_complete
        ),
        key=lambda bar: _as_utc(bar.candle.closed_at),
    )
    result: dict[str, int | float | None] = {
        name: None for name in CANDLE_RESEARCH_FEATURE_NAMES
    }
    if not relevant:
        result['candle_coverage_60m_ratio'] = 0.0
        result['candle_degraded_count_60m'] = 0
        result['candle_carried_forward_count_60m'] = 0
        return result

    candles_by_close = {
        _as_utc(bar.candle.closed_at): bar.candle for bar in relevant
    }
    latest = relevant[-1].candle
    latest_closed_at = _as_utc(latest.closed_at)
    if latest_closed_at != cutoff:
        result['candle_coverage_60m_ratio'] = 0.0
        result['candle_degraded_count_60m'] = 0
        result['candle_carried_forward_count_60m'] = 0
        return result

    last_sixty = _exact_window_candles(
        candles_by_close,
        cutoff=cutoff,
        minutes=60,
        include_reference=False,
    )
    result['candle_coverage_60m_ratio'] = _round(
        len(last_sixty) / 60
    )
    result['candle_degraded_count_60m'] = sum(
        candle.quality_degraded for candle in last_sixty
    )
    result['candle_carried_forward_count_60m'] = sum(
        candle.carried_forward for candle in last_sixty
    )

    for minutes in _RETURN_WINDOWS:
        reference = candles_by_close.get(
            cutoff - timedelta(minutes=minutes)
        )
        result[f'return_{minutes}m_percent'] = (
            None
            if reference is None or reference.close <= 0
            else _round(((latest.close / reference.close) - 1) * 100)
        )

    for minutes in _DISTRIBUTION_WINDOWS:
        window = _exact_window_candles(
            candles_by_close,
            cutoff=cutoff,
            minutes=minutes,
            include_reference=True,
        )
        if len(window) != minutes + 1:
            continue
        returns = [
            ((current.close / previous.close) - 1) * 100
            for previous, current in pairwise(window)
            if previous.close > 0
        ]
        if len(returns) >= 2:
            result[f'realized_volatility_{minutes}m_percent'] = _round(
                pstdev(returns)
            )
        true_ranges = _true_ranges(window)
        if true_ranges and latest.close > 0:
            result[f'atr_{minutes}m_percent'] = _round(
                (sum(true_ranges) / len(true_ranges) / latest.close) * 100
            )

    for minutes in _RANGE_WINDOWS:
        window = _exact_window_candles(
            candles_by_close,
            cutoff=cutoff,
            minutes=minutes,
            include_reference=False,
        )
        if len(window) != minutes:
            continue
        high = max(candle.high for candle in window)
        low = min(candle.low for candle in window)
        if high <= low or high <= 0 or low <= 0:
            continue
        result[f'range_position_{minutes}m_percent'] = _round(
            ((latest.close - low) / (high - low)) * 100
        )
        result[f'distance_to_high_{minutes}m_percent'] = _round(
            ((high - latest.close) / high) * 100
        )
        result[f'distance_to_low_{minutes}m_percent'] = _round(
            ((latest.close - low) / low) * 100
        )

    for minutes in (5, 15):
        window = _exact_window_candles(
            candles_by_close,
            cutoff=cutoff,
            minutes=minutes,
            include_reference=True,
        )
        result[f'price_path_efficiency_{minutes}m'] = (
            _path_efficiency(window)
            if len(window) == minutes + 1
            else None
        )

    five = _exact_window_candles(
        candles_by_close,
        cutoff=cutoff,
        minutes=5,
        include_reference=True,
    )
    thirty = _exact_window_candles(
        candles_by_close,
        cutoff=cutoff,
        minutes=30,
        include_reference=True,
    )
    five_ranges = _true_ranges(five)
    thirty_ranges = _true_ranges(thirty)
    if five_ranges and thirty_ranges:
        short_average = sum(five_ranges) / len(five_ranges)
        long_average = sum(thirty_ranges) / len(thirty_ranges)
        if long_average > 0:
            result['range_compression_5m_vs_30m'] = _round(
                short_average / long_average
            )

    session_candles = [bar.candle for bar in relevant]
    first_session_candle = session_candles[0]
    session_open_is_observed = (
        session_start_time is None
        or _as_utc(first_session_candle.opened_at)
        == _as_utc(session_start_time)
    )
    if session_open_is_observed and first_session_candle.open > 0:
        result['session_return_percent'] = _round(
            ((latest.close / first_session_candle.open) - 1) * 100
        )
    if session_start_time is not None:
        for minutes in _OPENING_RANGE_WINDOWS:
            result.update(
                _opening_range_features(
                    session_candles,
                    latest_close=latest.close,
                    state_at=cutoff,
                    session_start=_as_utc(session_start_time),
                    minutes=minutes,
                )
            )
    return result


def research_state_contract_metadata() -> dict[str, object]:
    return {
        'schema_version': RESEARCH_STATE_SCHEMA_VERSION,
        'version': RESEARCH_STATE_CONTRACT_VERSION,
        'cadence_minutes': RESEARCH_SAMPLING_CADENCE_MINUTES,
        'side_neutral': True,
        'candidate_required': False,
        'quote_or_candle_required': False,
        'collection_start_convention': 'state_at > runtime_started_at',
        'causal_cutoff_convention': RESEARCH_CAUSAL_CUTOFF_CONVENTION,
        'base_feature_names': list(RESEARCH_BASE_FEATURE_NAMES),
        'base_feature_set_sha256': RESEARCH_BASE_FEATURE_SET_SHA256,
    }


def _exact_window_candles(
    candles_by_close,
    *,
    cutoff: datetime,
    minutes: int,
    include_reference: bool,
):
    start = 0 if include_reference else 1
    offsets = range(minutes, start - 1, -1)
    return [
        candle
        for offset in offsets
        for candle in [
            candles_by_close.get(cutoff - timedelta(minutes=offset))
        ]
        if candle is not None
    ]


def _true_ranges(candles) -> list[float]:
    return [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in pairwise(candles)
    ]


def _path_efficiency(candles) -> float | None:
    if len(candles) < 2:
        return None
    path = sum(
        abs(current.close - previous.close)
        for previous, current in pairwise(candles)
    )
    if path == 0:
        return 0.0
    return _round(abs(candles[-1].close - candles[0].close) / path)


def _opening_range_features(
    candles,
    *,
    latest_close: float,
    state_at: datetime,
    session_start: datetime,
    minutes: int,
) -> dict[str, float | None]:
    prefix = f'opening_range_{minutes}m_'
    empty = {
        prefix + 'range_percent': None,
        prefix + 'position_percent': None,
        prefix + 'breakout_above_percent': None,
        prefix + 'breakdown_below_percent': None,
    }
    range_end = session_start + timedelta(minutes=minutes)
    if state_at < range_end:
        return empty
    opening = [
        candle
        for candle in candles
        if _as_utc(candle.opened_at) >= session_start
        and _as_utc(candle.closed_at) <= range_end
    ]
    if len(opening) != minutes:
        return empty
    high = max(candle.high for candle in opening)
    low = min(candle.low for candle in opening)
    reference = opening[0].open
    if high <= low or reference <= 0 or high <= 0 or low <= 0:
        return empty
    return {
        prefix + 'range_percent': _round(((high - low) / reference) * 100),
        prefix + 'position_percent': _round(
            ((latest_close - low) / (high - low)) * 100
        ),
        prefix + 'breakout_above_percent': _round(
            max(0.0, ((latest_close - high) / high) * 100)
        ),
        prefix + 'breakdown_below_percent': _round(
            max(0.0, ((low - latest_close) / low) * 100)
        ),
    }


def _round(value: float, *, digits: int = 8) -> float | None:
    if not math.isfinite(value):
        return None
    return round(float(value), digits)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
