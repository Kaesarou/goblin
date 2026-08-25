from app.brokers.etoro.etoro_client import EtoroClient
from app.config.settings import Settings


def _client():
    return EtoroClient(
        settings=Settings(
            BROKER='etoro_demo',
            ETORO_API_KEY='api-key',
            ETORO_USER_KEY='user-key',
        )
    )


def test_etoro_partial_close_submits_units_to_deduct(monkeypatch):
    client = _client()
    client.position_instruments['9001'] = 100000
    captured = {}

    def fake_post(path, payload):
        captured['path'] = path
        captured['payload'] = payload
        return {
            'orderForClose': {
                'positionID': 9001,
                'instrumentID': 100000,
                'orderID': 362447424,
                'statusID': 1,
            },
            'referenceId': 'close-ref',
        }

    monkeypatch.setattr(client, '_post', fake_post)

    submission = client.close_position('9001', units_to_deduct=0.84)

    assert captured == {
        'path': (
            '/api/v1/trading/execution/demo/'
            'market-close-orders/positions/9001'
        ),
        'payload': {'InstrumentId': 100000, 'UnitsToDeduct': 0.84},
    }
    assert submission.close_order_id == '362447424'
    assert '9001' in client.position_instruments
