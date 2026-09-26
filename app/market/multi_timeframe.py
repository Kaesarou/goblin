from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from statistics import median
from typing import Any

from app.market.models import Candle
from app.market.timeframes import (
    AGGREGATED_TIMEFRAMES,
    BASE_TIMEFRAME,
    MULTI_TIMEFRAME_MODEL_VERSION,
    SUPPORTED_TIMEFRAMES,
    BarCompleteness,
    MultiTimeframeAlignment,
    OpeningRangeStatus,
    SamplingQuality,
    Timeframe,
    TimeframeDirection,
    TimeframeMaturity,
)


@dataclass(frozen=True)
class TimeframeFeatureConfig:
    provisional_bars: int
    ready_bars: int
    range_lookback_bars: int
    ema_fast_bars: int
    ema_slow_bars: int
    atr_lookback_bars: int
    compression_lookback_bars: int
    acceleration_window_bars: int = 2

    def __post_init__(self) -> None:
        if self.provisional_bars < 2:
            raise ValueError('provisional_bars must be at least 2.')
        if self.ready_bars < self.provisional_bars:
            raise ValueError('ready_bars must be >= provisional_bars.')
        required = max(
            self.range_lookback_bars,
            self.ema_slow_bars,
            self.atr_lookback_bars + 1,
            self.compression_lookback_bars + 1,
            self.acceleration_window_bars * 2 + 1,
        )
        if self.ready_bars < required:
            raise ValueError(
                f'ready_bars={self.ready_bars} is below the required feature window {required}.'
            )


def _default_feature_configs() -> dict[str, TimeframeFeatureConfig]:
    return {
        'm1': TimeframeFeatureConfig(10, 20, 20, 3, 8, 14, 10),
        'm5': TimeframeFeatureConfig(8, 15, 15, 3, 8, 10, 8),
        'm15': TimeframeFeatureConfig(6, 12, 12, 3, 6, 8, 6),
        'm30': TimeframeFeatureConfig(5, 10, 10, 3, 6, 8, 6),
        'h1': TimeframeFeatureConfig(5, 8, 8, 2, 5, 5, 5),
    }


@dataclass(frozen=True)
class MultiTimeframeConfig:
    feature_configs: dict[str, TimeframeFeatureConfig] = field(
        default_factory=_default_feature_configs
    )
    opening_range_minutes: tuple[int, ...] = (15, 30)

    def feature_config_for(self, timeframe: Timeframe) -> TimeframeFeatureConfig:
        key = timeframe.name.lower()
        try:
            return self.feature_configs[key]
        except KeyError as exc:
            raise ValueError(f'Missing multi-timeframe feature config for {key}.') from exc


@dataclass(frozen=True)
class TimeframeBar:
    candle: Candle
    timeframe: Timeframe
    session_key: str
    completeness: BarCompleteness
    source_bar_count: int
    expected_source_bar_count: int
    missing_source_bar_count: int

    @property
    def is_complete(self) -> bool:
        return self.completeness == BarCompleteness.COMPLETE


@dataclass(frozen=True)
class CandleGap:
    symbol: str
    previous_closed_at: datetime
    next_opened_at: datetime
    missing_base_candles: int


@dataclass(frozen=True)
class MultiTimeframeUpdate:
    closed_bars: tuple[TimeframeBar, ...] = ()
    gaps: tuple[CandleGap, ...] = ()


@dataclass(frozen=True)
class TimeframeFeatures:
    timeframe: str
    maturity: TimeframeMaturity
    as_of: datetime
    latest_bar_closed_at: datetime
    bar_count: int
    covered_seconds: int
    direction: TimeframeDirection
    sampling_quality: SamplingQuality
    close: float
    ema_fast: float
    ema_slow: float
    return_1_bar_percent: float
    return_sample_percent: float
    velocity_percent_per_bar: float
    close_vs_fast_ema_percent: float | None = None
    fast_vs_slow_ema_percent: float | None = None
    atr_percent: float | None = None
    return_3_bars_percent: float | None = None
    rolling_high: float | None = None
    rolling_low: float | None = None
    rolling_range_percent: float | None = None
    range_position_percent: float | None = None
    distance_to_range_high_percent: float | None = None
    distance_to_range_low_percent: float | None = None
    previous_bar_high: float | None = None
    previous_bar_low: float | None = None
    body_percent_of_range: float | None = None
    upper_wick_percent_of_range: float | None = None
    lower_wick_percent_of_range: float | None = None
    close_position_percent: float | None = None
    compression_ratio: float | None = None
    previous_velocity_percent_per_bar: float | None = None
    acceleration_percent_per_bar: float | None = None
    pullback_from_recent_high_percent: float | None = None
    rebound_from_recent_low_percent: float | None = None


@dataclass(frozen=True)
class OpeningRangeWindow:
    minutes: int
    status: OpeningRangeStatus
    high: float | None = None
    low: float | None = None
    range_percent: float | None = None
    position_percent: float | None = None
    distance_to_high_percent: float | None = None
    distance_to_low_percent: float | None = None
    breakout_above_percent: float | None = None
    breakdown_below_percent: float | None = None
    source_bar_count: int = 0
    expected_source_bar_count: int = 0


@dataclass(frozen=True)
class OpeningRangeFeatures:
    session_key: str | None
    windows: dict[str, OpeningRangeWindow]


@dataclass(frozen=True)
class MultiTimeframeContext:
    model_version: str
    as_of: datetime
    side: str
    features_by_timeframe: dict[str, TimeframeFeatures]
    maturity_by_timeframe: dict[str, TimeframeMaturity]
    opening_ranges: OpeningRangeFeatures
    ready_aligned_timeframes: tuple[str, ...]
    ready_opposed_timeframes: tuple[str, ...]
    inclusive_aligned_timeframes: tuple[str, ...]
    inclusive_opposed_timeframes: tuple[str, ...]
    unavailable_timeframes: tuple[str, ...]
    ready_alignment: MultiTimeframeAlignment
    alignment_including_provisional: MultiTimeframeAlignment


@dataclass
class _AggregateBucket:
    timeframe: Timeframe
    session_key: str
    bucket_start: datetime
    bucket_end: datetime
    base_candles: list[Candle] = field(default_factory=list)


class TimeframeSeriesStore:
    _DEFAULT_LIMITS = {
        Timeframe.M1: 240,
        Timeframe.M5: 192,
        Timeframe.M15: 128,
        Timeframe.M30: 96,
        Timeframe.H1: 64,
    }

    def __init__(self) -> None:
        self._bars: dict[Timeframe, deque[TimeframeBar]] = {
            timeframe: deque(maxlen=self._DEFAULT_LIMITS[timeframe])
            for timeframe in SUPPORTED_TIMEFRAMES
        }

    def append(self, bar: TimeframeBar) -> None:
        self._bars[bar.timeframe].append(bar)

    def bars(
        self,
        timeframe: Timeframe,
        *,
        as_of: datetime | None = None,
        complete_only: bool = False,
        session_key: str | None = None,
    ) -> list[TimeframeBar]:
        result = list(self._bars[timeframe])
        if as_of is not None:
            actual_as_of = _as_utc(as_of)
            result = [
                bar for bar in result
                if _as_utc(bar.candle.closed_at) <= actual_as_of
            ]
        if complete_only:
            result = [bar for bar in result if bar.is_complete]
        if session_key is not None:
            result = [bar for bar in result if bar.session_key == session_key]
        return result

    def clear(self) -> None:
        for bars in self._bars.values():
            bars.clear()


class MultiTimeframeCandleEngine:
    def __init__(self, symbol: str):
        self.symbol = symbol.strip().upper()
        self._buckets: dict[Timeframe, _AggregateBucket] = {}
        self._last_base_closed_at: datetime | None = None

    def on_base_candle(
        self,
        candle: Candle,
        *,
        session_key: str,
        session_start_time: datetime | None,
        session_24_7: bool,
    ) -> MultiTimeframeUpdate:
        if candle.timeframe_seconds != BASE_TIMEFRAME.value:
            raise ValueError(
                f'Expected {BASE_TIMEFRAME.value}s base candle, '
                f'got {candle.timeframe_seconds}s.'
            )
        if candle.symbol.strip().upper() != self.symbol:
            raise ValueError(f'Expected candle for {self.symbol}, got {candle.symbol}.')

        gaps: list[CandleGap] = []
        candle_opened_at = _as_utc(candle.opened_at)
        if self._last_base_closed_at is not None and candle_opened_at > self._last_base_closed_at:
            missing = int(
                (candle_opened_at - self._last_base_closed_at).total_seconds()
                // BASE_TIMEFRAME.value
            )
            if missing > 0:
                gaps.append(
                    CandleGap(
                        symbol=self.symbol,
                        previous_closed_at=self._last_base_closed_at,
                        next_opened_at=candle_opened_at,
                        missing_base_candles=missing,
                    )
                )
        self._last_base_closed_at = _as_utc(candle.closed_at)

        closed_bars: list[TimeframeBar] = [
            TimeframeBar(
                candle=candle,
                timeframe=BASE_TIMEFRAME,
                session_key=session_key,
                completeness=BarCompleteness.COMPLETE,
                source_bar_count=1,
                expected_source_bar_count=1,
                missing_source_bar_count=0,
            )
        ]
        for timeframe in AGGREGATED_TIMEFRAMES:
            closed_bars.extend(
                self._add_to_timeframe(
                    candle=candle,
                    timeframe=timeframe,
                    session_key=session_key,
                    session_start_time=session_start_time,
                    session_24_7=session_24_7,
                )
            )
        return MultiTimeframeUpdate(closed_bars=tuple(closed_bars), gaps=tuple(gaps))

    def flush_partial(self) -> tuple[TimeframeBar, ...]:
        partials: list[TimeframeBar] = []
        for timeframe in AGGREGATED_TIMEFRAMES:
            bucket = self._buckets.pop(timeframe, None)
            if bucket is None or not bucket.base_candles:
                continue
            partials.append(self._finalize_bucket(bucket, forced=BarCompleteness.PARTIAL))
        self._last_base_closed_at = None
        return tuple(partials)

    def _add_to_timeframe(
        self,
        *,
        candle: Candle,
        timeframe: Timeframe,
        session_key: str,
        session_start_time: datetime | None,
        session_24_7: bool,
    ) -> list[TimeframeBar]:
        bucket_start, bucket_end = _bucket_bounds(
            opened_at=candle.opened_at,
            timeframe=timeframe,
            session_start_time=session_start_time,
            session_24_7=session_24_7,
        )
        closed: list[TimeframeBar] = []
        bucket = self._buckets.get(timeframe)
        if bucket is not None and (
            bucket.session_key != session_key or bucket.bucket_start != bucket_start
        ):
            closed.append(self._finalize_bucket(bucket))
            bucket = None
        if bucket is None:
            bucket = _AggregateBucket(
                timeframe=timeframe,
                session_key=session_key,
                bucket_start=bucket_start,
                bucket_end=bucket_end,
            )
            self._buckets[timeframe] = bucket
        bucket.base_candles.append(candle)
        if _as_utc(candle.closed_at) >= bucket.bucket_end:
            closed.append(self._finalize_bucket(bucket))
            self._buckets.pop(timeframe, None)
        return closed

    def _finalize_bucket(
        self,
        bucket: _AggregateBucket,
        forced: BarCompleteness | None = None,
    ) -> TimeframeBar:
        candles = bucket.base_candles
        expected_count = bucket.timeframe.value // BASE_TIMEFRAME.value
        complete = (
            len(candles) == expected_count
            and _as_utc(candles[0].opened_at) == bucket.bucket_start
            and _as_utc(candles[-1].closed_at) == bucket.bucket_end
            and _candles_are_contiguous(candles)
        )
        completeness = forced or (
            BarCompleteness.COMPLETE if complete else BarCompleteness.INCOMPLETE
        )
        volumes = [candle.volume for candle in candles]
        volume = (
            sum(float(value) for value in volumes)
            if volumes and all(value is not None for value in volumes)
            else None
        )
        aggregated = Candle(
            symbol=self.symbol,
            timeframe_seconds=bucket.timeframe.value,
            open=candles[0].open,
            high=max(candle.high for candle in candles),
            low=min(candle.low for candle in candles),
            close=candles[-1].close,
            volume=volume,
            opened_at=bucket.bucket_start,
            closed_at=bucket.bucket_end,
            sample_count=sum(max(0, candle.sample_count) for candle in candles),
        )
        return TimeframeBar(
            candle=aggregated,
            timeframe=bucket.timeframe,
            session_key=bucket.session_key,
            completeness=completeness,
            source_bar_count=len(candles),
            expected_source_bar_count=expected_count,
            missing_source_bar_count=max(0, expected_count - len(candles)),
        )


class MultiTimeframeService:
    def __init__(self, configs: dict[str, MultiTimeframeConfig] | None = None) -> None:
        self._configs = {
            symbol.strip().upper(): config for symbol, config in (configs or {}).items()
        }
        self._engines: dict[str, MultiTimeframeCandleEngine] = {}
        self._stores: dict[str, TimeframeSeriesStore] = defaultdict(TimeframeSeriesStore)

    def reset_symbol(
        self,
        symbol: str,
        *,
        clear_history: bool = False,
    ) -> tuple[TimeframeBar, ...]:
        normalized = symbol.strip().upper()
        engine = self._engines.pop(normalized, None)
        partials = engine.flush_partial() if engine is not None else ()
        for bar in partials:
            self._stores[normalized].append(bar)
        if clear_history:
            self._stores[normalized].clear()
        return partials

    def on_base_candle(
        self,
        *,
        symbol: str,
        candle: Candle,
        session_decision: Any,
    ) -> MultiTimeframeUpdate:
        normalized = symbol.strip().upper()
        session_key = getattr(session_decision, 'session_key', None) or 'unknown_session'
        engine = self._engines.setdefault(
            normalized,
            MultiTimeframeCandleEngine(normalized),
        )
        update = engine.on_base_candle(
            candle,
            session_key=session_key,
            session_start_time=getattr(session_decision, 'session_start_time', None),
            session_24_7=bool(getattr(session_decision, 'session_24_7', False)),
        )
        for bar in update.closed_bars:
            self._stores[normalized].append(bar)
        return update

    def build_context(
        self,
        *,
        symbol: str,
        side: str,
        as_of: datetime,
        session_decision: Any,
    ) -> MultiTimeframeContext:
        normalized = symbol.strip().upper()
        config = self._configs.get(normalized, MultiTimeframeConfig())
        actual_as_of = _as_utc(as_of)
        desired = TimeframeDirection.UP if side.upper() == 'BUY' else TimeframeDirection.DOWN
        opposite = (
            TimeframeDirection.DOWN
            if desired == TimeframeDirection.UP
            else TimeframeDirection.UP
        )
        features: dict[str, TimeframeFeatures] = {}
        maturity_by_timeframe: dict[str, TimeframeMaturity] = {}
        unavailable: list[str] = []
        ready_aligned: list[str] = []
        ready_opposed: list[str] = []
        inclusive_aligned: list[str] = []
        inclusive_opposed: list[str] = []

        for timeframe in SUPPORTED_TIMEFRAMES:
            key = timeframe.name.lower()
            bars = self._stores[normalized].bars(
                timeframe,
                as_of=actual_as_of,
                complete_only=True,
            )
            calculated = _timeframe_features(
                timeframe=timeframe,
                bars=bars,
                config=config.feature_config_for(timeframe),
                as_of=actual_as_of,
            )
            if calculated is None:
                maturity_by_timeframe[key] = TimeframeMaturity.UNAVAILABLE
                unavailable.append(key)
                continue
            features[key] = calculated
            maturity_by_timeframe[key] = calculated.maturity
            if calculated.direction == desired:
                inclusive_aligned.append(key)
                if calculated.maturity == TimeframeMaturity.READY:
                    ready_aligned.append(key)
            elif calculated.direction == opposite:
                inclusive_opposed.append(key)
                if calculated.maturity == TimeframeMaturity.READY:
                    ready_opposed.append(key)

        return MultiTimeframeContext(
            model_version=MULTI_TIMEFRAME_MODEL_VERSION,
            as_of=actual_as_of,
            side=side.upper(),
            features_by_timeframe=features,
            maturity_by_timeframe=maturity_by_timeframe,
            opening_ranges=_opening_ranges(
                store=self._stores[normalized],
                config=config,
                as_of=actual_as_of,
                session_decision=session_decision,
            ),
            ready_aligned_timeframes=tuple(ready_aligned),
            ready_opposed_timeframes=tuple(ready_opposed),
            inclusive_aligned_timeframes=tuple(inclusive_aligned),
            inclusive_opposed_timeframes=tuple(inclusive_opposed),
            unavailable_timeframes=tuple(unavailable),
            ready_alignment=_alignment(aligned=ready_aligned, opposed=ready_opposed),
            alignment_including_provisional=_alignment(
                aligned=inclusive_aligned,
                opposed=inclusive_opposed,
            ),
        )

    def bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        as_of: datetime | None = None,
        complete_only: bool = False,
    ) -> list[TimeframeBar]:
        return self._stores[symbol.strip().upper()].bars(
            timeframe,
            as_of=as_of,
            complete_only=complete_only,
        )


def expected_sampling_quality(poll_interval_seconds: int) -> SamplingQuality:
    if poll_interval_seconds <= 15:
        return SamplingQuality.DENSE
    if poll_interval_seconds <= 30:
        return SamplingQuality.ACCEPTABLE
    return SamplingQuality.SPARSE


def _timeframe_features(
    *,
    timeframe: Timeframe,
    bars: list[TimeframeBar],
    config: TimeframeFeatureConfig,
    as_of: datetime,
) -> TimeframeFeatures | None:
    if len(bars) < config.provisional_bars:
        return None
    candles = [bar.candle for bar in bars]
    latest = candles[-1]
    closes = [candle.close for candle in candles]
    latest_close = latest.close
    if latest_close <= 0:
        return None

    maturity = (
        TimeframeMaturity.READY
        if len(candles) >= config.ready_bars
        else TimeframeMaturity.PROVISIONAL
    )
    ema_fast_window = min(config.ema_fast_bars, len(closes))
    ema_slow_window = min(config.ema_slow_bars, len(closes))
    ema_fast = _ema(closes[-ema_fast_window:])
    ema_slow = _ema(closes[-ema_slow_window:])
    direction = _direction(latest_close, ema_fast, ema_slow)
    returns = [
        _signed_percent(closes[index], closes[index - 1])
        for index in range(1, len(closes))
    ]
    recent_velocity = _average(returns[-min(config.acceleration_window_bars, len(returns)):])
    sample_per_base_bar = latest.sample_count / max(
        1,
        timeframe.value // BASE_TIMEFRAME.value,
    )
    common = dict(
        timeframe=timeframe.name.lower(),
        maturity=maturity,
        as_of=as_of,
        latest_bar_closed_at=_as_utc(latest.closed_at),
        bar_count=len(candles),
        covered_seconds=max(
            0,
            int((_as_utc(latest.closed_at) - _as_utc(candles[0].opened_at)).total_seconds()),
        ),
        direction=direction,
        sampling_quality=_sampling_quality(sample_per_base_bar),
        close=round(latest_close, 8),
        ema_fast=round(ema_fast, 8),
        ema_slow=round(ema_slow, 8),
        return_1_bar_percent=round(
            _signed_percent(closes[-1], closes[-2]),
            6,
        ),
        return_sample_percent=round(
            _signed_percent(closes[-1], closes[0]),
            6,
        ),
        velocity_percent_per_bar=round(recent_velocity, 6),
    )
    if maturity == TimeframeMaturity.PROVISIONAL:
        return TimeframeFeatures(**common)

    range_candles = candles[-config.range_lookback_bars:]
    rolling_high = max(candle.high for candle in range_candles)
    rolling_low = min(candle.low for candle in range_candles)
    rolling_range = rolling_high - rolling_low
    reference_open = range_candles[0].open
    true_ranges = _true_ranges(candles)
    atr = _average(true_ranges[-config.atr_lookback_bars:])
    compression_window = true_ranges[-(config.compression_lookback_bars + 1):]
    historical_ranges = compression_window[:-1]
    compression_reference = median(historical_ranges)
    compression_ratio = (
        compression_window[-1] / compression_reference
        if compression_reference > 0
        else 0.0
    )
    window = config.acceleration_window_bars
    previous_velocity = _average(returns[-(window * 2):-window])
    latest_range = latest.high - latest.low
    body = abs(latest.close - latest.open)
    upper_wick = latest.high - max(latest.open, latest.close)
    lower_wick = min(latest.open, latest.close) - latest.low

    return TimeframeFeatures(
        **common,
        close_vs_fast_ema_percent=round(_signed_percent(latest_close, ema_fast), 6),
        fast_vs_slow_ema_percent=round(_signed_percent(ema_fast, ema_slow), 6),
        atr_percent=round(_percent(atr, latest_close), 6),
        return_3_bars_percent=round(_signed_percent(closes[-1], closes[-4]), 6),
        rolling_high=round(rolling_high, 8),
        rolling_low=round(rolling_low, 8),
        rolling_range_percent=round(_percent(rolling_range, reference_open), 6),
        range_position_percent=round(_position(latest_close, rolling_low, rolling_high), 6),
        distance_to_range_high_percent=round(
            _percent(rolling_high - latest_close, latest_close),
            6,
        ),
        distance_to_range_low_percent=round(
            _percent(latest_close - rolling_low, latest_close),
            6,
        ),
        previous_bar_high=round(candles[-2].high, 8),
        previous_bar_low=round(candles[-2].low, 8),
        body_percent_of_range=round(_ratio_percent(body, latest_range), 6),
        upper_wick_percent_of_range=round(_ratio_percent(upper_wick, latest_range), 6),
        lower_wick_percent_of_range=round(_ratio_percent(lower_wick, latest_range), 6),
        close_position_percent=round(_position(latest.close, latest.low, latest.high), 6),
        compression_ratio=round(compression_ratio, 6),
        previous_velocity_percent_per_bar=round(previous_velocity, 6),
        acceleration_percent_per_bar=round(recent_velocity - previous_velocity, 6),
        pullback_from_recent_high_percent=round(
            _percent(rolling_high - latest_close, rolling_high),
            6,
        ),
        rebound_from_recent_low_percent=round(
            _percent(latest_close - rolling_low, rolling_low),
            6,
        ),
    )


def _opening_ranges(
    *,
    store: TimeframeSeriesStore,
    config: MultiTimeframeConfig,
    as_of: datetime,
    session_decision: Any,
) -> OpeningRangeFeatures:
    session_key = getattr(session_decision, 'session_key', None)
    session_24_7 = bool(getattr(session_decision, 'session_24_7', False))
    session_start = getattr(session_decision, 'session_start_time', None)
    if session_24_7 or session_start is None or session_key is None:
        return OpeningRangeFeatures(
            session_key=session_key,
            windows={
                str(minutes): OpeningRangeWindow(
                    minutes=minutes,
                    status=OpeningRangeStatus.NOT_APPLICABLE,
                    expected_source_bar_count=minutes,
                )
                for minutes in config.opening_range_minutes
            },
        )

    start = _as_utc(session_start)
    session_bars = store.bars(
        BASE_TIMEFRAME,
        as_of=as_of,
        complete_only=True,
        session_key=session_key,
    )
    latest_close = session_bars[-1].candle.close if session_bars else None
    windows: dict[str, OpeningRangeWindow] = {}
    for minutes in config.opening_range_minutes:
        end = start + timedelta(minutes=minutes)
        key = str(minutes)
        if as_of < end:
            windows[key] = OpeningRangeWindow(
                minutes=minutes,
                status=OpeningRangeStatus.WARMING_UP,
                source_bar_count=len(
                    [
                        bar for bar in session_bars
                        if _as_utc(bar.candle.opened_at) >= start
                        and _as_utc(bar.candle.closed_at) <= as_of
                    ]
                ),
                expected_source_bar_count=minutes,
            )
            continue
        bars = [
            bar for bar in session_bars
            if _as_utc(bar.candle.opened_at) >= start
            and _as_utc(bar.candle.closed_at) <= end
        ]
        candles = [bar.candle for bar in bars]
        complete = (
            len(candles) == minutes
            and candles
            and _as_utc(candles[0].opened_at) == start
            and _as_utc(candles[-1].closed_at) == end
            and _candles_are_contiguous(candles)
        )
        if not complete or latest_close is None:
            windows[key] = OpeningRangeWindow(
                minutes=minutes,
                status=OpeningRangeStatus.INCOMPLETE,
                source_bar_count=len(candles),
                expected_source_bar_count=minutes,
            )
            continue
        high = max(candle.high for candle in candles)
        low = min(candle.low for candle in candles)
        windows[key] = OpeningRangeWindow(
            minutes=minutes,
            status=OpeningRangeStatus.READY,
            high=round(high, 8),
            low=round(low, 8),
            range_percent=round(_percent(high - low, low), 6),
            position_percent=round(_position(latest_close, low, high), 6),
            distance_to_high_percent=round(_percent(high - latest_close, latest_close), 6),
            distance_to_low_percent=round(_percent(latest_close - low, latest_close), 6),
            breakout_above_percent=round(max(0.0, _signed_percent(latest_close, high)), 6),
            breakdown_below_percent=round(max(0.0, _signed_percent(low, latest_close)), 6),
            source_bar_count=len(candles),
            expected_source_bar_count=minutes,
        )
    return OpeningRangeFeatures(session_key=session_key, windows=windows)


def _bucket_bounds(
    *,
    opened_at: datetime,
    timeframe: Timeframe,
    session_start_time: datetime | None,
    session_24_7: bool,
) -> tuple[datetime, datetime]:
    opened = _as_utc(opened_at)
    if not session_24_7 and session_start_time is not None:
        anchor = _as_utc(session_start_time)
    else:
        anchor = opened.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((opened - anchor).total_seconds())
    if elapsed < 0:
        raise ValueError(
            f'Candle {opened.isoformat()} precedes session anchor {anchor.isoformat()}.'
        )
    bucket_index = elapsed // timeframe.value
    bucket_start = anchor + timedelta(seconds=bucket_index * timeframe.value)
    return bucket_start, bucket_start + timedelta(seconds=timeframe.value)


def _candles_are_contiguous(candles: list[Candle]) -> bool:
    return all(
        _as_utc(previous.closed_at) == _as_utc(current.opened_at)
        for previous, current in zip(candles, candles[1:])
    )


def _alignment(
    *,
    aligned: list[str],
    opposed: list[str],
) -> MultiTimeframeAlignment:
    if not aligned and not opposed:
        return MultiTimeframeAlignment.UNKNOWN
    if aligned and not opposed:
        return MultiTimeframeAlignment.ALIGNED
    if opposed and not aligned:
        return MultiTimeframeAlignment.OPPOSED
    return MultiTimeframeAlignment.MIXED


def _direction(
    close: float,
    ema_fast: float,
    ema_slow: float,
) -> TimeframeDirection:
    if close > ema_fast > ema_slow:
        return TimeframeDirection.UP
    if close < ema_fast < ema_slow:
        return TimeframeDirection.DOWN
    return TimeframeDirection.MIXED


def _sampling_quality(samples_per_base_bar: float) -> SamplingQuality:
    if samples_per_base_bar >= 4:
        return SamplingQuality.DENSE
    if samples_per_base_bar >= 2:
        return SamplingQuality.ACCEPTABLE
    return SamplingQuality.SPARSE


def _ema(values: list[float]) -> float:
    if not values:
        return 0.0
    multiplier = 2 / (len(values) + 1)
    value = values[0]
    for current in values[1:]:
        value = (current - value) * multiplier + value
    return value


def _true_ranges(candles: list[Candle]) -> list[float]:
    result: list[float] = []
    for index, candle in enumerate(candles):
        if index == 0:
            result.append(candle.high - candle.low)
            continue
        previous_close = candles[index - 1].close
        result.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
    return result


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _signed_percent(value: float, reference: float) -> float:
    if reference <= 0:
        return 0.0
    return ((value - reference) / reference) * 100


def _percent(value: float, reference: float) -> float:
    if reference <= 0:
        return 0.0
    return (value / reference) * 100


def _ratio_percent(value: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return (value / total) * 100


def _position(value: float, low: float, high: float) -> float:
    spread = high - low
    if spread <= 0:
        return 50.0
    return ((value - low) / spread) * 100


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
