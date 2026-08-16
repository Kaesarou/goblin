from app.brokers.cached_broker import CachedBrokerClient
from app.brokers.etoro.market_data_client import EtoroRestMarketDataClient
from app.brokers.etoro.resilient_client import ResilientEtoroClient
from app.brokers.etoro.websocket_feed import EtoroWebSocketMarketDataFeed
from app.brokers.paper.paper_broker import PaperBrokerClient
from app.config.settings import Settings
from app.runtime.factories import build_runtime_clients


def _settings(tmp_path, broker: str) -> Settings:
    return Settings(
        BROKER=broker,
        ETORO_API_KEY='api-key',
        ETORO_USER_KEY='user-key',
        ETORO_INSTRUMENT_ID_CACHE_PATH=str(tmp_path / 'instrument-ids.json'),
    )


def test_paper_uses_etoro_websocket_with_execution_only_paper_broker(tmp_path):
    clients = build_runtime_clients(_settings(tmp_path, 'paper'))

    assert isinstance(clients.live_market_data, EtoroWebSocketMarketDataFeed)
    assert isinstance(clients.rest_market_data, EtoroRestMarketDataClient)
    assert isinstance(clients.execution_broker, CachedBrokerClient)
    assert isinstance(clients.execution_broker.delegate, PaperBrokerClient)
    assert clients.live_market_data.rest_client is clients.rest_market_data
    assert clients.live_market_data.payload_observer is None


def test_optional_payload_observer_is_only_installed_when_injected(tmp_path):
    observed = []

    def observer(*args):
        observed.append(args)

    clients = build_runtime_clients(
        _settings(tmp_path, 'paper'),
        websocket_payload_observer=observer,
    )

    assert clients.live_market_data.payload_observer is observer
    assert observed == []


def test_demo_shares_resolved_instrument_mapping_with_execution(tmp_path):
    clients = build_runtime_clients(_settings(tmp_path, 'etoro_demo'))

    assert isinstance(clients.live_market_data, EtoroWebSocketMarketDataFeed)
    assert isinstance(clients.rest_market_data, EtoroRestMarketDataClient)
    assert isinstance(clients.execution_broker, CachedBrokerClient)
    execution = clients.execution_broker.delegate
    assert isinstance(execution, ResilientEtoroClient)
    assert (
        execution.instrument_ids_by_symbol
        is clients.rest_market_data.instrument_ids_by_symbol
    )
    assert (
        execution.symbol_by_instrument_id
        is clients.rest_market_data.symbol_by_instrument_id
    )
