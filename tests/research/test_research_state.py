from datetime import UTC, datetime, timedelta

import pytest

from app.market.models import Candle
from app.market.multi_timeframe import TimeframeBar
from app.market.timeframes import BarCompleteness, Timeframe
from app.research.pipeline import build_research_state_id
from app.research.research_state import (
    CANDLE_RESEARCH_FEATURE_NAMES,
    build_candle_research_features,
)

STATE_AT = datetime(2026, 8, 17, 14, 30, tzinfo=UTC)
SESSION_START = STATE_AT - timedelta(minutes=61)
SESSION_KEY = 'EQUITY_US:2026-08-17T13:30:00+00:00'


def _bar(
    *,
    closed_at: datetime,
    close: float,
    session_key: str = SESSION_KEY,
    degraded: bool = False,
    carried: bool = False,
) -> TimeframeBar:
    candle = Candle(
        symbol='AAPL',
        timeframe_seconds=60,
        open=close - 0.1,
        high=close + 0.3,
        low=close - 0.3,
        close=close,
        volume=None,
        opened_at=closed_at - timedelta(minutes=1),
        closed_at=closed_at,
        sample_count=2,
        quality_degraded=degraded,
        carried_forward=carried,
    )
    return TimeframeBar(
        candle=candle,
        timeframe=Timeframe.M1,
        session_key=session_key,
        completeness=BarCompleteness.COMPLETE,
        source_bar_count=1,
        expected_source_bar_count=1,
        missing_source_bar_count=0,
    )


def _bars() -> list[TimeframeBar]:
    result = []
    for offset in range(61):
        closed_at = STATE_AT - timedelta(minutes=60 - offset)
        result.append(
            _bar(
                closed_at=closed_at,
                close=100.0 + offset,
                degraded=offset == 25,
                carried=offset == 26,
            )
        )
    return result


def test_candle_features_are_flat_complete_and_strictly_cut_off():
    bars = _bars()
    baseline = build_candle_research_features(
        bars=bars,
        state_at=STATE_AT,
        session_key=SESSION_KEY,
        session_start_time=SESSION_START,
    )
    with_future = build_candle_research_features(
        bars=[
            *bars,
            _bar(
                closed_at=STATE_AT + timedelta(minutes=1),
                close=10_000.0,
            ),
            _bar(
                closed_at=STATE_AT,
                close=20_000.0,
                session_key='other-session',
            ),
        ],
        state_at=STATE_AT,
        session_key=SESSION_KEY,
        session_start_time=SESSION_START,
    )

    assert with_future == baseline
    assert set(baseline) == set(CANDLE_RESEARCH_FEATURE_NAMES)
    assert all(not isinstance(value, dict) for value in baseline.values())
    assert baseline['candle_coverage_60m_ratio'] == 1.0
    assert baseline['candle_degraded_count_60m'] == 1
    assert baseline['candle_carried_forward_count_60m'] == 1
    assert baseline['return_1m_percent'] == pytest.approx((160 / 159 - 1) * 100)
    assert baseline['return_60m_percent'] == pytest.approx(60.0)
    assert baseline['price_path_efficiency_5m'] == 1.0
    assert baseline['price_path_efficiency_15m'] == 1.0
    assert baseline['session_return_percent'] == pytest.approx(
        (160 / 99.9 - 1) * 100
    )


def test_latest_candle_must_be_closed_at_the_state_cutoff():
    features = build_candle_research_features(
        bars=_bars()[:-1],
        state_at=STATE_AT,
        session_key=SESSION_KEY,
        session_start_time=SESSION_START,
    )

    assert features['candle_coverage_60m_ratio'] == 0.0
    assert features['return_1m_percent'] is None
    assert features['opening_range_15m_range_percent'] is None


def test_research_state_identifier_is_stable_and_segmented():
    first = build_research_state_id(
        symbol='aapl',
        session_key=SESSION_KEY,
        state_at=STATE_AT,
    )
    same = build_research_state_id(
        symbol=' AAPL ',
        session_key=SESSION_KEY,
        state_at=STATE_AT,
    )
    different_side_independent_state = build_research_state_id(
        symbol='MSFT',
        session_key=SESSION_KEY,
        state_at=STATE_AT,
    )

    assert first == same
    assert first.startswith('rs1_')
    assert len(first) == 28
    assert different_side_independent_state != first
