"""The diagnostic must never perform a broker mutation or reveal credentials."""

import pytest

from scripts.inspect_etoro_notional_readonly import inspect


class DemoBroker:
    env = "demo"

    def __init__(self):
        self.calls = []

    def get_order_details(self, order_id):
        self.calls.append(("order_get", order_id))
        return {"positionExecutions": [{
            "positionId": "p-7", "investedAmountCurrency": 1.0,
            "initialExposureAccountCurrency": 301.0,
            "initialExposureAssetCurrency": 260.0,
            "openingData": {"avgPrice": 130.0, "units": 2.0},
        }]}

    def _get(self, path):
        self.calls.append(("pnl_get", path))
        return {"clientPortfolio": {"positions": [{
            "positionID": "p-7", "amount": 300.0,
        }]}}

    def open_position(self, *_args, **_kwargs):
        raise AssertionError("No POST/open is permitted")

    def close_position(self, *_args, **_kwargs):
        raise AssertionError("No POST/close is permitted")


def test_diagnostic_reads_exact_order_and_pnl_without_mutations():
    broker = DemoBroker()
    result = inspect(broker, "order-7")
    assert result["broker_mutations"] == 0
    assert result["position_executions"][0]["order_invested_amount_currency"] == 1.0
    assert result["position_executions"][0]["pnl_position_amount_usd"] == 300.0
    assert broker.calls == [
        ("order_get", "order-7"),
        ("pnl_get", "/api/v1/trading/info/demo/pnl"),
    ]
    assert "api_key" not in str(result).lower()


def test_diagnostic_rejects_live_broker():
    broker = DemoBroker()
    broker.env = "real"
    with pytest.raises(ValueError, match="DEMO"):
        inspect(broker, "order-7")
    assert broker.calls == []
