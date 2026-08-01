from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

EXECUTABLE_PRICE_CONTRACT_VERSION = 'side_aware_executable_prices_v2'


class PriceSource(StrEnum):
    BROKER_LAST = 'broker_last'
    BID_ASK_MIDPOINT = 'bid_ask_midpoint'


class TimestampSource(StrEnum):
    BROKER = 'broker'
    LOCAL_RECEIVE_TIME = 'local_receive_time'


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    bid: float
    ask: float
    last: float
    timestamp: datetime
    received_at: datetime | None = None
    price_source: PriceSource = PriceSource.BROKER_LAST
    timestamp_source: TimestampSource = TimestampSource.BROKER

    def __post_init__(self) -> None:
        timestamp = _as_utc(self.timestamp)
        received_at = _as_utc(self.received_at or timestamp)
        object.__setattr__(self, 'timestamp', timestamp)
        object.__setattr__(self, 'received_at', received_at)
        object.__setattr__(self, 'symbol', self.symbol.strip().upper())

    def executable_exit_price(self, side: str) -> float:
        normalized_side = side.strip().upper()
        if normalized_side == 'BUY':
            price = self.bid
        elif normalized_side == 'SELL':
            price = self.ask
        else:
            raise ValueError(f'Unsupported position side: {side}')
        if price <= 0:
            raise ValueError(
                f'Invalid executable exit price for {normalized_side}: {price}'
            )
        return price

    def executable_entry_price(self, side: str) -> float:
        normalized_side = side.strip().upper()
        if normalized_side == 'BUY':
            price = self.ask
        elif normalized_side == 'SELL':
            price = self.bid
        else:
            raise ValueError(f'Unsupported position side: {side}')
        if price <= 0:
            raise ValueError(
                f'Invalid executable entry price for {normalized_side}: {price}'
            )
        return price

    @classmethod
    def now(
        cls,
        symbol: str,
        bid: float,
        ask: float,
        last: float,
        *,
        price_source: PriceSource = PriceSource.BROKER_LAST,
        timestamp_source: TimestampSource = TimestampSource.LOCAL_RECEIVE_TIME,
    ) -> 'MarketSnapshot':
        now = datetime.now(timezone.utc)
        return cls(
            symbol=symbol,
            bid=bid,
            ask=ask,
            last=last,
            timestamp=now,
            received_at=now,
            price_source=price_source,
            timestamp_source=timestamp_source,
        )


@dataclass(frozen=True)
class Candle:
    symbol: str
    timeframe_seconds: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    opened_at: datetime
    closed_at: datetime
    sample_count: int = 0
    carried_forward: bool = False
    source_price_age_seconds: float | None = None
    quality_degraded: bool = False


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
