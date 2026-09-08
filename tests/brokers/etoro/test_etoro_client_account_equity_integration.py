import pytest

from app.brokers.etoro.etoro_client import EtoroClient
from app.config.settings import Settings


def build_client(broker: str) -> EtoroClient:
    return EtoroClient(
        settings=Settings(
            BROKER=broker,
            ETORO_API_KEY='test-api-key',
            ETORO_USER_KEY='test-user-key',
        )
    )


@pytest.mark.parametrize(
    ("broker", "expected_path"),
    [
        ("etoro_demo", "/api/v1/trading/info/demo/aggregate-portfolio"),
        ("etoro_live", "/api/v1/trading/info/aggregate-portfolio"),
    ],
)
def test_etoro_client_get_account_equity_uses_environment_aggregate_portfolio(
    monkeypatch,
    broker,
    expected_path,
):
    client = build_client(broker)
    paths = []

    def get(path):
        paths.append(path)
        return {"accountTotals": {"accountTotalValue": 43210.0}}

    monkeypatch.setattr(client, "_get", get)
    assert client.get_account_equity() == 43210.0
    assert paths == [expected_path]
