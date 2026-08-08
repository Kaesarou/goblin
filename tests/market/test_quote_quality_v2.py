from datetime import UTC, datetime, timedelta

import pytest

from app.execution.managed_stop import ManagedProtectionType
from app.execution.position_close_reason import PositionCloseReason
from app.execution.position_models import EntryPriceSource, TrackedPosition
from app.execution.position_tracker import PositionTracker
from app.market.data_quality import (
    QUOTE_QUALITY_CONTRACT_VERSION,
    MarketDataQualityConfig,
    MarketDataStatus,
    MarketDataValidator,
    quote_quality_contract_metadata,
)
from app.market.models import MarketSnapshot

START = datetime(2026, 8, 4, 15, 0, tzinfo=UTC)
CONFIG = MarketDataQualityConfig()


def test_quote_quality_contract_names_executable_quote_basis():
    metadata = quote_quality_contract_metadata()

    assert metadata['change_basis'] == (
        'max_abs_percent_change_across_bid_ask_last'
    )
    assert metadata['confirmation_basis'] == (
        'max_abs_percent_distance_across_bid_ask_last'
    )
    assert metadata['ambiguous_follow_up'].startswith(
        'quarantine_and_rebase_pending'
    )


def _snapshot(price: float, index: int) -> MarketSnapshot:
    return MarketSnapshot(
        symbol='GOOGL',
        bid=price - 0.01,
        ask=price + 0.01,
        last=price,
        timestamp=START + timedelta(milliseconds=index * 20),
    )


def _warmed_validator() -> tuple[MarketDataValidator, int, float]:
    validator = MarketDataValidator()
    price = 100.0
    for index in range(21):
        price = 100.0 + index * 0.005
        result = validator.validate(
            _snapshot(price, index),
            CONFIG,
            now=_snapshot(price, index).timestamp,
        )
        assert result.status is MarketDataStatus.ACCEPTED
    return validator, 21, price


def _position(
    side: str,
    *,
    entry: float,
    stop: float,
    take_profit: float,
    breakeven_trigger: float = 0.0,
    trailing_trigger: float = 0.0,
) -> TrackedPosition:
    return TrackedPosition(
        position_id=f'position-{side}',
        symbol='GOOGL',
        side=side,
        amount=100.0,
        signal_price=entry,
        executable_entry_estimate=entry,
        broker_entry_fill_price=entry,
        pnl_entry_price=entry,
        entry_price_source=EntryPriceSource.BROKER_FILL,
        stop_loss=stop,
        take_profit=take_profit,
        opened_at=START - timedelta(minutes=1),
        initial_stop_loss=stop,
        highest_executable_price=entry,
        lowest_executable_price=entry,
        highest_last_execution_price=entry,
        lowest_last_execution_price=entry,
        breakeven_stop_enabled=breakeven_trigger > 0,
        breakeven_trigger_percent=breakeven_trigger,
        trailing_stop_enabled=trailing_trigger > 0,
        trailing_stop_trigger_percent=trailing_trigger,
        trailing_stop_distance_percent=0.40,
    )


@pytest.mark.parametrize(
    ('side', 'suspect_price', 'stop', 'take_profit'),
    [
        ('BUY', 99.10, 99.50, 102.0),
        ('SELL', 101.10, 100.70, 98.0),
    ],
)
def test_isolated_suspect_quote_cannot_trigger_initial_stop(
    side,
    suspect_price,
    stop,
    take_profit,
):
    validator, index, baseline = _warmed_validator()
    tracker = PositionTracker()
    tracker.restore_open_position(
        _position(
            side,
            entry=baseline,
            stop=stop,
            take_profit=take_profit,
        )
    )

    suspect = validator.validate(
        _snapshot(suspect_price, index),
        CONFIG,
        now=_snapshot(suspect_price, index).timestamp,
    )
    assert suspect.status is MarketDataStatus.QUARANTINED
    assert suspect.reasons == ('suspect_quote_requires_confirmation',)
    assert suspect.suspect_quote is True
    assert suspect.quality_contract_version == QUOTE_QUALITY_CONTRACT_VERSION
    assert suspect.suspicion_threshold_percent == pytest.approx(0.75)
    assert tracker.open_positions_snapshot()

    recovered = validator.validate(
        _snapshot(baseline + 0.005, index + 1),
        CONFIG,
        now=_snapshot(baseline + 0.005, index + 1).timestamp,
    )
    assert recovered.status is MarketDataStatus.ACCEPTED
    assert recovered.reasons == ('isolated_suspect_quote_rejected',)
    assert recovered.quarantined_snapshot_timestamp == suspect.snapshot.timestamp
    assert tracker.evaluate_snapshot(recovered.snapshot) == []


def test_recorded_googl_reversal_is_quarantined_before_initial_stop():
    validator = MarketDataValidator()
    for index in range(21):
        bid = 376.52 + index * 0.005
        snapshot = MarketSnapshot(
            'GOOGL',
            bid,
            bid + 0.02,
            bid,
            START + timedelta(milliseconds=index * 20),
        )
        assert validator.validate(
            snapshot,
            CONFIG,
            now=snapshot.timestamp,
        ).status is MarketDataStatus.ACCEPTED

    tracker = PositionTracker()
    tracker.restore_open_position(
        _position(
            'BUY',
            entry=376.73,
            stop=374.09289,
            take_profit=381.25,
        )
    )
    suspect_snapshot = MarketSnapshot(
        'GOOGL',
        373.11,
        373.13,
        373.11,
        START + timedelta(milliseconds=420),
    )
    suspect = validator.validate(
        suspect_snapshot,
        CONFIG,
        now=suspect_snapshot.timestamp,
    )

    assert suspect.status is MarketDataStatus.QUARANTINED
    assert suspect.price_change_percent == pytest.approx(-0.932, abs=0.001)
    assert tracker.open_positions_snapshot()

    recovery_snapshot = MarketSnapshot(
        'GOOGL',
        376.61,
        376.63,
        376.61,
        START + timedelta(milliseconds=439),
    )
    recovery = validator.validate(
        recovery_snapshot,
        CONFIG,
        now=recovery_snapshot.timestamp,
    )

    assert recovery.status is MarketDataStatus.ACCEPTED
    assert recovery.reasons == ('isolated_suspect_quote_rejected',)
    assert tracker.evaluate_snapshot(recovery.snapshot) == []


def test_executable_bid_anomaly_is_detected_even_when_last_is_unchanged():
    validator, index, baseline = _warmed_validator()
    suspect_snapshot = MarketSnapshot(
        'GOOGL',
        baseline - 1.0,
        baseline - 0.98,
        baseline,
        START + timedelta(milliseconds=index * 20),
    )

    suspect = validator.validate(
        suspect_snapshot,
        CONFIG,
        now=suspect_snapshot.timestamp,
    )

    assert suspect.status is MarketDataStatus.QUARANTINED
    assert suspect.price_change_percent < -0.9

    recovery_snapshot = _snapshot(baseline + 0.005, index + 1)
    recovery = validator.validate(
        recovery_snapshot,
        CONFIG,
        now=recovery_snapshot.timestamp,
    )

    assert recovery.status is MarketDataStatus.ACCEPTED
    assert recovery.reasons == ('isolated_suspect_quote_rejected',)


@pytest.mark.parametrize(
    ('side', 'first_price', 'second_price', 'stop', 'take_profit'),
    [
        ('BUY', 99.10, 99.11, 99.50, 102.0),
        ('SELL', 101.10, 101.09, 100.70, 98.0),
    ],
)
def test_persistent_stop_level_is_accepted_on_the_next_quote_only(
    side,
    first_price,
    second_price,
    stop,
    take_profit,
):
    validator, index, baseline = _warmed_validator()
    tracker = PositionTracker()
    tracker.restore_open_position(
        _position(
            side,
            entry=baseline,
            stop=stop,
            take_profit=take_profit,
        )
    )

    first = validator.validate(
        _snapshot(first_price, index),
        CONFIG,
        now=_snapshot(first_price, index).timestamp,
    )
    assert first.status is MarketDataStatus.QUARANTINED
    assert tracker.open_positions_snapshot()

    confirmed = validator.validate(
        _snapshot(second_price, index + 1),
        CONFIG,
        now=_snapshot(second_price, index + 1).timestamp,
    )
    assert confirmed.status is MarketDataStatus.ACCEPTED
    assert confirmed.reasons == ('suspect_quote_level_confirmed',)
    close = tracker.evaluate_snapshot(confirmed.snapshot)
    assert close[0].reason is PositionCloseReason.INITIAL_STOP


def test_ordinary_quote_is_never_forced_through_confirmation():
    validator, index, baseline = _warmed_validator()
    ordinary = _snapshot(baseline * 1.006, index)

    result = validator.validate(ordinary, CONFIG, now=ordinary.timestamp)

    assert result.status is MarketDataStatus.ACCEPTED
    assert result.reasons == ()
    assert result.suspect_quote is False


def test_ambiguous_follow_up_stays_quarantined_then_confirms_its_level():
    validator, index, baseline = _warmed_validator()
    first = _snapshot(baseline * 1.01, index)
    ambiguous = _snapshot(baseline * 1.005, index + 1)
    confirmation = _snapshot(baseline * 1.0051, index + 2)

    assert validator.validate(
        first,
        CONFIG,
        now=first.timestamp,
    ).status is MarketDataStatus.QUARANTINED
    pending = validator.validate(
        ambiguous,
        CONFIG,
        now=ambiguous.timestamp,
    )
    resolved = validator.validate(
        confirmation,
        CONFIG,
        now=confirmation.timestamp,
    )

    assert pending.status is MarketDataStatus.QUARANTINED
    assert pending.reasons == ('suspect_quote_resolution_pending',)
    assert resolved.status is MarketDataStatus.ACCEPTED
    assert resolved.reasons == ('suspect_quote_level_confirmed',)


@pytest.mark.parametrize(
    ('side', 'move_ratio', 'stop', 'take_profit'),
    [
        ('BUY', 0.994, 99.55, 102.0),
        ('SELL', 1.006, 100.65, 98.0),
    ],
)
def test_normal_initial_stop_remains_immediate(
    side,
    move_ratio,
    stop,
    take_profit,
):
    validator, index, baseline = _warmed_validator()
    tracker = PositionTracker()
    tracker.restore_open_position(
        _position(
            side,
            entry=baseline,
            stop=stop,
            take_profit=take_profit,
        )
    )
    quote = _snapshot(baseline * move_ratio, index)

    result = validator.validate(quote, CONFIG, now=quote.timestamp)
    close = tracker.evaluate_snapshot(result.snapshot)

    assert result.status is MarketDataStatus.ACCEPTED
    assert result.reasons == ()
    assert close[0].reason is PositionCloseReason.INITIAL_STOP


@pytest.mark.parametrize(
    ('side', 'move_ratio', 'take_profit'),
    [
        ('BUY', 1.006, 100.55),
        ('SELL', 0.994, 99.65),
    ],
)
def test_normal_take_profit_remains_immediate(side, move_ratio, take_profit):
    validator, index, baseline = _warmed_validator()
    tracker = PositionTracker()
    tracker.restore_open_position(
        _position(
            side,
            entry=baseline,
            stop=98.0 if side == 'BUY' else 102.0,
            take_profit=take_profit,
        )
    )
    quote = _snapshot(baseline * move_ratio, index)

    result = validator.validate(quote, CONFIG, now=quote.timestamp)
    close = tracker.evaluate_snapshot(result.snapshot)

    assert result.status is MarketDataStatus.ACCEPTED
    assert close[0].reason is PositionCloseReason.TAKE_PROFIT


@pytest.mark.parametrize(
    ('side', 'move_ratio', 'breakeven_trigger'),
    [
        ('BUY', 1.006, 0.55),
        ('SELL', 0.9935, 0.60),
    ],
)
def test_normal_breakeven_activation_has_no_quote_delay(
    side,
    move_ratio,
    breakeven_trigger,
):
    validator, index, baseline = _warmed_validator()
    tracker = PositionTracker()
    tracker.restore_open_position(
        _position(
            side,
            entry=baseline,
            stop=98.0 if side == 'BUY' else 102.0,
            take_profit=103.0 if side == 'BUY' else 97.0,
            breakeven_trigger=breakeven_trigger,
        )
    )
    quote = _snapshot(baseline * move_ratio, index)

    result = validator.validate(quote, CONFIG, now=quote.timestamp)
    assert result.status is MarketDataStatus.ACCEPTED
    assert tracker.evaluate_snapshot(result.snapshot) == []
    position = tracker.open_positions_snapshot()[0]
    assert position.managed_stop_protection_type is (
        ManagedProtectionType.BREAKEVEN
    )


def test_normal_trailing_activation_has_no_quote_delay():
    validator, index, baseline = _warmed_validator()
    tracker = PositionTracker()
    tracker.restore_open_position(
        _position(
            'BUY',
            entry=baseline,
            stop=98.0,
            take_profit=103.0,
            trailing_trigger=0.60,
        )
    )
    quote = _snapshot(baseline * 1.0065, index)

    result = validator.validate(quote, CONFIG, now=quote.timestamp)
    assert result.status is MarketDataStatus.ACCEPTED
    assert tracker.evaluate_snapshot(result.snapshot) == []
    assert tracker.open_positions_snapshot()[
        0
    ].managed_stop_protection_type is ManagedProtectionType.TRAILING


@pytest.mark.parametrize(
    ('side', 'suspect_ratio', 'stop', 'take_profit', 'breakeven_trigger'),
    [
        ('BUY', 1.01, 98.0, 100.55, 0.55),
        ('SELL', 0.99, 102.0, 99.55, 0.60),
    ],
)
def test_isolated_suspect_quote_cannot_trigger_profit_or_protection(
    side,
    suspect_ratio,
    stop,
    take_profit,
    breakeven_trigger,
):
    validator, index, baseline = _warmed_validator()
    tracker = PositionTracker()
    tracker.restore_open_position(
        _position(
            side,
            entry=baseline,
            stop=stop,
            take_profit=take_profit,
            breakeven_trigger=breakeven_trigger,
            trailing_trigger=breakeven_trigger,
        )
    )
    suspect_snapshot = _snapshot(baseline * suspect_ratio, index)

    suspect = validator.validate(
        suspect_snapshot,
        CONFIG,
        now=suspect_snapshot.timestamp,
    )

    assert suspect.status is MarketDataStatus.QUARANTINED
    position = tracker.open_positions_snapshot()[0]
    assert position.managed_stop_protection_type is None
    assert position.stop_loss == stop

    recovery_snapshot = _snapshot(baseline + 0.005, index + 1)
    recovered = validator.validate(
        recovery_snapshot,
        CONFIG,
        now=recovery_snapshot.timestamp,
    )

    assert recovered.status is MarketDataStatus.ACCEPTED
    assert recovered.reasons == ('isolated_suspect_quote_rejected',)
    assert tracker.evaluate_snapshot(recovered.snapshot) == []
    position = tracker.open_positions_snapshot()[0]
    assert position.managed_stop_protection_type is None
    assert position.stop_loss == stop
