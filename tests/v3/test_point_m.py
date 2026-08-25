from dataclasses import replace

import pytest

from app.v3.point_m import MAX7_GOLDEN, assert_point_m
from app.v3.replay import ReplayMetrics


def _metrics():
    return ReplayMetrics(
        starting_balance=100_000.0,
        ending_equity=100_857.5287996,
        return_pct=MAX7_GOLDEN.return_pct,
        max_drawdown_pct=MAX7_GOLDEN.max_drawdown_pct,
        max_gross_exposure_pct=MAX7_GOLDEN.max_gross_exposure_pct,
        max_single_exposure_pct=0.124078160,
        entry_fills=MAX7_GOLDEN.entry_fills,
        exit_fills=MAX7_GOLDEN.exit_fills or 0,
        cycles=MAX7_GOLDEN.cycles,
        forced_closes=MAX7_GOLDEN.forced_closes,
        positive_days=12,
        total_days=18,
        gross_pnl_before_costs=MAX7_GOLDEN.gross_pnl_before_costs or 0.0,
        fees_paid=MAX7_GOLDEN.fees_paid or 0.0,
    )


def test_point_m_accepts_frozen_max7_reference():
    assert_point_m(_metrics(), MAX7_GOLDEN)


def test_point_m_rejects_fill_count_drift():
    with pytest.raises(AssertionError, match="entry_fills"):
        assert_point_m(replace(_metrics(), entry_fills=235), MAX7_GOLDEN)
