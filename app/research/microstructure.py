from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from statistics import median

from app.market.models import MarketSnapshot, PriceSource

MICROSTRUCTURE_CONTRACT_VERSION = 'etoro_microstructure_v2'
MICROSTRUCTURE_WINDOWS_SECONDS = (10, 30, 60)
MICROSTRUCTURE_RETENTION_SECONDS = 120
MICROSTRUCTURE_MAX_OBSERVATIONS_PER_SYMBOL = 4096

_COMMON_FEATURE_SUFFIXES = (
    'quote_count',
    'quote_rate_hz',
    'sample_count',
    'temporal_coverage_ratio',
    'mid_return_percent',
    'mid_absolute_path_percent',
    'mid_tick_imbalance',
    'mid_change_count',
    'mid_path_efficiency',
    'mid_directional_persistence',
    'bid_tick_imbalance',
    'bid_change_count',
    'ask_tick_imbalance',
    'ask_change_count',
    'bid_vs_ask_update_imbalance',
    'last_tick_imbalance',
    'last_change_count',
    'last_value_change_ratio',
    'spread_mean_bps',
    'spread_change_bps',
    'interarrival_median_ms',
    'interarrival_burstiness',
    'last_position_in_spread',
)
_SIXTY_SECOND_ONLY_SUFFIXES = (
    'spread_min_bps',
    'spread_max_bps',
    'spread_range_bps',
)

MICROSTRUCTURE_FORMULA_DEFINITIONS = {
    'quote_count': 'N accepted snapshots in [T-W,T)',
    'quote_rate_hz': 'quote_count / W_seconds',
    'sample_count': (
        '1 + adjacent changes in (bid,ask,canonical_broker_last)'
    ),
    'temporal_coverage_ratio': (
        'clip((received_last-received_first)/W_seconds,0,1)'
    ),
    'mid_return_percent': '(mid_last/mid_first-1)*100',
    'mid_absolute_path_percent': 'sum(abs(delta_mid))/mid_first*100',
    'mid_tick_imbalance': '(up_mid-down_mid)/(up_mid+down_mid)',
    'mid_change_count': 'up_mid+down_mid',
    'mid_path_efficiency': (
        'abs(mid_last-mid_first)/sum(abs(delta_mid))'
    ),
    'mid_directional_persistence': (
        '(mid_last-mid_first)/sum(abs(delta_mid))'
    ),
    'bid_tick_imbalance': '(up_bid-down_bid)/(up_bid+down_bid)',
    'bid_change_count': 'up_bid+down_bid',
    'ask_tick_imbalance': '(up_ask-down_ask)/(up_ask+down_ask)',
    'ask_change_count': 'up_ask+down_ask',
    'bid_vs_ask_update_imbalance': (
        '(bid_change_count-ask_change_count)'
        '/(bid_change_count+ask_change_count)'
    ),
    'last_tick_imbalance': '(up_last-down_last)/(up_last+down_last)',
    'last_change_count': 'up_last+down_last',
    'last_value_change_ratio': (
        'canonical broker-last value changes divided by canonical broker-last '
        'transitions; carried merged values remain observations and this is '
        'not a LastExecution retransmission rate'
    ),
    'spread_mean_bps': 'mean((ask-bid)/mid*10000)',
    'spread_change_bps': 'spread_bps_last-spread_bps_first',
    'interarrival_median_ms': 'median(nonnegative_received_at_deltas_ms)',
    'interarrival_burstiness': '(P90_delta-P10_delta)/(P90_delta+P10_delta)',
    'last_position_in_spread': '(canonical_broker_last-bid)/(ask-bid)',
    'spread_min_bps': 'min(spread_bps)',
    'spread_max_bps': 'max(spread_bps)',
    'spread_range_bps': 'spread_max_bps-spread_min_bps',
}


def _feature_names() -> tuple[str, ...]:
    names: list[str] = []
    for window_seconds in MICROSTRUCTURE_WINDOWS_SECONDS:
        prefix = f'micro_{window_seconds}s_'
        names.extend(prefix + suffix for suffix in _COMMON_FEATURE_SUFFIXES)
        if window_seconds == 60:
            names.extend(
                prefix + suffix for suffix in _SIXTY_SECOND_ONLY_SUFFIXES
            )
    return tuple(names)


MICROSTRUCTURE_FEATURE_NAMES = _feature_names()
MICROSTRUCTURE_FEATURE_SET_SHA256 = hashlib.sha256(
    json.dumps(
        MICROSTRUCTURE_FEATURE_NAMES,
        separators=(',', ':'),
    ).encode('utf-8')
).hexdigest()


@dataclass(frozen=True)
class _QuoteObservation:
    market_timestamp: datetime
    observed_at: datetime
    bid: float
    ask: float
    mid: float
    last_execution: float | None

    @property
    def spread_bps(self) -> float:
        if self.mid <= 0:
            return 0.0
        return ((self.ask - self.bid) / self.mid) * 10_000


class EtoroMicrostructureAccumulator:
    """Bounded, research-only accumulator for accepted eToro price quotes.

    Updates are O(1). Feature calculation happens only at the five-minute
    research cadence and scans at most the bounded in-memory window.
    """

    def __init__(self) -> None:
        self._observations: dict[str, deque[_QuoteObservation]] = defaultdict(
            lambda: deque(
                maxlen=MICROSTRUCTURE_MAX_OBSERVATIONS_PER_SYMBOL
            )
        )

    def observe(self, snapshot: MarketSnapshot) -> None:
        bid = float(snapshot.bid)
        ask = float(snapshot.ask)
        if not all(math.isfinite(value) and value > 0 for value in (bid, ask)):
            return
        if ask < bid:
            return
        observed_at = _as_utc(snapshot.received_at or snapshot.timestamp)
        market_timestamp = _as_utc(snapshot.timestamp)
        mid = (bid + ask) / 2
        last_execution = (
            float(snapshot.last)
            if snapshot.price_source is PriceSource.BROKER_LAST
            and math.isfinite(float(snapshot.last))
            else None
        )
        symbol = snapshot.symbol.strip().upper()
        observations = self._observations[symbol]
        observations.append(
            _QuoteObservation(
                market_timestamp=market_timestamp,
                observed_at=observed_at,
                bid=bid,
                ask=ask,
                mid=mid,
                last_execution=last_execution,
            )
        )
        cutoff = observed_at - timedelta(
            seconds=MICROSTRUCTURE_RETENTION_SECONDS
        )
        while observations and observations[0].observed_at < cutoff:
            observations.popleft()

    def reset_symbol(self, symbol: str) -> None:
        self._observations.pop(symbol.strip().upper(), None)

    def features(
        self,
        *,
        symbol: str,
        state_at: datetime,
    ) -> dict[str, int | float | None]:
        cutoff = _as_utc(state_at)
        observations = tuple(
            observation
            for observation in self._observations.get(
                symbol.strip().upper(), ()
            )
            if _causally_available(observation, cutoff)
        )
        result: dict[str, int | float | None] = {
            name: None for name in MICROSTRUCTURE_FEATURE_NAMES
        }
        for window_seconds in MICROSTRUCTURE_WINDOWS_SECONDS:
            window_start = cutoff - timedelta(seconds=window_seconds)
            window = [
                observation
                for observation in observations
                if window_start <= observation.observed_at < cutoff
            ]
            result.update(
                _window_features(
                    window,
                    window_seconds=window_seconds,
                )
            )
        return result


def microstructure_contract_metadata() -> dict[str, object]:
    return {
        'version': MICROSTRUCTURE_CONTRACT_VERSION,
        'input': 'accepted_websocket_market_snapshots_only',
        'windows_seconds': list(MICROSTRUCTURE_WINDOWS_SECONDS),
        'window_convention': '[state_at-window, state_at)',
        'availability_convention': (
            'market_timestamp < state_at and received_at < state_at'
        ),
        'feature_names': list(MICROSTRUCTURE_FEATURE_NAMES),
        'feature_set_sha256': MICROSTRUCTURE_FEATURE_SET_SHA256,
        'feature_definitions_by_suffix': dict(
            MICROSTRUCTURE_FORMULA_DEFINITIONS
        ),
        'maximum_observations_per_symbol': (
            MICROSTRUCTURE_MAX_OBSERVATIONS_PER_SYMBOL
        ),
        'last_execution_presence_source_of_truth': (
            'etoro_payload_schema patch_presence_count'
        ),
    }


def _window_features(
    observations: list[_QuoteObservation],
    *,
    window_seconds: int,
) -> dict[str, int | float | None]:
    prefix = f'micro_{window_seconds}s_'
    count = len(observations)
    result: dict[str, int | float | None] = {
        prefix + suffix: None for suffix in _COMMON_FEATURE_SUFFIXES
    }
    if window_seconds == 60:
        result.update(
            {
                prefix + suffix: None
                for suffix in _SIXTY_SECOND_ONLY_SUFFIXES
            }
        )
    result[prefix + 'quote_count'] = count
    result[prefix + 'quote_rate_hz'] = _round(count / window_seconds)
    if not observations:
        result[prefix + 'sample_count'] = 0
        result[prefix + 'temporal_coverage_ratio'] = 0.0
        return result

    result[prefix + 'sample_count'] = _price_state_sample_count(observations)
    coverage_seconds = (
        observations[-1].observed_at - observations[0].observed_at
    ).total_seconds()
    result[prefix + 'temporal_coverage_ratio'] = _round(
        min(1.0, max(0.0, coverage_seconds) / window_seconds)
    )

    mids = [observation.mid for observation in observations]
    bids = [observation.bid for observation in observations]
    asks = [observation.ask for observation in observations]
    actual_lasts = [
        observation.last_execution
        for observation in observations
        if observation.last_execution is not None
    ]
    spreads = [observation.spread_bps for observation in observations]

    result[prefix + 'mid_return_percent'] = _return_percent(mids)
    result[prefix + 'mid_absolute_path_percent'] = _absolute_path_percent(
        mids
    )
    result[prefix + 'mid_tick_imbalance'] = _tick_imbalance(mids)
    result[prefix + 'mid_change_count'] = _change_count(mids)
    signed_efficiency = _signed_path_efficiency(mids)
    result[prefix + 'mid_directional_persistence'] = signed_efficiency
    result[prefix + 'mid_path_efficiency'] = (
        None if signed_efficiency is None else _round(abs(signed_efficiency))
    )
    result[prefix + 'bid_tick_imbalance'] = _tick_imbalance(bids)
    bid_changes = _change_count(bids)
    result[prefix + 'bid_change_count'] = bid_changes
    result[prefix + 'ask_tick_imbalance'] = _tick_imbalance(asks)
    ask_changes = _change_count(asks)
    result[prefix + 'ask_change_count'] = ask_changes
    result[prefix + 'bid_vs_ask_update_imbalance'] = _count_imbalance(
        bid_changes,
        ask_changes,
    )
    result[prefix + 'last_tick_imbalance'] = _tick_imbalance(actual_lasts)
    last_changes = _change_count(actual_lasts)
    result[prefix + 'last_change_count'] = last_changes
    result[prefix + 'last_value_change_ratio'] = (
        None
        if len(actual_lasts) < 2
        else _round(last_changes / (len(actual_lasts) - 1))
    )
    result[prefix + 'spread_mean_bps'] = _round(
        sum(spreads) / len(spreads)
    )
    result[prefix + 'spread_change_bps'] = (
        None if len(spreads) < 2 else _round(spreads[-1] - spreads[0])
    )
    if window_seconds == 60:
        minimum = min(spreads)
        maximum = max(spreads)
        result[prefix + 'spread_min_bps'] = _round(minimum)
        result[prefix + 'spread_max_bps'] = _round(maximum)
        result[prefix + 'spread_range_bps'] = _round(maximum - minimum)

    arrivals_ms = [
        max(
            0.0,
            (
                observations[index].observed_at
                - observations[index - 1].observed_at
            ).total_seconds()
            * 1000,
        )
        for index in range(1, len(observations))
    ]
    result[prefix + 'interarrival_median_ms'] = (
        None if not arrivals_ms else _round(median(arrivals_ms), digits=3)
    )
    result[prefix + 'interarrival_burstiness'] = _robust_burstiness(
        arrivals_ms
    )
    latest = observations[-1]
    result[prefix + 'last_position_in_spread'] = _last_position_in_spread(
        latest
    )
    return result


def _causally_available(
    observation: _QuoteObservation,
    state_at: datetime,
) -> bool:
    return (
        observation.market_timestamp < state_at
        and observation.observed_at < state_at
    )


def _price_state_sample_count(
    observations: list[_QuoteObservation],
) -> int:
    count = 1
    for previous, current in pairwise(observations):
        if (
            previous.bid,
            previous.ask,
            previous.last_execution,
        ) != (
            current.bid,
            current.ask,
            current.last_execution,
        ):
            count += 1
    return count


def _tick_imbalance(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    upward = 0
    downward = 0
    for previous, current in pairwise(values):
        if current > previous:
            upward += 1
        elif current < previous:
            downward += 1
    directional = upward + downward
    if directional == 0:
        return 0.0
    return _round((upward - downward) / directional)


def _change_count(values: list[float]) -> int:
    return sum(current != previous for previous, current in pairwise(values))


def _count_imbalance(left: int, right: int) -> float:
    total = left + right
    if total == 0:
        return 0.0
    return _round((left - right) / total)


def _return_percent(values: list[float]) -> float | None:
    if len(values) < 2 or values[0] <= 0:
        return None
    return _round(((values[-1] / values[0]) - 1) * 100)


def _absolute_path_percent(values: list[float]) -> float | None:
    if len(values) < 2 or values[0] <= 0:
        return None
    path = sum(abs(current - previous) for previous, current in pairwise(values))
    return _round((path / values[0]) * 100)


def _signed_path_efficiency(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    path = sum(abs(current - previous) for previous, current in pairwise(values))
    if path == 0:
        return 0.0
    return _round((values[-1] - values[0]) / path)


def _robust_burstiness(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    p10 = _percentile(values, 0.10)
    p90 = _percentile(values, 0.90)
    denominator = p90 + p10
    if denominator == 0:
        return 0.0
    return _round((p90 - p10) / denominator)


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _last_position_in_spread(
    observation: _QuoteObservation,
) -> float | None:
    if observation.last_execution is None:
        return None
    spread = observation.ask - observation.bid
    if spread <= 0:
        return None
    return _round((observation.last_execution - observation.bid) / spread)


def _round(value: float, *, digits: int = 8) -> float:
    return round(float(value), digits)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
