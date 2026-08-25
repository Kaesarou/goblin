from datetime import datetime, timezone

from app.v3.forager import ForagerCandidate, NoVolumeForager
from app.v3.models import MarketState

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def market(symbol):
    return MarketState(symbol, NOW, 99, 100, 99.5, 98, 102, 0.001, 0.01)


def test_no_volume_forager_rewards_high_volatility_and_low_readiness():
    candidates = [
        ForagerCandidate(market("AAA"), forager_volatility=0.01, ema_readiness=0.01),
        ForagerCandidate(market("BBB"), forager_volatility=0.02, ema_readiness=0.00),
        ForagerCandidate(market("CCC"), forager_volatility=0.015, ema_readiness=0.02),
    ]
    ranked = NoVolumeForager().rank(candidates, limit=2)
    assert ranked[0].market.symbol == "BBB"
    assert len(ranked) == 2


def test_forager_excludes_non_entry_allowed_state():
    blocked = MarketState("AAA", NOW, 99, 100, 99.5, 98, 102, 0.001, 0.01, entry_allowed=False)
    ranked = NoVolumeForager().rank(
        [ForagerCandidate(blocked, 0.02, 0.0)],
        limit=1,
    )
    assert ranked == ()
