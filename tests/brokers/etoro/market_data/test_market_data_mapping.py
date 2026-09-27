import pytest

from app.brokers.etoro.market_data_mapper import to_market_snapshots
from app.market.models import PriceSource

# REST contract (distinct from the capitalized WebSocket fields):
# https://api-portal.etoro.com/api-reference/market-data/get-instrument-market-rates


def test_etoro_market_data_maps_multiple_rates_by_cached_instrument_id():
    snapshots = to_market_snapshots(
        rates_payload={
            'rates': [
                {
                    'instrumentID': 1001,
                    'bid': 199.5,
                    'ask': 200.5,
                    'lastExecution': 200.25,
                },
                {
                    'instrumentID': 1002,
                    'bid': 349.0,
                    'ask': 351.0,
                    'lastExecution': 350.5,
                },
            ],
        },
        symbol_by_instrument_id={
            1001: 'AAPL',
            1002: 'MSFT',
        },
    )

    assert set(snapshots) == {'AAPL', 'MSFT'}
    assert snapshots['AAPL'].symbol == 'AAPL'
    assert snapshots['AAPL'].bid == 199.5
    assert snapshots['AAPL'].ask == 200.5
    assert snapshots['AAPL'].last == 200.25
    assert snapshots['AAPL'].price_source == PriceSource.BROKER_LAST
    assert snapshots['MSFT'].symbol == 'MSFT'
    assert snapshots['MSFT'].bid == 349.0
    assert snapshots['MSFT'].ask == 351.0
    assert snapshots['MSFT'].last == 350.5


def test_etoro_market_data_uses_mid_price_when_last_is_missing():
    snapshots = to_market_snapshots(
        rates_payload={
            'rates': [
                {
                    'instrumentID': 1001,
                    'bid': 199.0,
                    'ask': 201.0,
                },
            ],
        },
        symbol_by_instrument_id={1001: 'AAPL'},
    )

    assert snapshots['AAPL'].last == 200.0
    assert snapshots['AAPL'].price_source == PriceSource.BID_ASK_MIDPOINT


def test_etoro_market_data_requires_documented_rate_field_names():
    with pytest.raises(ValueError, match='required int'):
        to_market_snapshots(
            rates_payload={
                'rates': [
                    {
                        'instrumentId': 1001,
                        'bidPrice': '199.5',
                        'askPrice': '200.5',
                        'lastPrice': '200.25',
                    },
                ],
            },
            symbol_by_instrument_id={1001: 'AAPL'},
        )


def test_etoro_market_data_raises_when_instrument_symbol_is_not_cached():
    with pytest.raises(ValueError, match='Unable to find cached symbol'):
        to_market_snapshots(
            rates_payload={
                'rates': [
                    {
                        'instrumentID': 9999,
                        'bid': 199.0,
                        'ask': 201.0,
                    },
                ],
            },
            symbol_by_instrument_id={},
        )


def test_etoro_market_data_raises_when_required_bid_is_missing():
    with pytest.raises(ValueError, match='Unable to extract required float'):
        to_market_snapshots(
            rates_payload={
                'rates': [
                    {
                        'instrumentID': 1001,
                        'ask': 201.0,
                    },
                ],
            },
            symbol_by_instrument_id={1001: 'AAPL'},
        )
