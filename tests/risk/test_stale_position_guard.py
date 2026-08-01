from datetime import datetime, timedelta, timezone

import pytest

from app.risk.stale_position_guard import StalePositionConfig, StalePositionGuard


def checked_at() -> datetime:
    return datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)


def stale_config() -> StalePositionConfig:
    return StalePositionConfig(
        enabled=True,
        max_age_minutes=60,
        min_favorable_move_percent=0.5,
        buffer_percent=0.1,
    )


def test_buy_stale_when_mfe_is_too_low():
    decision = StalePositionGuard().evaluate(
        side='BUY',
        entry_price=100.0,
        highest_price=100.4,
        lowest_price=99.8,
        opened_at=checked_at() - timedelta(minutes=61),
        now=checked_at(),
        estimated_explicit_cost_percent=0.3,
        config=stale_config(),
    )

    assert decision.should_close
    assert decision.mfe_percent == pytest.approx(0.4)
    assert decision.required_mfe_percent == pytest.approx(0.5)


def test_buy_not_stale_when_mfe_is_sufficient():
    decision = StalePositionGuard().evaluate(
        side='BUY',
        entry_price=100.0,
        highest_price=100.6,
        lowest_price=99.8,
        opened_at=checked_at() - timedelta(minutes=61),
        now=checked_at(),
        estimated_explicit_cost_percent=0.3,
        config=stale_config(),
    )

    assert not decision.should_close
    assert decision.mfe_percent == pytest.approx(0.6)


def test_position_too_young_is_not_stale():
    decision = StalePositionGuard().evaluate(
        side='BUY',
        entry_price=100.0,
        highest_price=100.0,
        lowest_price=100.0,
        opened_at=checked_at() - timedelta(minutes=59),
        now=checked_at(),
        estimated_explicit_cost_percent=0.3,
        config=stale_config(),
    )

    assert not decision.should_close
