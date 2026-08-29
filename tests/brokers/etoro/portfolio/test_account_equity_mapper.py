import pytest

from app.brokers.etoro.account_equity_mapper import extract_account_equity
from app.brokers.etoro.pnl_mapper import extract_account_metrics


def _pnl_payload():
    return {
        'credit': 1000.0,
        'positions': [
            {
                'positionID': 'p1',
                'amount': 200.0,
                'unrealizedPnL': {'pnL': 15.0},
            },
        ],
        'mirrors': [
            {
                'availableAmount': 50.0,
                'closedPositionsNetProfit': 10.0,
                'positions': [
                    {
                        'positionID': 'mp1',
                        'amount': 300.0,
                        'unrealizedPnL': {'pnL': -5.0},
                    },
                ],
            },
        ],
        'ordersForOpen': [
            {
                'amount': 100.0,
                'mirrorID': 0,
                'totalExternalCosts': 5.0,
            },
            {
                'amount': 50.0,
                'mirrorID': 123,
                'totalExternalCosts': 99.0,
            },
        ],
        'orders': [
            {'amount': 20.0},
        ],
    }


def test_extract_account_equity_uses_documented_pnl_components():
    metrics = extract_account_metrics(_pnl_payload())

    assert metrics.credit == 1000.0
    assert metrics.available_cash == 880.0
    assert metrics.total_invested == 665.0
    assert metrics.unrealized_pnl == 20.0
    assert metrics.equity == 1565.0
    assert extract_account_equity(_pnl_payload()) == 1565.0


def test_extract_account_equity_accepts_nested_data_pnl_payload():
    assert extract_account_equity({'data': _pnl_payload()}) == 1565.0


def test_extract_account_equity_rejects_portfolio_credit_shape():
    with pytest.raises(ValueError, match='Unable to locate eToro P&L'):
        extract_account_equity(
            {
                'clientPortfolio': {
                    'positions': [],
                    'mirrors': [],
                    'credit': 99973.89,
                    'orders': [],
                    'bonusCredit': 0.0,
                }
            }
        )


def test_extract_account_equity_rejects_loose_cash_or_balance_aliases():
    for payload in (
        {'equity': 100000.25},
        {'balance': 98765.43},
        {'availableBalance': 12345.67},
        {'credit': 99973.89},
    ):
        with pytest.raises(ValueError, match='Unable to locate eToro P&L'):
            extract_account_equity(payload)


def test_extract_account_equity_raises_when_zero():
    payload = {
        'credit': 0.0,
        'positions': [],
        'mirrors': [],
        'ordersForOpen': [],
        'orders': [],
    }
    with pytest.raises(ValueError, match='Invalid eToro account equity'):
        extract_account_equity(payload)
