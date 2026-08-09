from datetime import UTC, datetime, timedelta

import pytest

from app.backtesting.replay_pricing import (
    REPLAY_PRICING_CONTRACT_VERSION,
    HistoricalReplayFill,
    ReplayFillLedger,
    ReplayMode,
    ReplayPriceProvenance,
    historical_fills_from_journal_records,
)
from app.execution.position_economics import calculate_position_pnl
from app.execution.strategy_segment import StrategySegment
from app.execution.trade_candidate import TradeCandidate
from app.market.models import Candle, MarketSnapshot
from app.strategies.signals import Signal

NOW = datetime(2026, 8, 4, 15, 0, tzinfo=UTC)


def _candidate(side: str) -> TradeCandidate:
    segment = (
        StrategySegment.EQUITY_US_BUY
        if side == 'BUY'
        else StrategySegment.EQUITY_US_SELL
    )
    return TradeCandidate(
        symbol='AMD',
        snapshot=MarketSnapshot('AMD', 99.8, 100.2, 100.0, NOW),
        candle=Candle(
            'AMD',
            60,
            99.5,
            100.2,
            99.4,
            100.0,
            None,
            NOW - timedelta(minutes=1),
            NOW,
        ),
        signal=Signal(side, 0.8, 'test'),
        rank_reason='test',
        candidate_id=f'candidate-{side}',
        origin_candidate_id=f'candidate-{side}',
        segment=segment,
    )


def _fill(side: str, *, with_exit: bool = True) -> HistoricalReplayFill:
    return HistoricalReplayFill(
        candidate_id=f'candidate-{side}',
        position_id=f'position-{side}',
        entry_price=100.35 if side == 'BUY' else 99.65,
        entry_at=NOW + timedelta(seconds=1),
        exit_price=(101.10 if side == 'BUY' else 98.90) if with_exit else None,
        exit_at=NOW + timedelta(minutes=10) if with_exit else None,
        close_order_id=f'close-{side}' if with_exit else None,
    )


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_live_validation_prioritizes_recorded_broker_entry_fill(side):
    ledger = ReplayFillLedger((_fill(side),))

    entry = ledger.entry_for(_candidate(side), ReplayMode.LIVE_VALIDATION)

    assert entry.price == _fill(side).entry_price
    assert entry.provenance is ReplayPriceProvenance.BROKER_HISTORICAL_FILL
    assert entry.historical_position_id == f'position-{side}'
    assert entry.historical_entry_at == NOW + timedelta(seconds=1)


@pytest.mark.parametrize(
    ('side', 'expected_price'),
    [('BUY', 100.2), ('SELL', 99.8)],
)
def test_counterfactual_uses_side_aware_executable_and_never_fill(
    side,
    expected_price,
):
    ledger = ReplayFillLedger((_fill(side),))

    entry = ledger.entry_for(_candidate(side), ReplayMode.COUNTERFACTUAL)

    assert entry.price == expected_price
    assert entry.provenance is ReplayPriceProvenance.EXECUTABLE_ESTIMATE
    assert entry.historical_position_id is None


@pytest.mark.parametrize(
    ('side', 'expected_price'),
    [('BUY', 100.2), ('SELL', 99.8)],
)
def test_live_validation_missing_fill_is_explicit_contractual_fallback(
    side,
    expected_price,
):
    entry = ReplayFillLedger().entry_for(
        _candidate(side),
        ReplayMode.LIVE_VALIDATION,
    )

    assert entry.price == expected_price
    assert entry.provenance is (
        ReplayPriceProvenance.VALIDATION_CONTRACTUAL_FALLBACK
    )


@pytest.mark.parametrize('side', ['BUY', 'SELL'])
def test_live_validation_prioritizes_recorded_broker_exit_fill(side):
    ledger = ReplayFillLedger((_fill(side),))

    execution, provenance = ledger.close_execution_for(
        historical_position_id=f'position-{side}',
        replay_position_id='replay-position',
        mode=ReplayMode.LIVE_VALIDATION,
    )

    assert provenance is ReplayPriceProvenance.BROKER_HISTORICAL_FILL
    assert execution.executed_exit_price == _fill(side).exit_price
    assert execution.position_id == 'replay-position'
    assert execution.broker_response['historical_position_id'] == (
        f'position-{side}'
    )


def test_live_validation_exposes_historical_fill_timing_without_counterfactual_leakage():
    fill = _fill('BUY')
    ledger = ReplayFillLedger((fill,))

    assert ledger.historical_fill_for_position(
        'position-BUY',
        ReplayMode.LIVE_VALIDATION,
    ) == fill
    assert ledger.historical_fill_for_position(
        'position-BUY',
        ReplayMode.COUNTERFACTUAL,
    ) is None


def test_missing_historical_exit_fill_has_explicit_fallback_provenance():
    ledger = ReplayFillLedger((_fill('BUY', with_exit=False),))

    execution, provenance = ledger.close_execution_for(
        historical_position_id='position-BUY',
        replay_position_id='replay-position',
        mode=ReplayMode.LIVE_VALIDATION,
    )

    assert execution is None
    assert provenance is (
        ReplayPriceProvenance.VALIDATION_CONTRACTUAL_FALLBACK
    )


@pytest.mark.parametrize(
    ('side', 'entry_price', 'exit_price'),
    [('BUY', 100.2, 101.0), ('SELL', 99.8, 99.0)],
)
def test_net_pnl_deducts_explicit_cost_once_without_second_spread(
    side,
    entry_price,
    exit_price,
):
    pnl = calculate_position_pnl(
        side=side,
        amount=1_000.0,
        entry_price=entry_price,
        exit_price=exit_price,
        explicit_cost=2.0,
        explicit_cost_percent=0.2,
    )

    assert pnl.net_pnl == pytest.approx(pnl.gross_pnl - 2.0)
    assert pnl.net_pnl_percent == pytest.approx(
        pnl.gross_pnl_percent - 0.2
    )
    assert REPLAY_PRICING_CONTRACT_VERSION == 'replay_pricing_v2'


def test_duplicate_historical_fill_identity_fails_closed():
    fill = _fill('BUY')
    with pytest.raises(ValueError, match='Duplicate historical candidate'):
        ReplayFillLedger((fill, fill))


def test_historical_exit_before_entry_fails_closed():
    fill = _fill('BUY')
    invalid = HistoricalReplayFill(
        **{
            **fill.__dict__,
            'exit_at': fill.entry_at - timedelta(seconds=1),
        }
    )

    with pytest.raises(ValueError, match='cannot precede'):
        ReplayFillLedger((invalid,))


@pytest.mark.parametrize('invalid_price', [float('nan'), float('inf')])
def test_non_finite_historical_fill_prices_fail_closed(invalid_price):
    fill = _fill('BUY')
    invalid = HistoricalReplayFill(
        **{
            **fill.__dict__,
            'entry_price': invalid_price,
        }
    )

    with pytest.raises(ValueError, match='entry fill must be positive'):
        ReplayFillLedger((invalid,))


def test_fill_ledger_is_built_from_explicit_canonical_broker_fills():
    records = [
        {
            'event_type': 'position_opened',
            'timestamp': NOW.isoformat(),
            'payload': {
                'position_id': 'position-BUY',
                'candidate': {'candidate_id': 'candidate-BUY'},
                'position': {
                    'position_id': 'position-BUY',
                    'opened_at': NOW.isoformat(),
                    'broker_entry_fill_price': 100.35,
                    'entry_price_source': 'broker_fill',
                },
                'broker_entry_fill_price': 100.35,
            },
        },
        {
            'event_type': 'position_close_confirmed',
            'timestamp': (NOW + timedelta(minutes=10)).isoformat(),
            'payload': {
                'position_id': 'position-BUY',
                'close_order_id': 'close-BUY',
                'closed_position': {
                    'position_id': 'position-BUY',
                    'broker_exit_fill_price': 101.10,
                    'closed_at': (NOW + timedelta(minutes=10)).isoformat(),
                    'close_reason': 'take_profit',
                },
            },
        },
    ]

    extraction = historical_fills_from_journal_records(records)
    entry = extraction.ledger().entry_for(
        _candidate('BUY'),
        ReplayMode.LIVE_VALIDATION,
    )

    assert extraction.position_open_events == 1
    assert extraction.broker_entry_fills == 1
    assert extraction.broker_exit_fills == 1
    assert extraction.missing_candidate_ids == 0
    assert entry.price == 100.35
    assert entry.provenance is ReplayPriceProvenance.BROKER_HISTORICAL_FILL
    assert extraction.fills[0].close_reason == 'take_profit'


def test_explicit_broker_execution_event_is_a_valid_exit_fill_source():
    records = [
        {
            'event_type': 'position_opened',
            'timestamp': NOW.isoformat(),
            'payload': {
                'position_id': 'position-BUY',
                'candidate': {'candidate_id': 'candidate-BUY'},
                'broker_entry_fill_price': 100.35,
                'position': {'opened_at': NOW.isoformat()},
            },
        },
        {
            'event_type': 'broker_close_fill_resolved',
            'timestamp': (NOW + timedelta(minutes=10)).isoformat(),
            'payload': {
                'broker_execution': {
                    'position_id': 'position-BUY',
                    'executed_exit_price': 101.10,
                    'executed_at': (
                        NOW + timedelta(minutes=10)
                    ).isoformat(),
                    'close_order_id': 'close-BUY',
                },
            },
        },
    ]

    extraction = historical_fills_from_journal_records(records)
    fill = extraction.fills[0]

    assert fill.exit_price == 101.10
    assert fill.exit_at == NOW + timedelta(minutes=10)
    assert fill.close_order_id == 'close-BUY'


def test_legacy_ambiguous_price_is_never_silently_promoted_to_broker_fill():
    extraction = historical_fills_from_journal_records(
        [
            {
                'event_type': 'position_opened',
                'timestamp': NOW.isoformat(),
                'payload': {
                    'position_id': 'legacy-position',
                    'position': {
                        'position_id': 'legacy-position',
                        'entry_price': 100.0,
                        'opened_at': NOW.isoformat(),
                    },
                    'candidate': {'symbol': 'AMD'},
                },
            }
        ]
    )

    assert extraction.fills == ()
    assert extraction.missing_candidate_ids == 1
    assert extraction.ambiguous_legacy_entry_prices == 1


def test_non_finite_journal_price_is_not_an_explicit_broker_fill():
    extraction = historical_fills_from_journal_records(
        [
            {
                'event_type': 'position_opened',
                'timestamp': NOW.isoformat(),
                'payload': {
                    'position_id': 'invalid-position',
                    'broker_entry_fill_price': float('inf'),
                    'position': {'opened_at': NOW.isoformat()},
                    'candidate': {'candidate_id': 'invalid-candidate'},
                },
            }
        ]
    )

    assert extraction.fills == ()
    assert extraction.broker_entry_fills == 0
