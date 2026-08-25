from __future__ import annotations

from dataclasses import dataclass

from app.v3.replay import ReplayMetrics

POINT_M_ROWS = 934_557
POINT_M_SYMBOLS = 115
POINT_M_START = "2026-07-29T07:00:00+00:00"
POINT_M_END = "2026-08-21T19:58:00+00:00"


@dataclass(frozen=True)
class PointMGolden:
    return_pct: float
    entry_fills: int
    exit_fills: int | None
    cycles: int
    forced_closes: int
    max_drawdown_pct: float
    max_gross_exposure_pct: float
    gross_pnl_before_costs: float | None = None
    fees_paid: float | None = None


MAX7_GOLDEN = PointMGolden(
    return_pct=0.008575287996,
    entry_fills=236,
    exit_fills=311,
    cycles=131,
    forced_closes=3,
    max_drawdown_pct=0.008095739,
    max_gross_exposure_pct=0.222406697,
    gross_pnl_before_costs=1389.404658,
    fees_paid=531.875858,
)

RR5_GOLDEN = PointMGolden(
    return_pct=0.005254827,
    entry_fills=213,
    exit_fills=295,
    cycles=125,
    forced_closes=3,
    max_drawdown_pct=0.002806171,
    max_gross_exposure_pct=0.132029993,
)


def assert_point_m(metrics: ReplayMetrics, golden: PointMGolden, *, atol: float = 1e-6) -> None:
    numeric = {
        "return_pct": (metrics.return_pct, golden.return_pct),
        "max_drawdown_pct": (metrics.max_drawdown_pct, golden.max_drawdown_pct),
        "max_gross_exposure_pct": (
            metrics.max_gross_exposure_pct,
            golden.max_gross_exposure_pct,
        ),
    }
    if golden.gross_pnl_before_costs is not None:
        numeric["gross_pnl_before_costs"] = (
            metrics.gross_pnl_before_costs,
            golden.gross_pnl_before_costs,
        )
    if golden.fees_paid is not None:
        numeric["fees_paid"] = (metrics.fees_paid, golden.fees_paid)
    for name, (actual, expected) in numeric.items():
        if abs(actual - expected) > atol:
            raise AssertionError(f"Point-M {name}: actual={actual}, expected={expected}")

    exact = {
        "entry_fills": (metrics.entry_fills, golden.entry_fills),
        "cycles": (metrics.cycles, golden.cycles),
        "forced_closes": (metrics.forced_closes, golden.forced_closes),
    }
    if golden.exit_fills is not None:
        exact["exit_fills"] = (metrics.exit_fills, golden.exit_fills)
    for name, (actual, expected) in exact.items():
        if actual != expected:
            raise AssertionError(f"Point-M {name}: actual={actual}, expected={expected}")
