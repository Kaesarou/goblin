from app.brokers.etoro.etoro_client import EtoroClient
from app.config.settings import Settings


def build_client() -> EtoroClient:
    return EtoroClient(
        settings=Settings(
            BROKER='etoro_demo',
            ETORO_API_KEY='test-api-key',
            ETORO_USER_KEY='test-user-key',
        )
    )


def test_etoro_client_get_account_equity_uses_aggregate_portfolio(monkeypatch):
    client = build_client()
    paths = []
    def get(path):
        paths.append(path)
        return {"accountTotals": {"accountTotalValue": 43210.0}}
    monkeypatch.setattr(client, "_get", get)
    assert client.get_account_equity() == 43210.0
    assert paths == ["/api/v1/trading/info/aggregate-portfolio"]
