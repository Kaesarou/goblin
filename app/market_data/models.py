from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.market.models import Candle, MarketSnapshot
from app.research.payload_schema_observer import (
    WebSocketPayloadSchemaSample,
)

MARKET_DATA_MODEL_VERSION = 'market_data_v2_ws_clocked_2'


class MarketDataSource(StrEnum):
    WEBSOCKET = 'websocket'
    REST_CONTROL = 'rest_control'
    REST_FALLBACK = 'rest_fallback'


class SymbolFeedState(StrEnum):
    STARTING = 'starting'
    WS_HEALTHY = 'ws_healthy'
    WS_STALE = 'ws_stale'
    REST_FALLBACK = 'rest_fallback'
    RECOVERING = 'recovering'
    BLOCKED = 'blocked'


@dataclass(frozen=True)
class MarketDataEvent:
    symbol: str
    source: MarketDataSource
    received_at: datetime
    snapshot: MarketSnapshot | None
    instrument_id: int | None = None
    message_id: str | None = None
    price_rate_id: str | None = None
    connection_id: str | None = None
    price_changed: bool = True
    state_reconstructed: bool = False
    payload_schema: WebSocketPayloadSchemaSample | None = None


@dataclass(frozen=True)
class MarketDataDecision:
    accepted: bool
    reason: str
    event: MarketDataEvent
    entry_allowed: bool


@dataclass(frozen=True)
class CandleQuality:
    source: str
    message_count: int
    price_event_count: int
    fallback_event_count: int
    out_of_order_drop_count: int
    degraded: bool
    carried_forward: bool = False
    last_price_age_seconds: float | None = None
    ordering_drop_ratio: float = 0.0
    degraded_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandleBuildResult:
    candle: Candle
    quality: CandleQuality
