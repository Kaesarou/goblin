from datetime import datetime, timedelta, timezone

import pandas as pd

from app.instruments.models import AssetClass
from app.market.models import Candle
from app.v3.features import AdjustedEwm, SymbolOnlineFeatureState, UnadjustedEma

UTC = timezone.utc


def test_streaming_ewm_matches_pandas_for_both_adjust_modes():
    values = [1.0, 3.0, 2.0, 5.0, 4.0]
    expected_adjusted = pd.Series(values).ewm(span=4, adjust=True, min_periods=1).mean()
    expected_unadjusted = pd.Series(values).ewm(span=4, adjust=False, min_periods=1).mean()
    adjusted = AdjustedEwm(4)
    unadjusted = UnadjustedEma(4)
    actual_adjusted = [adjusted.update(value) for value in values]
    actual_unadjusted = [unadjusted.update(value) for value in values]
    for actual, expected in zip(actual_adjusted, expected_adjusted, strict=True):
        assert abs(actual - expected) < 1e-12
    for actual, expected in zip(actual_unadjusted, expected_unadjusted, strict=True):
        assert abs(actual - expected) < 1e-12


def _candle(minute: int, *, price: float, high: float | None = None, low: float | None = None):
    opened = datetime(2026, 8, 25, 7, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return Candle(
        symbol="AIR.PA",
        timeframe_seconds=60,
        open=price,
        high=high if high is not None else price * 1.001,
        low=low if low is not None else price * 0.999,
        close=price,
        volume=None,
        opened_at=opened,
        closed_at=opened + timedelta(minutes=1),
        sample_count=6,
    )


def test_online_features_are_causal_and_recoverability_warms_progressively():
    state = SymbolOnlineFeatureState(symbol="AIR.PA", asset_class=AssetClass.EQUITY_EU)
    first = state.update(_candle(0, price=100))
    assert first.ema_lower == 100
    assert first.features["session_frac"] == 0.0
    assert pd.isna(first.features["ret5"])
    latest = first
    for minute in range(1, 61):
        latest = state.update(_candle(minute, price=100 + minute / 100))
    assert not pd.isna(latest.features["ret60"])
    assert not pd.isna(latest.features["range60"])


def test_hourly_volatility_uses_only_completed_previous_hour():
    state = SymbolOnlineFeatureState(symbol="AIR.PA", asset_class=AssetClass.EQUITY_EU)
    first = state.update(_candle(0, price=100, high=101, low=99))
    assert first.volatility_1h == 0.0
    for minute in range(1, 60):
        state.update(_candle(minute, price=100, high=101, low=99))
    next_hour = state.update(_candle(60, price=100, high=100.5, low=99.5))
    expected = __import__("math").log(101 / 99)
    assert abs(next_hour.volatility_1h - expected) < 1e-12
