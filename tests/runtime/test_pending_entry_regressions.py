from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.execution.candidate_economics import CandidateEconomics, EvaluatedTradeCandidate
from app.execution.trade_candidate import TradeCandidate
from app.market.models import Candle, MarketSnapshot
from app.runtime.pending_entry import PendingEntryManager, PendingEntryState
from app.strategies.entry_confirmation import EntryConfirmationConfig
from app.strategies.signals import Signal


NOW = datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)


def _candle(*, minute=0, close=100.0, high=100.2, low=99.8, open_=100.0):
    closed_at = NOW + timedelta(minutes=minute)
    return Candle(
        'AMD',
        60,
        open_,
        high,
        low,
        close,
        None,
        closed_at - timedelta(minutes=1),
        closed_at,
    )


def _evaluated(*, level=100.0):
    current_candle = _candle()
    signal = Signal(
        action='BUY',
        setup_quality=0.8,
        reason='test',
        metadata={
            'range_high': level,
            'snapshot_momentum_percent': 0.2,
            'atr_percent': 0.2,
        },
    )
    candidate = TradeCandidate(
        symbol='AMD',
        snapshot=MarketSnapshot('AMD', 99.95, 100.05, 100.0, NOW),
        candle=current_candle,
        signal=signal,
        rank_reason='test',
        session_key='US',
        directional_score=120.0,
    )
    economics = CandidateEconomics(
        100, 1, 0.5, 0.5, 0.5, 0.5, 0.1, 0.1
    )
    return EvaluatedTradeCandidate(
        candidate=candidate,
        economics=economics,
        tp_feasibility=SimpleNamespace(
            feasibility_score=20.0,
            score_contribution=-9.0,
        ),
        readiness_reason='entry_decision_required',
    )


def test_expired_setup_is_not_immediately_registered_again():
    manager = PendingEntryManager()
    evaluated = _evaluated(level=100.0)
    manager.register(evaluated_candidate=evaluated, max_candles=1)

    result = manager.observe(
        symbol='AMD',
        candle=_candle(minute=1),
        snapshot=evaluated.candidate.snapshot,
        signal=Signal.hold('confirmation_pending'),
        session_key='US',
        session_tradable=True,
        spread_percent=0.05,
        config=EntryConfirmationConfig(max_candles=1),
    )

    assert result.events[-1].event_type == 'pending_entry_expired'
    assert manager.register(evaluated_candidate=evaluated, max_candles=1) == ()
    assert manager.snapshot() == []


def test_new_breakout_level_can_register_after_previous_setup_expired():
    manager = PendingEntryManager()
    first = _evaluated(level=100.0)
    manager.register(evaluated_candidate=first, max_candles=1)
    manager.observe(
        symbol='AMD',
        candle=_candle(minute=1),
        snapshot=first.candidate.snapshot,
        signal=Signal.hold('confirmation_pending'),
        session_key='US',
        session_tradable=True,
        spread_percent=0.05,
        config=EntryConfirmationConfig(max_candles=1),
    )

    events = manager.register(
        evaluated_candidate=_evaluated(level=101.0),
        max_candles=1,
    )

    assert events[0].event_type == 'pending_entry_registered'


def test_confirmed_pending_stays_confirmed_until_selection_outcome():
    manager = PendingEntryManager()
    evaluated = _evaluated()
    manager.register(evaluated_candidate=evaluated, max_candles=5)
    pending = manager.snapshot()[0]
    manager._entries[pending.key] = replace(
        pending,
        state=PendingEntryState.CONFIRMED,
        observed_candles=2,
        confirmation_type='persistence',
    )

    observation = manager.observe(
        symbol='AMD',
        candle=_candle(minute=1, close=100.0),
        snapshot=evaluated.candidate.snapshot,
        signal=Signal.hold('confirmation_pending'),
        session_key='US',
        session_tradable=True,
        spread_percent=0.05,
        config=EntryConfirmationConfig(max_candles=5),
    )

    assert observation.events == ()
    assert manager.get(pending.key).state == PendingEntryState.CONFIRMED
    assert manager.get(pending.key).observed_candles == 2
