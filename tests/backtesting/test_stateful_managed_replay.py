from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.backtesting import stateful_managed_replay
from app.backtesting.stateful_managed_replay import (
    ReplayCandidateBatch,
    StatefulManagedReplay,
)
from app.config.settings import Settings
from app.execution.candidate_selector import EvaluatedCandidateSelectionResult
from app.market.models import MarketSnapshot
from app.market.relative_spread import SPREAD_REFERENCE_MAX_OBSERVATIONS
from app.strategies.balanced_strategy_config import BalancedStrategyConfig


def test_candidate_batch_is_scored_before_spread_history_compaction(
    monkeypatch,
):
    replay = StatefulManagedReplay(
        settings=Settings(EQUITY_US_SYMBOLS='AAPL'),
        strategy_profile=BalancedStrategyConfig(),
        scenario_name='spread-history-order',
    )
    started_at = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)
    history = replay.relative_spread_history['AAPL']
    history.extend(
        MarketSnapshot(
            symbol='AAPL',
            bid=99.95,
            ask=100.05,
            last=100.0,
            timestamp=started_at + timedelta(seconds=index),
        )
        for index in range(SPREAD_REFERENCE_MAX_OBSERVATIONS + 9)
    )
    candidate = SimpleNamespace(symbol='AAPL', pending_entry_id=None)
    observed_history_sizes = []

    monkeypatch.setattr(
        replay.cooldown_guard,
        'filter_candidates',
        lambda **_kwargs: SimpleNamespace(
            selected_candidates=[candidate],
            rejected_candidates=[],
        ),
    )

    def evaluate(candidate, account_equity):
        observed_history_sizes.append(len(history))
        return SimpleNamespace(candidate=candidate)

    monkeypatch.setattr(replay, '_evaluate_candidate', evaluate)
    monkeypatch.setattr(
        stateful_managed_replay,
        'select_evaluated_trade_candidates_with_strategy_profile',
        lambda *_args, **_kwargs: EvaluatedCandidateSelectionResult([], []),
    )

    replay.on_candidate_batch(
        ReplayCandidateBatch(
            occurred_at=started_at + timedelta(minutes=10),
            account_equity=10_000.0,
            candidates=(candidate,),
        )
    )

    assert observed_history_sizes == [SPREAD_REFERENCE_MAX_OBSERVATIONS + 9]
    assert len(history) == SPREAD_REFERENCE_MAX_OBSERVATIONS
