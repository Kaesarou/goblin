from datetime import UTC, datetime, timedelta, timezone

from app.market.models import Candle, MarketSnapshot
from app.market.timeframes import BASE_TIMEFRAME


class CandleBuilder:
    """Build the canonical fixed M1 candle stream from accepted snapshots."""

    def __init__(self) -> None:
        self.timeframe_seconds = BASE_TIMEFRAME.value
        self._current_bucket_start: datetime | None = None
        self._prices: list[float] = []
        self._symbol: str | None = None
        self._real_sample_count = 0
        self._carried_forward = False
        self._last_real_timestamp: datetime | None = None
        self._last_closed_carried_forward = False
        self._last_closed_price_age_seconds: float | None = None

    def reset(self) -> None:
        self._current_bucket_start = None
        self._prices = []
        self._symbol = None
        self._real_sample_count = 0
        self._carried_forward = False
        self._last_real_timestamp = None
        self._last_closed_carried_forward = False
        self._last_closed_price_age_seconds = None

    @property
    def current_bucket_start(self) -> datetime | None:
        return self._current_bucket_start

    @property
    def last_price(self) -> float | None:
        return self._prices[-1] if self._prices else None

    def take_last_closed_metadata(self) -> tuple[bool, float | None]:
        result = (
            self._last_closed_carried_forward,
            self._last_closed_price_age_seconds,
        )
        self._last_closed_carried_forward = False
        self._last_closed_price_age_seconds = None
        return result

    def on_snapshot(self, snapshot: MarketSnapshot) -> Candle | None:
        bucket_start = self._bucket_start(snapshot.timestamp)
        snapshot_timestamp = self._as_utc(snapshot.timestamp)

        if self._current_bucket_start is None:
            self._last_real_timestamp = snapshot_timestamp
            self._start_new_bucket(snapshot, bucket_start)
            return None

        if bucket_start < self._current_bucket_start:
            return None

        if bucket_start == self._current_bucket_start:
            self._last_real_timestamp = snapshot_timestamp
            self._prices.append(snapshot.last)
            self._real_sample_count += 1
            return None

        closed_candle = self._close_current_bucket()
        self._last_closed_carried_forward = self._carried_forward
        self._last_closed_price_age_seconds = self._price_age_at(
            closed_candle.closed_at
        )
        self._last_real_timestamp = snapshot_timestamp
        self._start_new_bucket(snapshot, bucket_start)
        return closed_candle

    def finalize_until(
        self,
        now: datetime,
        *,
        grace_seconds: float = 1.0,
    ) -> list[tuple[Candle, bool, float | None]]:
        """Close every elapsed M1 bucket without waiting for the next tick.

        A new bucket is seeded with the previous close so higher timeframes keep
        deterministic boundaries. Its sample_count remains zero until a real
        broker update arrives.
        """
        if self._current_bucket_start is None or not self._prices:
            return []
        cutoff = self._as_utc(now) - timedelta(
            seconds=max(0.0, grace_seconds)
        )
        closed: list[tuple[Candle, bool, float | None]] = []
        while (
            self._current_bucket_start
            + timedelta(seconds=self.timeframe_seconds)
            <= cutoff
        ):
            candle = self._close_current_bucket()
            carried = self._carried_forward
            age = self._price_age_at(candle.closed_at)
            last_price = candle.close
            symbol = candle.symbol
            next_bucket = candle.closed_at
            closed.append((candle, carried, age))
            self._start_carried_bucket(
                symbol=symbol,
                bucket_start=next_bucket,
                price=last_price,
            )
        return closed

    def _start_new_bucket(
        self,
        snapshot: MarketSnapshot,
        bucket_start: datetime,
    ) -> None:
        self._current_bucket_start = bucket_start
        self._symbol = snapshot.symbol
        self._prices = [snapshot.last]
        self._real_sample_count = 1
        self._carried_forward = False

    def _start_carried_bucket(
        self,
        *,
        symbol: str,
        bucket_start: datetime,
        price: float,
    ) -> None:
        self._current_bucket_start = bucket_start
        self._symbol = symbol
        self._prices = [price]
        self._real_sample_count = 0
        self._carried_forward = True

    def _close_current_bucket(self) -> Candle:
        if self._current_bucket_start is None:
            raise RuntimeError('Cannot close candle without current bucket')
        if self._symbol is None:
            raise RuntimeError('Cannot close candle without symbol')
        if not self._prices:
            raise RuntimeError('Cannot close candle without prices')
        return Candle(
            symbol=self._symbol,
            timeframe_seconds=self.timeframe_seconds,
            open=self._prices[0],
            high=max(self._prices),
            low=min(self._prices),
            close=self._prices[-1],
            volume=None,
            opened_at=self._current_bucket_start,
            closed_at=self._current_bucket_start
            + timedelta(seconds=self.timeframe_seconds),
            sample_count=self._real_sample_count,
        )

    def _price_age_at(self, value: datetime) -> float | None:
        if self._last_real_timestamp is None:
            return None
        return max(
            0.0,
            (self._as_utc(value) - self._last_real_timestamp).total_seconds(),
        )

    def _bucket_start(self, timestamp: datetime) -> datetime:
        actual = self._as_utc(timestamp)
        epoch_seconds = int(actual.timestamp())
        bucket_epoch = epoch_seconds - (
            epoch_seconds % self.timeframe_seconds
        )
        return datetime.fromtimestamp(bucket_epoch, tz=UTC)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
