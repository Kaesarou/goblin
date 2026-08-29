import pytest

from app.brokers.etoro.etoro_client import EtoroClient
from app.config.settings import Settings


def build_settings(broker: str = 'etoro_demo') -> Settings:
    return Settings(
        BROKER=broker,
        ETORO_API_KEY='api-key',
        ETORO_USER_KEY='user-key',
    )


def build_filled_order_details(position_id: int = 9001) -> dict:
    return {
        'status': {'id': 1, 'name': 'Executed', 'errorCode': 0},
        'positionExecutions': [
            {
                'positionId': position_id,
                'state': 'open',
                'investedAmountCurrency': 5.0,
                'openingData': {'avgPrice': 238.0, 'units': 0.021},
            }
        ],
    }


def test_find_instrument_id_uses_exact_symbol_match(monkeypatch):
    client = EtoroClient(settings=build_settings())
    monkeypatch.setattr(
        client,
        '_get',
        lambda path, params=None: {
            'items': [
                {
                    'internalSymbolFull': 'BTCA',
                    'internalInstrumentId': 100134,
                },
                {
                    'internalSymbolFull': 'BTC',
                    'internalInstrumentId': 100000,
                },
            ]
        },
    )

    assert client._find_instrument_id('BTC') == 100000


def test_etoro_headers_include_required_keys():
    headers = EtoroClient(settings=build_settings()).headers

    assert headers['x-api-key'] == 'api-key'
    assert headers['x-user-key'] == 'user-key'
    assert 'x-request-id' in headers
    assert headers['Content-Type'] == 'application/json'
    assert headers['Accept'] == 'application/json'


@pytest.mark.parametrize(
    ('broker', 'expected_path'),
    [
        ('etoro_demo', '/api/v1/trading/info/demo/portfolio'),
        ('etoro_live', '/api/v1/trading/info/portfolio'),
    ],
)
def test_get_portfolio_uses_execution_environment(
    broker,
    expected_path,
    monkeypatch,
):
    client = EtoroClient(settings=build_settings(broker))
    captured = {}

    def fake_get(path, params=None):
        captured['path'] = path
        captured['params'] = params
        return {'clientPortfolio': {'positions': []}}

    monkeypatch.setattr(client, '_get', fake_get)

    assert client.get_portfolio() == {'clientPortfolio': {'positions': []}}
    assert captured == {'path': expected_path, 'params': None}


@pytest.mark.parametrize(
    ('broker', 'expected_path'),
    [
        ('etoro_demo', '/api/v2/trading/info/demo/orders:lookup'),
        ('etoro_live', '/api/v2/trading/info/orders:lookup'),
    ],
)
def test_get_order_details_uses_execution_environment(
    broker,
    expected_path,
    monkeypatch,
):
    client = EtoroClient(settings=build_settings(broker))
    captured = {}

    def fake_get(path, params=None):
        captured['path'] = path
        captured['params'] = params
        return {'orderId': 123}

    monkeypatch.setattr(client, '_get', fake_get)

    assert client.get_order_details('123') == {'orderId': 123}
    assert captured == {
        'path': expected_path,
        'params': {'orderId': '123'},
    }


def test_etoro_open_position_sends_demo_buy_order(monkeypatch):
    client = EtoroClient(settings=build_settings())
    captured = {}
    monkeypatch.setattr(client, '_find_instrument_id', lambda symbol: 100000)

    def fake_post(path, payload):
        captured['path'] = path
        captured['payload'] = payload
        return {'orderId': 362406474, 'referenceId': 'ref-1'}

    monkeypatch.setattr(client, '_post', fake_post)
    monkeypatch.setattr(
        client,
        '_wait_for_executed_order',
        lambda order_id, require_position_details=True: (
            build_filled_order_details()
        ),
    )

    result = client.open_position(
        symbol='BTC',
        side='BUY',
        amount=5.0,
        stop_loss=60000.0,
        take_profit=62000.0,
    )

    assert result.position_id == '9001'
    assert result.executed_entry_price == 238.0
    assert result.executed_units == pytest.approx(0.021)
    assert result.executed_notional == pytest.approx(5.0)
    assert captured == {
        'path': '/api/v2/trading/execution/demo/orders',
        'payload': {
            'action': 'open',
            'transaction': 'buy',
            'InstrumentID': 100000,
            'orderType': 'mkt',
            'leverage': 1,
            'amount': 5.0,
            'orderCurrency': 'usd',
        },
    }
    assert client.position_instruments['9001'] == 100000


def test_etoro_open_position_uses_fixed_sell_safety_stop(monkeypatch):
    client = EtoroClient(settings=build_settings())
    captured = {}
    monkeypatch.setattr(client, '_find_instrument_id', lambda symbol: 1261)

    def fake_post(path, payload):
        captured['path'] = path
        captured['payload'] = payload
        return {'orderId': 362406475, 'referenceId': 'ref-2'}

    monkeypatch.setattr(client, '_post', fake_post)
    monkeypatch.setattr(
        client,
        '_wait_for_executed_order',
        lambda order_id, require_position_details=True: (
            build_filled_order_details(position_id=9002)
        ),
    )

    result = client.open_position(
        symbol='HO.PA',
        side='SELL',
        amount=497.26,
        stop_loss=337.4092,
        take_profit=334.8862,
    )

    assert result.position_id == '9002'
    assert result.executed_units == pytest.approx(0.021)
    assert captured['payload']['transaction'] == 'sellShort'
    assert captured['payload']['StopLossRate'] == 338.42143
    assert 'TakeProfitRate' not in captured['payload']


def test_etoro_open_position_rejects_multiple_executions(monkeypatch):
    client = EtoroClient(settings=build_settings())
    monkeypatch.setattr(client, '_find_instrument_id', lambda symbol: 100000)
    monkeypatch.setattr(
        client,
        '_post',
        lambda path, payload: {
            'orderId': 362406474,
            'referenceId': 'ref-1',
        },
    )
    monkeypatch.setattr(
        client,
        '_wait_for_executed_order',
        lambda *args, **kwargs: {
            'status': {'id': 1, 'name': 'Executed', 'errorCode': 0},
            'positionExecutions': [
                {
                    'positionId': 9001,
                    'openingData': {'avgPrice': 238.0, 'units': 1.0},
                },
                {
                    'positionId': 9002,
                    'openingData': {'avgPrice': 239.0, 'units': 1.0},
                },
            ],
        },
    )

    with pytest.raises(RuntimeError, match='unsupported position execution count'):
        client.open_position(
            symbol='BTC',
            side='BUY',
            amount=5.0,
            stop_loss=60000.0,
            take_profit=62000.0,
        )


def test_etoro_close_submits_once_and_keeps_metadata_until_confirmation(
    monkeypatch,
):
    client = EtoroClient(settings=build_settings())
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
    monkeypatch.setattr(
        client,
        '_wait_for_executed_order',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('close submission must not poll')
        ),
    )

    submission = client.close_position('9001')

    assert captured == {
        'path': (
            '/api/v1/trading/execution/demo/'
            'market-close-orders/positions/9001'
        ),
        'payload': {'InstrumentId': 100000, 'UnitsToDeduct': None},
    }
    assert submission.close_order_id == '362447424'
    assert submission.reference_id == 'close-ref'
    assert '9001' in client.position_instruments
