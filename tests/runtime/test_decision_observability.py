from datetime import UTC, datetime
from types import SimpleNamespace

from app.execution.candidate_economics import EvaluatedTradeCandidate
from app.execution.candidate_selector import RejectedEvaluatedCandidateSelection
from app.execution.trade_candidate import TradeCandidate
from app.journal.analysis_journal import AnalysisJournal
from app.market.models import Candle, MarketSnapshot
from app.risk.trade_cooldown_guard import (
    RejectedCooldownCandidate,
    TradeCooldownDecision,
    TradeCooldownFilterResult,
)
from app.runtime.resilient_candidate_execution import (
    ResilientCandidateExecutionCoordinator,
)
from app.strategies.signals import Signal

NOW = datetime(2026, 7, 22, 9, 0, tzinfo=UTC)


class RecordingWriter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write(self, event_type: str, payload: dict) -> bool:
        self.events.append((event_type, payload))
        return True


class RecordingStore:
    def __init__(self) -> None:
        self.deleted_at = []

    def delete_expired(self, now) -> None:
        self.deleted_at.append(now)


class BlockingCooldownGuard:
    def __init__(self, candidate: TradeCandidate) -> None:
        self.store = RecordingStore()
        self.candidate = candidate

    def filter_candidates(self, *, candidates, risk_manager, now):
        assert candidates == [self.candidate]
        return TradeCooldownFilterResult(
            selected_candidates=[],
            rejected_candidates=[
                RejectedCooldownCandidate(
                    candidate=self.candidate,
                    decision=TradeCooldownDecision(
                        allowed=False,
                        reason='same_side_trade_cooldown_active',
                        remaining_seconds=120,
                        lock_scope='same_side',
                        blocked_sides=('BUY',),
                    ),
                )
            ],
        )


class FakeRiskManager:
    def instrument_profile_for(self, symbol: str):
        return SimpleNamespace(asset_class='EQUITY_EU')

    def risk_profile_for(self, symbol: str):
        return SimpleNamespace(name='eu')


def candidate() -> TradeCandidate:
    snapshot = MarketSnapshot(
        symbol='AIR.PA',
        bid=100.0,
        ask=100.1,
        last=100.05,
        timestamp=NOW,
    )
    candle = Candle(
        symbol='AIR.PA',
        timeframe_seconds=60,
        open=99.5,
        high=100.2,
        low=99.4,
        close=100.05,
        volume=None,
        opened_at=NOW,
        closed_at=NOW,
    )
    return TradeCandidate(
        symbol='AIR.PA',
        snapshot=snapshot,
        candle=candle,
        signal=Signal('BUY', 90.0, 'breakout'),
        rank_reason='test',
        session_key='eu:2026-07-22',
        base_score=115.0,
        directional_score=116.0,
        probability_score=34.0,
        touch_probability=0.40,
        direction_probability=0.425,
        tp_probability=0.17,
        sl_probability=0.23,
        neither_probability=0.60,
        direction_break_even_probability=0.55,
        direction_edge=-0.125,
        outcome_probability_model_version='outcome_probability_v1',
        candidate_id='candidate-1',
        origin_candidate_id='candidate-1',
    )


def evaluated_candidate() -> EvaluatedTradeCandidate:
    item = candidate()
    return EvaluatedTradeCandidate(
        candidate=item,
        economics=SimpleNamespace(
            estimated_total_cost_percent=0.15,
            expected_net_profit_percent=1.85,
        ),
        effective_sl_tp=SimpleNamespace(
            source='eu_trend_buy_v1',
            stop_loss_percent=1.2,
            take_profit_percent=2.0,
        ),
        tp_feasibility=SimpleNamespace(
            movement_consumed_to_tp_ratio=0.3,
            entry_freshness_score=55.0,
            model_version='tp_feasibility_score_v4',
        ),
        outcome_probability=SimpleNamespace(
            profile_key='eu_trend_buy_v1',
        ),
        entry_decision=SimpleNamespace(
            action='READY_FOR_SELECTION',
            reason='candidate_ready_for_selection',
            model_version='entry_router_v6',
        ),
    )


def test_cooldown_rejection_retains_the_fully_evaluated_candidate():
    evaluated = evaluated_candidate()
    coordinator = object.__new__(ResilientCandidateExecutionCoordinator)
    coordinator.cooldown_guard = BlockingCooldownGuard(evaluated.candidate)
    coordinator.risk_manager = FakeRiskManager()
    coordinator.trade_journal = RecordingWriter()

    selected, rejected = coordinator._filter_evaluated_candidates_by_cooldown(
        [evaluated],
        now=NOW,
    )

    assert selected == []
    assert rejected == [
        RejectedEvaluatedCandidateSelection(
            evaluated_candidate=evaluated,
            reason='same_side_trade_cooldown_active',
            selection_threshold_source='trade_cooldown',
        )
    ]
    event_type, payload = coordinator.trade_journal.events[0]
    assert event_type == 'cooldown_blocked'
    assert payload['evaluated_candidate'] is evaluated
    assert payload['entry_decision'] is evaluated.entry_decision
    assert payload['candidate_economics'] is evaluated.economics


def test_cooldown_rejection_produces_a_standalone_entry_decision(tmp_path):
    evaluated = evaluated_candidate()
    trade_writer = RecordingWriter()
    journal = AnalysisJournal(
        trade_journal=trade_writer,
        errors_journal=RecordingWriter(),
        summary_path=str(tmp_path / 'summary.json'),
        detail_level='normal',
        run_id='run-test',
        strategy='TrendStrategy',
        profile='balanced',
    )

    journal.write(
        'candidate_selection',
        {
            'strategy_profile': 'balanced',
            'selected_candidates': [],
            'rejected_candidates': [],
            'selected_evaluated_candidates': [],
            'rejected_evaluated_candidates': [
                RejectedEvaluatedCandidateSelection(
                    evaluated_candidate=evaluated,
                    reason='same_side_trade_cooldown_active',
                    selection_threshold_source='trade_cooldown',
                )
            ],
        },
    )

    entry_events = [
        payload
        for event_type, payload in trade_writer.events
        if event_type == 'entry_decision'
    ]
    assert len(entry_events) == 1
    assert entry_events[0]['candidate_id'] == 'candidate-1'
    assert entry_events[0]['selection_outcome'] == 'rejected'
    assert entry_events[0]['selection_reason'] == (
        'same_side_trade_cooldown_active'
    )
    assert entry_events[0]['tp_feasibility'] is evaluated.tp_feasibility
