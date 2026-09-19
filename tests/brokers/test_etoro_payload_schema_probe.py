"""The recovery payload probe must never submit orders or expose account data."""

from scripts.inspect_etoro_payload_schema_readonly import describe, inspect


class DemoClient:
    env = "demo"

    def __init__(self):
        self.calls = []

    def get_portfolio(self):
        self.calls.append("portfolio_GET")
        return {"clientPortfolio": {"positions": [
            {"positionID": "private-id", "units": 3.1},
        ]}}

    def _get(self, path):
        self.calls.append(path)
        return {"ordersForOpen": [], "orders": [], "positions": [
            {"positionID": "private-id", "amount": 300.0},
        ]}

    def _post(self, *_args, **_kwargs):
        raise AssertionError("payload schema probe must not mutate eToro")


def test_probe_reads_broker_without_exposing_position_identifiers_or_amounts():
    client = DemoClient()
    report = inspect(client)
    assert client.calls == ["portfolio_GET", "/api/v1/trading/info/demo/pnl"]
    assert report["broker_mutations"] == 0
    assert report["pnl_schema"]["root_ordersForOpen_count"] == 0
    assert report["pnl_schema"]["root_orders_count"] == 0
    assert report["portfolio_schema"]["clientPortfolio_positions_count"] == 1
    assert "private-id" not in str(report)
    assert "300.0" not in str(report)


def test_non_dict_payload_is_described_not_mistaken_for_empty():
    assert describe([{"orders": []}]) == {"root_type": "list"}
