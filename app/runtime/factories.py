from dataclasses import dataclass

from app.brokers.alpaca.client import AlpacaBrokerClient
from app.brokers.alpaca.environment import AlpacaEnvironment
from app.brokers.alpaca.http_client import AlpacaHttpClient
from app.brokers.alpaca.instrument_cache import AlpacaInstrumentCache
from app.brokers.alpaca.market_data import AlpacaMarketDataFeed, AlpacaRestMarketDataClient
from app.brokers.alpaca.order_store import AlpacaOrderStore
from app.brokers.base import BrokerClient
from app.brokers.cached_broker import CachedBrokerClient
from app.brokers.etoro.get_rate_governor import EtoroGetRateGovernor
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
    if settings.broker in {"alpaca_demo", "alpaca_live"}:
        return _build_alpaca_clients(settings)
    if settings.broker not in {"paper", "etoro_demo", "etoro_live"}:
        raise ValueError(f"Unsupported broker: {settings.broker}")
    # Account reads and REST market data share a conservative user-key budget.
    # Documented order lookups have a distinct 45/60s bucket and 429 cooldown.
    get_rate_governor = EtoroGetRateGovernor()
    market_data = EtoroRestMarketDataClient(
        api_key=settings.etoro_api_key,
        user_key=settings.etoro_user_key,
        instrument_id_cache_path=settings.instrument_id_cache_path,
        get_rate_governor=get_rate_governor,
    )
    live_feed = EtoroWebSocketMarketDataFeed(
        api_key=settings.etoro_api_key,
        user_key=settings.etoro_user_key,
        rest_client=market_data,
        queue_capacity=MARKET_DATA_QUEUE_CAPACITY,
        global_silence_seconds=WS_GLOBAL_SILENCE_SECONDS,
        payload_observer=websocket_payload_observer,
    )

    if settings.broker == "paper":
        execution: BrokerClient = PaperBrokerClient()
    else:
        etoro = ResilientEtoroClient(
            settings=settings,
            get_rate_governor=get_rate_governor,
            order_lookup_get_rate_governor=EtoroGetRateGovernor(),
        )
        etoro.instrument_ids_by_symbol = market_data.instrument_ids_by_symbol
        etoro.symbol_by_instrument_id = market_data.symbol_by_instrument_id
        execution = etoro

    return RuntimeClients(
        execution_broker=CachedBrokerClient(execution),
        rest_market_data=market_data,
        live_market_data=live_feed,
    )


def _build_alpaca_clients(settings: Settings) -> RuntimeClients:
    environment = AlpacaEnvironment(settings.broker)
    if settings.base_currency != "USD":
        raise ValueError("Alpaca execution currently requires BASE_CURRENCY=USD")
    trading = AlpacaHttpClient(
        settings.alpaca_api_key, settings.alpaca_secret_key, environment=environment
    )
    data = AlpacaHttpClient(
        settings.alpaca_api_key, settings.alpaca_secret_key, environment=environment, data=True
    )
    instruments = AlpacaInstrumentCache(trading, settings.alpaca_instrument_id_cache_path)
    # This is durable execution state, not the disposable instrument cache.
    # Derive it from the operator-selected V3 state path and broker environment.
    store = AlpacaOrderStore(
        f"{settings.position_store_path}.{environment.value}.orders.sqlite", environment=environment
    )
    execution = AlpacaBrokerClient(
        trading,
        store,
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        instrument_cache=instruments,
    )
    return RuntimeClients(
        execution_broker=CachedBrokerClient(execution),
        rest_market_data=AlpacaRestMarketDataClient(data, feed=settings.alpaca_data_feed),
        live_market_data=AlpacaMarketDataFeed(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            feed=settings.alpaca_data_feed,
            trade_stream=execution.trade_stream,
            queue_capacity=MARKET_DATA_QUEUE_CAPACITY,
            global_silence_seconds=WS_GLOBAL_SILENCE_SECONDS,
        ),
    )
