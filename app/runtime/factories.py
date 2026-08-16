from dataclasses import dataclass

from app.brokers.base import BrokerClient
from app.brokers.cached_broker import CachedBrokerClient
from app.brokers.etoro.market_data_client import EtoroRestMarketDataClient
from app.brokers.etoro.resilient_client import ResilientEtoroClient
from app.brokers.etoro.websocket_feed import EtoroWebSocketMarketDataFeed
from app.brokers.etoro.websocket_protocol import WebSocketPayloadObserver
from app.brokers.paper.paper_broker import PaperBrokerClient
from app.config.settings import Settings
from app.market_data.contracts import LiveMarketDataFeed, RestMarketDataClient
from app.runtime.runtime_policy import (
    MARKET_DATA_QUEUE_CAPACITY,
    WS_GLOBAL_SILENCE_SECONDS,
)


@dataclass(frozen=True)
class RuntimeClients:
    execution_broker: BrokerClient
    rest_market_data: RestMarketDataClient
    live_market_data: LiveMarketDataFeed


def build_runtime_clients(
    settings: Settings,
    *,
    websocket_payload_observer: WebSocketPayloadObserver | None = None,
) -> RuntimeClients:
    market_data = EtoroRestMarketDataClient(
        api_key=settings.etoro_api_key,
        user_key=settings.etoro_user_key,
        instrument_id_cache_path=settings.instrument_id_cache_path,
    )
    live_feed = EtoroWebSocketMarketDataFeed(
        api_key=settings.etoro_api_key,
        user_key=settings.etoro_user_key,
        rest_client=market_data,
        queue_capacity=MARKET_DATA_QUEUE_CAPACITY,
        global_silence_seconds=WS_GLOBAL_SILENCE_SECONDS,
        payload_observer=websocket_payload_observer,
    )

    if settings.broker == 'paper':
        execution: BrokerClient = PaperBrokerClient()
    else:
        etoro = ResilientEtoroClient(settings=settings)
        etoro.instrument_ids_by_symbol = market_data.instrument_ids_by_symbol
        etoro.symbol_by_instrument_id = market_data.symbol_by_instrument_id
        execution = etoro

    return RuntimeClients(
        execution_broker=CachedBrokerClient(execution),
        rest_market_data=market_data,
        live_market_data=live_feed,
    )
