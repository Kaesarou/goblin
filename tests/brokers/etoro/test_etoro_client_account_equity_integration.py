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


def test_etoro_client_get_account_equity_uses_pnl_payload(monkeypatch):
    client = build_client()
    monkeypatch.setattr(
        client,
        'get_pnl',
        lambda: {
            'credit': 43000.0,
            'positions': [
                {
                    'amount': 200.0,
                    'unrealizedPnL': {'pnL': 10.0},
                }
            ],
            'mirrors': [],
            'ordersForOpen': [],
            'orders': [],
        },
    )

    assert client.get_account_equity() == 43210.0
