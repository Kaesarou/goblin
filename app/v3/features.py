from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from app.instruments.models import AssetClass
from app.market.models import Candle, MarketSnapshot
from app.v3.models import MarketState


class AdjustedEwm:
    """Streaming equivalent of pandas ``ewm(span=..., adjust=True).mean()``."""

    def __init__(self, span: float) -> None:
        if span <= 0:
            raise ValueError("span must be positive")
        self.alpha = 2.0 / (float(span) + 1.0)
        self.decay = 1.0 - self.alpha
        self.numerator = 0.0
        self.denominator = 0.0
        self.value: float | None = None

    def update(self, value: float) -> float:
        current = float(value)
        self.numerator = current + self.decay * self.numerator
        self.denominator = 1.0 + self.decay * self.denominator
        self.value = self.numerator / self.denominator
        return self.value


class UnadjustedEma:
    """Streaming equivalent of pandas ``ewm(span=..., adjust=False).mean()``."""

    def __init__(self, span: float) -> None:
        if span <= 0:
            raise ValueError("span must be positive")
        self.alpha = 2.0 / (float(span) + 1.0)
        self.value: float | None = None

    def update(self, value: float) -> float:
        current = float(value)
        if self.value is None:
            self.value = current
        else:
            self.value = self.alpha * current + (1.0 - self.alpha) * self.value
        return self.value


@dataclass(frozen=True)
class OnlineFeatureSnapshot:
    symbol: str
    asof: datetime
    candle_close: float
    ema_lower: float
    ema_upper: float
    volatility_1m: float
    volatility_1h: float
    forager_volatility: float
    activity: float
    ema_readiness: float
    features: Mapping[str, float]

    def market_state(
        self,
        *,
        snapshot: MarketSnapshot,
        entry_allowed: bool,
        quality_ok: bool = True,
    ) -> MarketState:
        if snapshot.symbol.strip().upper() != self.symbol:
            raise ValueError(
                f"Snapshot symbol mismatch: {snapshot.symbol} != {self.symbol}"
            )
        return MarketState(
            symbol=self.symbol,
            asof=self.asof,
            bid=float(snapshot.bid),
            ask=float(snapshot.ask),
            last=float(self.candle_close),
            ema_lower=self.ema_lower,
            ema_upper=self.ema_upper,
            volatility_1m=self.volatility_1m,
            volatility_1h=self.volatility_1h,
            features=dict(self.features),
            quality_ok=quality_ok,
            entry_allowed=entry_allowed,
        )


class SymbolOnlineFeatureState:
    """Causal online feature state matching the Point-M feature definitions.

    Strategy EMA spans are adjust=False. Volatility/activity spans are adjust=True.
    Hourly volatility consumes only completed observed hours, matching the shifted
    hourly aggregate used by Point M. Missing hours are not fabricated.
    """

    def __init__(self, *, symbol: str, asset_class: AssetClass) -> None:
        self.symbol = symbol.strip().upper()
        self.asset_class = asset_class
        spans = (790.0, math.sqrt(790.0 * 1080.0), 1080.0)
        self.strategy_emas = tuple(UnadjustedEma(span) for span in spans)
        self.volatility_1m = AdjustedEwm(604.0)
        self.forager_volatility = AdjustedEwm(2274.0)
        self.activity = AdjustedEwm(310.0)
        self.hourly_volatility = AdjustedEwm(683.0)
        self.current_hour: datetime | None = None
        self.current_hour_high: float | None = None
        self.current_hour_low: float | None = None
        self.completed_hour_volatility = 0.0
        self.closes: deque[float] = deque(maxlen=61)
        self.highs: deque[float] = deque(maxlen=60)
        self.lows: deque[float] = deque(maxlen=60)
        self.last_opened_at: datetime | None = None

    def update(self, candle: Candle) -> OnlineFeatureSnapshot:
        if candle.symbol.strip().upper() != self.symbol:
            raise ValueError(f"Candle symbol mismatch: {candle.symbol} != {self.symbol}")
        if self.last_opened_at is not None and candle.opened_at <= self.last_opened_at:
            raise ValueError(
                f"Non-causal candle order for {self.symbol}: "
                f"{candle.opened_at} <= {self.last_opened_at}"
            )
        self.last_opened_at = candle.opened_at
        self._advance_hour(candle)

        close = float(candle.close)
        high = float(candle.high)
        low = float(candle.low)
        if close <= 0 or high <= 0 or low <= 0:
            raise ValueError(f"Invalid candle price for {self.symbol}")

        log_range = math.log(high / low)
        vol1m = self.volatility_1m.update(log_range)
        forager_vol = self.forager_volatility.update(log_range)
        activity = self.activity.update(float(candle.sample_count))
        ema_values = [ema.update(close) for ema in self.strategy_emas]
        ema_lower = min(ema_values)
        ema_upper = max(ema_values)

        previous_closes = tuple(self.closes)
        returns = {
            horizon: (
                close / previous_closes[-horizon] - 1.0
                if len(previous_closes) >= horizon
                else float("nan")
            )
            for horizon in (5, 15, 30, 60)
        }
        self.closes.append(close)
        self.highs.append(high)
        self.lows.append(low)

        range15 = self._rolling_range(15)
        range60 = self._rolling_range(60)
        ema_ratio = ema_upper / ema_lower - 1.0
        dist_lower = close / ema_lower - 1.0
        ema_readiness = close / (ema_lower * (1.0 - 0.0028)) - 1.0
        session_frac = self._session_fraction(candle.opened_at)
        features = {
            "ema_readiness": ema_readiness,
            "vol1m_604": vol1m,
            "vol1h_683": self.completed_hour_volatility,
            "ret5": returns[5],
            "ret15": returns[15],
            "ret30": returns[30],
            "ret60": returns[60],
            "range15": range15,
            "range60": range60,
            "ema_ratio": ema_ratio,
            "dist_lower": dist_lower,
            "session_frac": session_frac,
        }
        return OnlineFeatureSnapshot(
            symbol=self.symbol,
            asof=candle.closed_at,
            candle_close=close,
            ema_lower=ema_lower,
            ema_upper=ema_upper,
            volatility_1m=vol1m,
            volatility_1h=self.completed_hour_volatility,
            forager_volatility=forager_vol,
            activity=activity,
            ema_readiness=ema_readiness,
            features=features,
        )

    def _advance_hour(self, candle: Candle) -> None:
        hour = candle.opened_at.replace(minute=0, second=0, microsecond=0)
        high = float(candle.high)
        low = float(candle.low)
        if self.current_hour is None:
            self.current_hour = hour
            self.current_hour_high = high
            self.current_hour_low = low
            return
        if hour < self.current_hour:
            raise ValueError(f"Hourly state moved backwards for {self.symbol}")
        if hour > self.current_hour:
            assert self.current_hour_high is not None
            assert self.current_hour_low is not None
            completed_range = math.log(self.current_hour_high / self.current_hour_low)
            self.completed_hour_volatility = self.hourly_volatility.update(completed_range)
            self.current_hour = hour
            self.current_hour_high = high
            self.current_hour_low = low
            return
        self.current_hour_high = max(self.current_hour_high or high, high)
        self.current_hour_low = min(self.current_hour_low or low, low)

    def _rolling_range(self, window: int) -> float:
        if len(self.highs) < window or len(self.lows) < window:
            return float("nan")
        highs = tuple(self.highs)[-window:]
        lows = tuple(self.lows)[-window:]
        return max(highs) / min(lows) - 1.0

    def _session_fraction(self, opened_at: datetime) -> float:
        minutes = opened_at.hour * 60 + opened_at.minute
        if self.asset_class == AssetClass.EQUITY_EU:
            start, duration = 420, 510
        elif self.asset_class == AssetClass.EQUITY_US:
            start, duration = 810, 390
        else:
            return 0.0
        return min(1.0, max(0.0, (minutes - start) / duration))


class OnlineFeatureEngine:
    def __init__(self, asset_class_by_symbol: Mapping[str, AssetClass]) -> None:
        self.states = {
            symbol.strip().upper(): SymbolOnlineFeatureState(
                symbol=symbol,
                asset_class=asset_class,
            )
            for symbol, asset_class in asset_class_by_symbol.items()
        }

    def update(self, candle: Candle) -> OnlineFeatureSnapshot:
        symbol = candle.symbol.strip().upper()
        try:
            state = self.states[symbol]
        except KeyError as exc:
            raise KeyError(f"No V3 feature state for {symbol}") from exc
        return state.update(candle)
