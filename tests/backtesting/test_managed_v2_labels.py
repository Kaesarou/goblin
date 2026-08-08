from datetime import UTC, datetime, timedelta

import pytest

from app.backtesting.managed_v2_labels import ManagedV2LifecycleLabeler
from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_economics import calculate_position_pnl
from app.execution.position_models import EntryPriceSource, TrackedPosition
from app.execution.scoring.managed_v2_model_contract import (
    MANAGED_V2_LABEL_CONTRACT_VERSION,
)
from app.market.models import MarketSnapshot

OPENED_AT = datetime(2026, 8, 4, 15, 0, tzinfo=UTC)


def _position(side: str) -> TrackedPosition:
    return TrackedPosition(
        position_id=f'position-{side}',
        symbol='AMD',
        side=side,
        amount=1_000.0,
        signal_price=100.0,
        executable_entry_estimate=100.0,
        broker_entry_fill_price=None,
        pnl_entry_price=100.0,
        entry_price_source=EntryPriceSource.EXECUTABLE_ESTIMATE,
        stop_loss=99.3 if side == 'BUY' else 100.7,
        take_profit=101.0 if side == 'BUY' else 99.0,
        opened_at=OPENED_AT,
        initial_stop_loss=99.3 if side == 'BUY' else 100.7,
        highest_executable_price=100.0,
        lowest_executable_price=100.0,
        highest_last_execution_price=100.0,
        lowest_last_execution_price=100.0,
        breakeven_stop_enabled=True,
        breakeven_trigger_percent=0.55,
        estimated_explicit_cost=2.0,
        estimated_explicit_cost_percent=0.20,
        stale_position_enabled=True,
        stale_position_max_age_minutes=60,
    )


def _snapshot(price: float, minute: int) -> MarketSnapshot:
    return MarketSnapshot(
        symbol='AMD',
        bid=price - 0.01,
        ask=price + 0.01,
        last=price,
        timestamp=OPENED_AT + timedelta(minutes=minute),
    )


@pytest.mark.parametrize(
    ('side', 'stop_price', 'later_opportunity_price'),
    [('BUY', 99.20, 100.60), ('SELL', 100.80, 99.40)],
)
def test_path_label_distinguishes_opportunity_after_initial_stop(
    side,
    stop_price,
    later_opportunity_price,
):
    labeler = ManagedV2LifecycleLabeler()
    labeler.add(candidate_id=f'candidate-{side}', position=_position(side))

    labeler.on_snapshot(_snapshot(stop_price, 1))
    labeler.on_snapshot(
        _snapshot(later_opportunity_price, 2),
        force_close=True,
    )
    labels = labeler.consume_completed()[0]

    assert labels.label_contract_version == MANAGED_V2_LABEL_CONTRACT_VERSION
    assert labels.opportunity == 1
    assert labels.path_quality == 0
    assert labels.first_initial_stop_at == OPENED_AT + timedelta(minutes=1)
    assert labels.first_protection_at == OPENED_AT + timedelta(minutes=2)
    assert labels.close_reason == PositionCloseReason.INITIAL_STOP.value


@pytest.mark.parametrize(
    ('side', 'opportunity_price', 'close_price'),
    [('BUY', 100.60, 100.40), ('SELL', 99.40, 99.60)],
)
def test_path_label_is_positive_when_protection_precedes_adverse_path(
    side,
    opportunity_price,
    close_price,
):
    labeler = ManagedV2LifecycleLabeler()
    labeler.add(candidate_id=f'candidate-{side}', position=_position(side))

    labeler.on_snapshot(_snapshot(opportunity_price, 1))
    labeler.on_snapshot(_snapshot(close_price, 2), force_close=True)
    labels = labeler.consume_completed()[0]

    assert labels.opportunity == 1
    assert labels.path_quality == 1
    assert labels.first_initial_stop_at is None
    assert labels.time_to_protection_seconds == 60.0
    assert labels.close_reason == PositionCloseReason.SESSION_FORCE_CLOSE.value


def test_path_is_explicitly_absent_when_no_opportunity_exists():
    labeler = ManagedV2LifecycleLabeler()
    labeler.add(candidate_id='candidate-none', position=_position('BUY'))

    labeler.on_snapshot(_snapshot(100.10, 60), force_close=True)
    labels = labeler.consume_completed()[0]

    assert labels.opportunity == 0
    assert labels.path_quality is None
    assert labels.first_protection_at is None


def test_economics_label_uses_side_aware_price_and_explicit_cost_once():
    labeler = ManagedV2LifecycleLabeler()
    labeler.add(candidate_id='candidate-economics', position=_position('BUY'))
    stop = _snapshot(99.20, 1)

    labeler.on_snapshot(stop, force_close=True)
    labels = labeler.consume_completed()[0]
    expected = calculate_position_pnl(
        side='BUY',
        amount=1_000.0,
        entry_price=100.0,
        exit_price=stop.bid,
        explicit_cost=2.0,
        explicit_cost_percent=0.20,
    )

    assert labels.net_return_percent == pytest.approx(expected.net_pnl_percent)


def test_duplicate_candidate_label_fails_at_source():
    labeler = ManagedV2LifecycleLabeler()
    labeler.add(candidate_id='duplicate', position=_position('BUY'))

    with pytest.raises(ValueError, match='Duplicate MANAGED V2 label candidate'):
        labeler.add(candidate_id='duplicate', position=_position('BUY'))
