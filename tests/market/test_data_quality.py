from datetime import datetime, timedelta, timezone

from app.market.data_quality import (
    MarketDataQualityConfig,
    MarketDataStatus,
    MarketDataValidator,
)
from app.market.models import MarketSnapshot

NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)


def snapshot(
    *,
    symbol='TEST',
    bid=99.9,
    ask=100.1,
    last=100.0,
    timestamp=NOW,
):
    return MarketSnapshot(
        symbol=symbol,
        bid=bid,
        ask=ask,
        last=last,
        timestamp=timestamp,
    )


def test_rejects_invalid_quote_before_it_can_reach_candles():
    result = MarketDataValidator().validate(
        snapshot(bid=101.0, ask=100.0),
        MarketDataQualityConfig(),
        now=NOW,
    )

    assert result.status == MarketDataStatus.REJECTED
    assert 'inverted_quote' in result.reasons


def test_rejects_stale_snapshot():
    result = MarketDataValidator().validate(
        snapshot(timestamp=NOW - timedelta(minutes=10)),
        MarketDataQualityConfig(max_snapshot_age_seconds=60),
        now=NOW,
    )

    assert result.status == MarketDataStatus.REJECTED
    assert result.reasons == ('snapshot_too_old',)


def test_live_fetch_start_time_does_not_create_false_future_snapshot():
    wall_clock = datetime.now(timezone.utc)
    request_started_at = wall_clock - timedelta(seconds=2)
    live_snapshot = snapshot(
        timestamp=wall_clock - timedelta(seconds=1)
    )

    result = MarketDataValidator().validate(
        live_snapshot,
        MarketDataQualityConfig(
            max_future_skew_seconds=0,
            max_snapshot_age_seconds=120,
        ),
        now=request_started_at,
    )

    assert result.status == MarketDataStatus.ACCEPTED
    assert 'snapshot_from_future' not in result.reasons


def test_historical_replay_time_remains_deterministic():
    replay_snapshot = snapshot(timestamp=NOW + timedelta(seconds=2))

    result = MarketDataValidator().validate(
        replay_snapshot,
        MarketDataQualityConfig(max_future_skew_seconds=0),
        now=NOW,
    )

    assert result.status == MarketDataStatus.REJECTED
    assert 'snapshot_from_future' in result.reasons


def test_large_jump_requires_a_second_snapshot_near_the_new_level():
    validator = MarketDataValidator()
    config = MarketDataQualityConfig(
        max_jump_percent=2.0,
        jump_confirmation_tolerance_percent=0.5,
    )
    assert (
        validator.validate(snapshot(), config, now=NOW).status
        == MarketDataStatus.ACCEPTED
    )

    jump = validator.validate(
        snapshot(
            bid=109.9,
            ask=110.1,
            last=110.0,
            timestamp=NOW + timedelta(seconds=10),
        ),
        config,
        now=NOW + timedelta(seconds=10),
    )
    confirmation = validator.validate(
        snapshot(
            bid=110.0,
            ask=110.2,
            last=110.1,
            timestamp=NOW + timedelta(seconds=20),
        ),
        config,
        now=NOW + timedelta(seconds=20),
    )

    assert jump.status == MarketDataStatus.QUARANTINED
    assert confirmation.status == MarketDataStatus.ACCEPTED
    assert confirmation.reasons == ('suspect_quote_level_confirmed',)


def test_batch_reports_missing_symbols_without_failing_valid_symbols():
    validator = MarketDataValidator()
    config = MarketDataQualityConfig()
    batch = validator.validate_batch(
        loop_id=7,
        requested_symbols=['ONE', 'MISSING'],
        snapshots={'ONE': snapshot(symbol='ONE')},
        configs={'ONE': config, 'MISSING': config},
        now=NOW,
    )

    assert set(batch.accepted) == {'ONE'}
    assert batch.missing_symbols == ('MISSING',)
    assert batch.rejected['MISSING'].reasons == ('missing_snapshot',)
