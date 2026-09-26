"""No eToro network calls: account-currency fill reconstruction from P&L."""

import pytest

from app.brokers.etoro.pnl_position_amount import position_amount_usd
from app.brokers.etoro.resilient_client import ResilientEtoroClient
from app.config.settings import Settings


def _client(monkeypatch, *, instrument_price=100.0, units=3.0,
            order_notional=1.0, pnl=None):
    client = ResilientEtoroClient(settings=Settings.model_construct(
        broker="etoro_demo", base_currency="USD",
        etoro_api_key="api", etoro_user_key="user",
    ))
    calls = []
    monkeypatch.setattr(client, "_find_instrument_id", lambda _symbol: 100)
    monkeypatch.setattr(client, "_build_open_order_payload", lambda **_kwargs: {})
    monkeypatch.setattr(client, "_post", lambda _path, _payload: {
        "orderId": "order-1", "referenceId": "reference-1",
    })
    monkeypatch.setattr(client, "_wait_for_executed_order", lambda *_args, **_kwargs: {
        "status": {"name": "Executed", "errorCode": 0},
        "positionExecutions": [{
            "positionId": "position-1", "investedAmountCurrency": order_notional,
            "openingData": {"avgPrice": instrument_price, "units": units},
        }],
    })
    def get_pnl(path):
        calls.append(path)
        if isinstance(pnl, Exception):
            raise pnl
        return pnl
    monkeypatch.setattr(client, "_get", get_pnl)
    return client, calls


def test_one_dollar_order_field_uses_exact_pnl_position_usd_amount(monkeypatch):
    client, calls = _client(monkeypatch, pnl={"clientPortfolio": {"positions": [
        {"positionID": "different", "amount": 10_000.0},
        {"positionID": "position-1", "amount": 299.0},
    ]}})
    result = client.open_position("INTC", "BUY", 300.0, 50.0, 1_000.0)
    assert result.position_id == "position-1"
    assert result.executed_notional == 299.0
    assert result.executed_units == 3.0
    assert calls == ["/api/v1/trading/info/demo/pnl"]


def test_european_instrument_amount_stays_usd_not_units_times_euro_price(monkeypatch):
    client, _ = _client(monkeypatch, instrument_price=210.0, units=1.4,
                        pnl={"clientPortfolio": {"positions": [
                            {"positionID": "position-1", "amount": 327.0},
                        ]}})
    result = client.open_position("SAP.DE", "BUY", 327.0, 100.0, 2_100.0)
    assert result.executed_notional == 327.0
    assert result.executed_units * result.executed_entry_price == pytest.approx(294.0)


def test_good_broker_notional_requires_no_extra_pnl_get(monkeypatch):
    client, calls = _client(monkeypatch, order_notional=290.0)
    assert client.open_position("INTC", "BUY", 300.0, 50.0, 1_000.0).executed_notional == 290.0
    assert calls == []


@pytest.mark.parametrize("pnl", (
    {"clientPortfolio": {"positions": []}},
    {"clientPortfolio": {"positions": [{"positionID": "position-1", "amount": 1.0}]}},
    {"clientPortfolio": {"positions": [
        {"positionID": "position-1", "amount": 300.0},
        {"positionID": "position-1", "amount": 300.0},
    ]}},
    {"no": "positions"},
    RuntimeError("GET 429"),
))
def test_missing_stale_ambiguous_or_unavailable_pnl_never_hides_confirmed_fill(
    monkeypatch, pnl,
):
    client, _ = _client(monkeypatch, pnl=pnl)
    result = client.open_position("INTC", "BUY", 300.0, 50.0, 1_000.0)
    assert result.position_id == "position-1"
    assert result.executed_notional in (1.0,)
    assert client.position_instruments["position-1"] == 100


def test_pnl_parser_rejects_duplicate_or_invalid_amounts():
    with pytest.raises(ValueError, match="Duplicate"):
        position_amount_usd({"clientPortfolio": {"positions": [
            {"positionID": "p1", "amount": 300},
            {"positionID": "p1", "amount": 300},
        ]}}, "p1")
    with pytest.raises(ValueError, match="amount"):
        position_amount_usd({"clientPortfolio": {"positions": [
            {"positionID": "p1", "amount": True},
        ]}}, "p1")
