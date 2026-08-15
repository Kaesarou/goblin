from datetime import UTC, datetime, timedelta

import pytest

from app.market.models import MarketSnapshot, PriceSource
from app.research.microstructure import (
    MICROSTRUCTURE_FORMULA_DEFINITIONS,
    MICROSTRUCTURE_MAX_OBSERVATIONS_PER_SYMBOL,
    EtoroMicrostructureAccumulator,
    microstructure_contract_metadata,
)

STATE_AT = datetime(2026, 8, 17, 14, 0, tzinfo=UTC)


def _snapshot(
    *,
    seconds_before: float,
    bid: float,
    ask: float,
    last: float | None = None,
    market_seconds_before: float | None = None,
    price_source: PriceSource = PriceSource.BROKER_LAST,
) -> MarketSnapshot:
    observed_at = STATE_AT - timedelta(seconds=seconds_before)
    market_timestamp = STATE_AT - timedelta(
        seconds=(
            seconds_before
            if market_seconds_before is None
            else market_seconds_before
        )
    )
    return MarketSnapshot(
        symbol='AAPL',
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2 if last is None else last,
        timestamp=market_timestamp,
        received_at=observed_at,
        price_source=price_source,
    )


def test_windows_are_half_open_and_both_timestamps_are_strictly_causal():
    accumulator = EtoroMicrostructureAccumulator()
    for snapshot in (
        _snapshot(seconds_before=60, bid=99.9, ask=100.1),
        _snapshot(seconds_before=30, bid=100.0, ask=100.2),
        _snapshot(seconds_before=10, bid=100.1, ask=100.3),
        _snapshot(seconds_before=0, bid=100.2, ask=100.4),
        _snapshot(
            seconds_before=1,
            market_seconds_before=0,
            bid=100.3,
            ask=100.5,
        ),
    ):
        accumulator.observe(snapshot)

    features = accumulator.features(symbol='AAPL', state_at=STATE_AT)

    assert features['micro_10s_quote_count'] == 1
    assert features['micro_30s_quote_count'] == 2
    assert features['micro_60s_quote_count'] == 3


def test_interpretable_feature_formulas_are_frozen():
    accumulator = EtoroMicrostructureAccumulator()
    for seconds_before, bid, ask, last in (
        (9, 99.0, 101.0, 99.5),
        (7, 100.0, 102.0, 101.0),
        (4, 99.0, 101.0, 100.5),
        (1, 101.0, 103.0, 102.0),
    ):
        accumulator.observe(
            _snapshot(
                seconds_before=seconds_before,
                bid=bid,
                ask=ask,
                last=last,
            )
        )

    features = accumulator.features(symbol='AAPL', state_at=STATE_AT)
    prefix = 'micro_10s_'

    assert features[prefix + 'quote_count'] == 4
    assert features[prefix + 'quote_rate_hz'] == pytest.approx(0.4)
    assert features[prefix + 'sample_count'] == 4
    assert features[prefix + 'temporal_coverage_ratio'] == pytest.approx(0.8)
    assert features[prefix + 'mid_return_percent'] == pytest.approx(2.0)
    assert features[prefix + 'mid_absolute_path_percent'] == pytest.approx(4.0)
    assert features[prefix + 'mid_tick_imbalance'] == pytest.approx(1 / 3)
    assert features[prefix + 'mid_change_count'] == 3
    assert features[prefix + 'mid_path_efficiency'] == pytest.approx(0.5)
    assert features[prefix + 'mid_directional_persistence'] == pytest.approx(0.5)
    assert features[prefix + 'bid_tick_imbalance'] == pytest.approx(1 / 3)
    assert features[prefix + 'bid_change_count'] == 3
    assert features[prefix + 'ask_tick_imbalance'] == pytest.approx(1 / 3)
    assert features[prefix + 'ask_change_count'] == 3
    assert features[prefix + 'bid_vs_ask_update_imbalance'] == 0.0
    assert features[prefix + 'last_tick_imbalance'] == pytest.approx(1 / 3)
    assert features[prefix + 'last_change_count'] == 3
    assert features[prefix + 'last_update_activity_ratio'] == 1.0
    assert features[prefix + 'interarrival_median_ms'] == 3000.0
    assert features[prefix + 'interarrival_burstiness'] == pytest.approx(
        (3000 - 2200) / (3000 + 2200)
    )
    assert features[prefix + 'last_position_in_spread'] == 0.5


def test_spread_statistics_include_min_max_and_range_only_for_60_seconds():
    accumulator = EtoroMicrostructureAccumulator()
    accumulator.observe(
        _snapshot(seconds_before=50, bid=99.5, ask=100.5, last=100.0)
    )
    accumulator.observe(
        _snapshot(seconds_before=20, bid=99.0, ask=101.0, last=100.0)
    )
    accumulator.observe(
        _snapshot(seconds_before=1, bid=99.75, ask=100.25, last=100.0)
    )

    features = accumulator.features(symbol='AAPL', state_at=STATE_AT)

    assert features['micro_60s_spread_mean_bps'] == pytest.approx(350 / 3)
    assert features['micro_60s_spread_change_bps'] == pytest.approx(-50.0)
    assert features['micro_60s_spread_min_bps'] == pytest.approx(50.0)
    assert features['micro_60s_spread_max_bps'] == pytest.approx(200.0)
    assert features['micro_60s_spread_range_bps'] == pytest.approx(150.0)
    assert 'micro_30s_spread_min_bps' not in features


def test_bid_vs_ask_update_imbalance_uses_change_counts():
    accumulator = EtoroMicrostructureAccumulator()
    for seconds_before, bid, ask in (
        (5, 99.0, 102.0),
        (3, 100.0, 102.0),
        (1, 101.0, 103.0),
    ):
        accumulator.observe(
            _snapshot(seconds_before=seconds_before, bid=bid, ask=ask)
        )

    features = accumulator.features(symbol='AAPL', state_at=STATE_AT)

    assert features['micro_10s_bid_change_count'] == 2
    assert features['micro_10s_ask_change_count'] == 1
    assert features['micro_10s_bid_vs_ask_update_imbalance'] == pytest.approx(
        1 / 3
    )


def test_zero_one_two_flat_and_missing_last_have_explicit_semantics():
    accumulator = EtoroMicrostructureAccumulator()
    empty = accumulator.features(symbol='AAPL', state_at=STATE_AT)
    assert empty['micro_10s_quote_count'] == 0
    assert empty['micro_10s_sample_count'] == 0
    assert empty['micro_10s_mid_tick_imbalance'] is None

    accumulator.observe(
        _snapshot(
            seconds_before=9,
            bid=99.9,
            ask=100.1,
            price_source=PriceSource.BID_ASK_MIDPOINT,
        )
    )
    one = accumulator.features(symbol='AAPL', state_at=STATE_AT)
    assert one['micro_10s_sample_count'] == 1
    assert one['micro_10s_mid_return_percent'] is None
    assert one['micro_10s_interarrival_median_ms'] is None
    assert one['micro_10s_last_tick_imbalance'] is None
    assert one['micro_10s_last_position_in_spread'] is None

    accumulator.observe(
        _snapshot(
            seconds_before=1,
            bid=99.9,
            ask=100.1,
            price_source=PriceSource.BID_ASK_MIDPOINT,
        )
    )
    flat = accumulator.features(symbol='AAPL', state_at=STATE_AT)
    assert flat['micro_10s_quote_count'] == 2
    assert flat['micro_10s_sample_count'] == 1
    assert flat['micro_10s_mid_tick_imbalance'] == 0.0
    assert flat['micro_10s_mid_change_count'] == 0
    assert flat['micro_10s_mid_path_efficiency'] == 0.0
    assert flat['micro_10s_spread_change_bps'] == 0.0
    assert flat['micro_10s_interarrival_median_ms'] == 8000.0
    assert flat['micro_10s_interarrival_burstiness'] is None


def test_invalid_quotes_are_ignored_and_memory_is_bounded():
    accumulator = EtoroMicrostructureAccumulator()
    accumulator.observe(
        _snapshot(seconds_before=3, bid=0.0, ask=100.0)
    )
    accumulator.observe(
        _snapshot(seconds_before=2, bid=101.0, ask=100.0)
    )
    assert accumulator.features(
        symbol='AAPL', state_at=STATE_AT
    )['micro_10s_quote_count'] == 0

    for index in range(MICROSTRUCTURE_MAX_OBSERVATIONS_PER_SYMBOL + 100):
        observed_at = STATE_AT + timedelta(milliseconds=index)
        accumulator.observe(
            MarketSnapshot(
                symbol='AAPL',
                bid=99.9,
                ask=100.1,
                last=100.0,
                timestamp=observed_at,
                received_at=observed_at,
            )
        )

    assert len(accumulator._observations['AAPL']) == (
        MICROSTRUCTURE_MAX_OBSERVATIONS_PER_SYMBOL
    )


def test_retention_expires_only_observations_older_than_120_seconds():
    accumulator = EtoroMicrostructureAccumulator()
    accumulator.observe(
        _snapshot(seconds_before=122, bid=99.8, ask=100.0)
    )
    accumulator.observe(
        _snapshot(seconds_before=121, bid=99.9, ask=100.1)
    )
    accumulator.observe(
        _snapshot(seconds_before=1, bid=100.0, ask=100.2)
    )

    retained = accumulator._observations['AAPL']

    assert len(retained) == 2
    assert retained[0].observed_at == STATE_AT - timedelta(seconds=121)
    assert accumulator.features(
        symbol='AAPL', state_at=STATE_AT
    )['micro_60s_quote_count'] == 1


def test_versioned_contract_exposes_every_formula_definition():
    metadata = microstructure_contract_metadata()

    assert set(metadata['feature_definitions_by_suffix']) == set(
        MICROSTRUCTURE_FORMULA_DEFINITIONS
    )
    assert set(MICROSTRUCTURE_FORMULA_DEFINITIONS) == {
        *{
            name.removeprefix('micro_10s_')
            for name in metadata['feature_names']
            if name.startswith('micro_10s_')
        },
        'spread_min_bps',
        'spread_max_bps',
        'spread_range_bps',
    }
