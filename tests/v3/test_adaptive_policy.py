from types import SimpleNamespace

from app.v3.adaptive_policy import (
    AdaptiveConfig, CapitalAllocationService, InventoryLifecycleService,
    Lifecycle, MarketRegimeService, Regime,
)


def test_regime_requires_coverage_and_persistence_and_stress_is_immediate():
    service = MarketRegimeService(AdaptiveConfig())
    def update(ret, mom, values):
        return service.evaluate(benchmark_return=ret, benchmark_momentum=mom,
                                returns=values, expected=10).regime
    assert update(0, 0, [1]) == Regime.UNKNOWN
    for _ in range(2):
        assert update(0, 0, [1] * 10) == Regime.UNKNOWN
    assert update(0, 0, [1] * 10) == Regime.NORMAL
    assert update(-.02, -.01, [-1] * 10) == Regime.SYSTEMIC_STRESS
    for _ in range(2):
        assert update(-.01, .003, [1] * 10) == Regime.SYSTEMIC_STRESS
    assert update(-.01, .003, [1] * 10) == Regime.RECOVERY
    assert update(None, None, []) == Regime.UNKNOWN


def test_risk_gates_treat_technology_as_shared_exposure():
    policy = CapitalAllocationService(AdaptiveConfig())
    holdings = [SimpleNamespace(symbol="NOW"), SimpleNamespace(symbol="AMAT")]
    assert not policy.entry_allowed(symbol="LRCX", regime=Regime.NORMAL, inventories=holdings)
    assert not policy.entry_allowed(symbol="AIR.PA", regime=Regime.BEARISH, inventories=holdings)
    assert policy.entry_allowed(symbol="AIR.PA", regime=Regime.NORMAL, inventories=holdings)
    assert not policy.reentry_allowed(regime=Regime.NORMAL, lifecycle=Lifecycle.STALE)
    assert not policy.reentry_allowed(regime=Regime.SYSTEMIC_STRESS, lifecycle=Lifecycle.ACTIVE)


def test_time_and_rotation_are_separate_from_profit_policy():
    config = AdaptiveConfig()
    lifecycle = InventoryLifecycleService(config, 5)
    inventory = SimpleNamespace(entry_fill_count=5)
    assert lifecycle.evaluate(inventory=inventory, age_sessions=1) == Lifecycle.SATURATED
    assert lifecycle.evaluate(inventory=inventory, age_sessions=5) == Lifecycle.STALE
    assert lifecycle.evaluate(inventory=inventory, age_sessions=10) == Lifecycle.EXIT_DUE
    policy = CapitalAllocationService(config)
    kwargs = dict(regime=Regime.NORMAL, unrealized_return=-.1,
                  rotation_candidate=True, full=True, partial_done=False)
    assert policy.risk_exit_reason(lifecycle=Lifecycle.EXIT_DUE, **kwargs) == "time_exit"
    assert policy.risk_exit_reason(lifecycle=Lifecycle.STALE, **kwargs) == "rotation"
    assert policy.risk_exit_reason(lifecycle=Lifecycle.ACTIVE, **kwargs) is None
