import pytest
from app.brokers.etoro.account_equity_mapper import extract_account_equity


def test_aggregate_equity_is_the_documented_total():
    payload = {"accountTotals": {"accountTotalValue": 43210.0}, "credit": 99999}
    assert extract_account_equity(payload) == 43210.0
    assert extract_account_equity({"data": payload}) == 43210.0


@pytest.mark.parametrize("value", [None, True, False, "100", "bad", [], {}, 0, -1, float("nan"), float("inf"), -float("inf")])
def test_invalid_equity_fails_closed(value):
    with pytest.raises(ValueError):
        extract_account_equity({"accountTotals": {"accountTotalValue": value}})


@pytest.mark.parametrize("payload", [None, {}, {"credit": 1000}, {"cash": 1000}, {"equity": 1000},
    {"clientPortfolio": {"credit": 1000, "positions": []}},
    {"credit": 1000, "ordersForOpen": [], "positions": []},
    {"accountTotals": {"accountAvailableCash": 100, "accountTotalUsedMargin": 200, "accountCurrentPnl": 10}},
    {"accountTotals": None}])
def test_no_liquidity_portfolio_pnl_or_component_fallback(payload):
    with pytest.raises(ValueError):
        extract_account_equity(payload)
