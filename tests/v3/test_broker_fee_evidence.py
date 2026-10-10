import pytest

from app.v3.broker_fee_evidence import broker_order_cost_evidence


def test_etoro_order_cost_is_broker_reported_but_not_a_commission_only():
    evidence = broker_order_cost_evidence({
        "orderCurrency": "usd",
        "totalCosts": 1.0,
        "positionExecutions": [{"openingData": {"fees": 1}}],
    })
    assert evidence == {
        "status": "broker_order_reported",
        "amount": 1.0,
        "currency": "USD",
        "source": "etoro.order_info.totalCosts",
        "scope": "total_order_costs_not_commission_only",
        "ledger_applied": False,
    }


def test_zero_is_only_reported_when_the_broker_explicitly_provides_it():
    zero = broker_order_cost_evidence({"orderCurrency": "USD", "totalCosts": 0})
    assert zero["status"] == "broker_order_reported"
    assert zero["amount"] == 0

    unknown = broker_order_cost_evidence({"orderCurrency": "USD"})
    assert unknown["status"] == "unavailable"
    assert unknown["amount"] is None


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True, None, "invalid"])
def test_bad_cost_evidence_cannot_be_interpreted_as_real_fee(bad):
    evidence = broker_order_cost_evidence({
        "orderCurrency": "USD", "totalCosts": bad,
    })
    assert evidence["status"] == "unavailable"
    assert evidence["amount"] is None
    assert not evidence["ledger_applied"]


def test_alpaca_optional_commission_is_not_mislabeled_total_cost():
    evidence = broker_order_cost_evidence({
        "asset_class": "us_equity", "commission": "0",
    })
    assert evidence["status"] == "broker_order_reported"
    assert evidence["amount"] == 0
    assert evidence["currency"] == "USD"
    assert evidence["scope"] == "optional_end_user_commission_not_total_friction"
    assert not evidence["ledger_applied"]


def test_standard_alpaca_order_without_commission_is_unknown():
    evidence = broker_order_cost_evidence({
        "asset_class": "us_equity", "filled_qty": "1.4",
        "filled_avg_price": "100",
    })
    assert evidence["status"] == "unavailable"
    assert evidence["amount"] is None


def test_no_empty_or_unrecognized_response_masquerades_as_zero_fees():
    for response in (None, {}, {"fee": 0}, {"totalCosts": 0}):
        evidence = broker_order_cost_evidence(response)
        assert evidence["status"] == "unavailable"
        assert evidence["amount"] is None
