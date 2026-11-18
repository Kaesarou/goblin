"""Research-only adaptive capital policy; no broker or runtime authority."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import StrEnum

from app.instruments.sector_map import DEFAULT_SECTOR_BY_SYMBOL


class Regime(StrEnum):
    UNKNOWN = "unknown"
    NORMAL = "normal"
    BEARISH = "bearish"
    SYSTEMIC_STRESS = "systemic_stress"
    RECOVERY = "recovery"


# Extend the existing partial research map for the observed concentration.
# Unknown symbols form one conservative bucket, not independent sectors.
SECTORS = {
    **DEFAULT_SECTOR_BY_SYMBOL,
    **dict.fromkeys(("NOW", "LRCX", "AMAT", "PLTR", "ADBE", "INTC", "MU",
                     "ASML", "KLAC", "QCOM", "TXN", "CRM", "ORCL", "SNPS",
                     "CDNS", "ADSK", "PANW", "CRWD", "ANET", "MRVL"), "TECHNOLOGY"),
}


@dataclass(frozen=True)
class AdaptiveConfig:
    name: str = "INVENTORY_ADAPTIVE_RR5_V1"
    min_coverage: float = .60
    min_breadth_symbols: int = 5
    bearish_benchmark_return: float = -.005
    stress_benchmark_return: float = -.015
    bearish_breadth: float = .40
    stress_breadth: float = .25
    recovery_momentum: float = .002
    persistence_windows: int = 3
    stale_after_sessions: int = 5
    exit_after_sessions: int = 10
    max_sector_inventories: int = 2
    use_entry_regime: bool = True
    use_reentry_regime: bool = True
    use_time_exit: bool = True
    use_rotation: bool = True
    use_sector_limit: bool = True

    def __post_init__(self):
        if not (0 < self.min_coverage <= 1 and self.min_breadth_symbols > 0
                and self.persistence_windows > 0
                and 0 < self.stale_after_sessions < self.exit_after_sessions
                and self.max_sector_inventories > 0
                and self.stress_benchmark_return < self.bearish_benchmark_return < 0
                and 0 < self.stress_breadth < self.bearish_breadth < 1
                and self.recovery_momentum > 0):
            raise ValueError("Invalid adaptive research configuration")

    def metadata(self):
        return asdict(self)


@dataclass(frozen=True)
class MarketRegime:
    regime: Regime
    benchmark_return: float | None
    benchmark_momentum: float | None
    breadth: float | None
    coverage: float
    valid_symbols: int


class MarketRegimeService:
    """Classify observed session returns, not a directional forecast."""
    def __init__(self, config: AdaptiveConfig):
        self.config = config
        self.current = Regime.UNKNOWN
        self.pending = Regime.UNKNOWN
        self.count = 0

    def reset(self):
        self.current = self.pending = Regime.UNKNOWN
        self.count = 0

    def evaluate(self, *, benchmark_return, benchmark_momentum, returns, expected):
        values = [v for v in returns if math.isfinite(v)]
        coverage = len(values) / expected if expected > 0 else 0.
        breadth = sum(v > 0 for v in values) / len(values) if values else None
        c = self.config
        if (benchmark_return is None or not math.isfinite(benchmark_return)
                or benchmark_momentum is None or not math.isfinite(benchmark_momentum)
                or coverage < c.min_coverage or len(values) < c.min_breadth_symbols):
            self.reset()
            return MarketRegime(self.current, benchmark_return, benchmark_momentum,
                                breadth, coverage, len(values))
        if benchmark_return <= c.stress_benchmark_return and breadth <= c.stress_breadth:
            proposed = Regime.SYSTEMIC_STRESS
        elif (self.current in (Regime.BEARISH, Regime.SYSTEMIC_STRESS, Regime.RECOVERY)
              and benchmark_momentum >= c.recovery_momentum and breadth > c.bearish_breadth):
            proposed = Regime.RECOVERY
        elif benchmark_return <= c.bearish_benchmark_return and breadth <= c.bearish_breadth:
            proposed = Regime.BEARISH
        else:
            proposed = Regime.NORMAL
        if proposed != self.pending:
            self.pending, self.count = proposed, 1
        else:
            self.count += 1
        if proposed == Regime.SYSTEMIC_STRESS or self.count >= c.persistence_windows:
            self.current = proposed
        return MarketRegime(self.current, benchmark_return, benchmark_momentum,
                            breadth, coverage, len(values))


class Lifecycle(StrEnum):
    ACTIVE = "active"
    SATURATED = "saturated"
    STALE = "stale"
    EXIT_DUE = "exit_due"


class InventoryLifecycleService:
    def __init__(self, config: AdaptiveConfig, max_entry_fills: int):
        self.config = config
        self.max_entry_fills = max_entry_fills

    def evaluate(self, *, inventory, age_sessions):
        if age_sessions >= self.config.exit_after_sessions:
            return Lifecycle.EXIT_DUE
        if age_sessions >= self.config.stale_after_sessions:
            return Lifecycle.STALE
        if inventory.entry_fill_count >= self.max_entry_fills:
            return Lifecycle.SATURATED
        return Lifecycle.ACTIVE


class CapitalAllocationService:
    """Deterministic, explicitly uncalibrated research gates and exit policy."""
    def __init__(self, config: AdaptiveConfig):
        self.config = config

    def entry_allowed(self, *, symbol, regime, inventories):
        c = self.config
        if c.use_entry_regime and regime in (Regime.UNKNOWN, Regime.BEARISH, Regime.SYSTEMIC_STRESS):
            return False
        if c.use_sector_limit:
            sector = SECTORS.get(symbol, "UNKNOWN")
            count = sum(SECTORS.get(i.symbol, "UNKNOWN") == sector for i in inventories)
            if count >= c.max_sector_inventories:
                return False
        return True

    def reentry_allowed(self, *, regime, lifecycle):
        if self.config.use_reentry_regime and regime not in (Regime.NORMAL, Regime.RECOVERY):
            return False
        if self.config.use_time_exit and lifecycle in (Lifecycle.STALE, Lifecycle.EXIT_DUE):
            return False
        return True

    def risk_exit_reason(self, *, lifecycle, regime, unrealized_return,
                         rotation_candidate, full, partial_done):
        c = self.config
        if c.use_time_exit and lifecycle == Lifecycle.EXIT_DUE:
            return "time_exit"
        if lifecycle == Lifecycle.STALE and unrealized_return < 0:
            if c.use_time_exit and regime == Regime.SYSTEMIC_STRESS and not partial_done:
                return "stress_reduction"
            if (c.use_rotation and full and rotation_candidate
                    and regime in (Regime.NORMAL, Regime.RECOVERY)):
                return "rotation"
        return None
