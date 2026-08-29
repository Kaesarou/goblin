from app.brokers.cached_broker import CachedBrokerClient
from app.brokers.etoro.resilient_client import ResilientEtoroClient
from app.config.settings import Settings
from app.runtime.factories import build_runtime_clients


def test_execution_and_rest_market_data_share_one_user_key_get_governor(tmp_path):
    settings = Settings(
        BROKER="etoro_demo",
        ETORO_API_KEY="api-key",
        ETORO_USER_KEY="user-key",
        ETORO_INSTRUMENT_ID_CACHE_PATH=str(tmp_path / "instrument_ids.json"),
    )

    clients = build_runtime_clients(settings)

    assert isinstance(clients.execution_broker, CachedBrokerClient)
    execution = clients.execution_broker.delegate
    assert isinstance(execution, ResilientEtoroClient)
    assert execution._get_rate_governor is clients.rest_market_data._get_rate_governor
