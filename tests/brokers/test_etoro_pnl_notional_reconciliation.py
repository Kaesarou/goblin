"""No eToro network calls: account-currency fill reconstruction from P&L."""

import pytest

from app.brokers.etoro.pnl_position_amount import position_amount_usd
from app.brokers.etoro.resilient_client import ResilientEtoroClient
from app.config.settings import Settings

# P&L nests positions under clientPortfolio and uses positionId (not the
# portfolio endpoint's positionID):
# https://api-portal.etoro.com/api-reference/trading--demo/get-account-pnl-and-portfolio-details


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
        {"positionId": "different", "amount": 10_000.0},
        {"positionId": "position-1", "amount": 299.0},
    ]}})
    result = client.open_position("INTC", "BUY", 300.0, 50.0, 1_000.0)
    assert result.position_id == "position-1"
    assert result.executed_notional == 299.0
    assert result.executed_units == 3.0
    assert calls == ["/api/v1/trading/info/demo/pnl"]


def test_european_instrument_amount_stays_usd_not_units_times_euro_price(monkeypatch):
    client, _ = _client(monkeypatch, instrument_price=210.0, units=1.4,
                        pnl={"clientPortfolio": {"positions": [
                            {"positionId": "position-1", "amount": 327.0},
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
    {"clientPortfolio": {"positions": [{"positionId": "position-1", "amount": 1.0}]}},
    {"clientPortfolio": {"positions": [
        {"positionId": "position-1", "amount": 300.0},
        {"positionId": "position-1", "amount": 300.0},
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
            {"positionId": "p1", "amount": 300},
            {"positionId": "p1", "amount": 300},
        ]}}, "p1")
    with pytest.raises(ValueError, match="amount"):
        position_amount_usd({"clientPortfolio": {"positions": [
            {"positionId": "p1", "amount": True},
        ]}}, "p1")


def _exact_position(**changes):
    return {"positionId": "position-1", "instrumentId": 100,
            "isBuy": True, "leverage": 1, "units": 3.0,
            "openRate": 100.01, "amount": 300.0, "orderId": "order-1", **changes}


def test_exact_economics_preserves_asset_price_and_usd_principal_in_one_governed_get(monkeypatch):
    client, calls = _client(monkeypatch, pnl={"clientPortfolio": {"positions": [
        _exact_position(openRate=210.5, units=1.4, amount=327.0),
    ]}})
    client.position_instruments["position-1"] = 100
    result = client.get_open_position_economics(["position-1"])["position-1"]
    assert result.entry_price == 210.5 and result.units == 1.4
    assert result.account_notional == 327.0
    assert result.broker_response["openRate"] == 210.5
    assert calls == ["/api/v1/trading/info/demo/pnl"]


@pytest.mark.parametrize("changes", [
    {"instrumentId": 200}, {"isBuy": False}, {"leverage": 2},
    {"leverage": True}, {"units": None}, {"units": float("nan")},
    {"openRate": True}, {"openRate": float("inf")}, {"amount": -1.0},
])
def test_exact_economics_fails_closed_on_invalid_identity_and_numbers(monkeypatch, changes):
    client, _ = _client(monkeypatch, pnl={"clientPortfolio": {"positions": [
        _exact_position(**changes),
    ]}})
    client.position_instruments["position-1"] = 100
    with pytest.raises(ValueError):
        client.get_open_position_economics(["position-1"])
