from datetime import datetime, timezone

import pytest

from app.brokers.base import BrokerClient, BrokerCloseExecution, OpenPositionResult
from app.market.models import MarketSnapshot
from app.runtime.broker_queries import (
    UnknownOrderLookup,
    get_fresh_position_open_states,
    reconcile_positions_and_close_executions,
    resolve_unknown_open_order,
)


class PortfolioBroker(BrokerClient):
    def __init__(self):
        self.portfolio_calls = 0
        self.order_calls = 0

    def get_portfolio(self):
        self.portfolio_calls += 1
        return {
            'clientPortfolio': {
                'positions': [
                    {'positionID': 'p-1', 'isOpen': True},
                    {'positionID': 'p-2', 'isOpen': False},
                ]
            }
        }

    def get_order_details(self, order_id: str):
        self.order_calls += 1
        return {
            'status': {'id': 1, 'name': 'Executed', 'errorCode': 0},
            'positionExecutions': [
                {
                    'positionId': 'p-new',
                    'investedAmountCurrency': 100.0,
                    'openingData': {
                        'avgPrice': 101.5,
                        'units': 0.9852216748768473,
                    },
                }
            ],
        }

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        raise NotImplementedError

    def get_market_snapshots(self, symbols: list[str]):
        raise NotImplementedError

    def get_account_equity(self) -> float:
        return 1000.0

    def open_position(self, symbol, side, amount, stop_loss, take_profit):
        return OpenPositionResult(position_id='unused')

    def close_position(self, position_id: str) -> None:
        return None

    def is_position_open(self, position_id: str) -> bool:
        raise AssertionError('individual position lookup must not be used')


def test_fresh_position_states_use_one_portfolio_snapshot():
    broker = PortfolioBroker()

    states = get_fresh_position_open_states(broker, ['p-1', 'p-2', 'p-3'])

    assert states == {'p-1': True, 'p-2': False, 'p-3': False}
    assert broker.portfolio_calls == 1


def test_unknown_order_recovers_from_order_lookup_before_portfolio():
    broker = PortfolioBroker()
    lookup = UnknownOrderLookup(
        order_id='o-1',
        reference_id='r-1',
        symbol='BTC',
        side='BUY',
        amount=100.0,
        submitted_at=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
    )

    resolution = resolve_unknown_open_order(broker, lookup)

    assert resolution.status == 'confirmed'
    assert resolution.matched_by == 'order_lookup'
    assert resolution.result is not None
    assert resolution.result.position_id == 'p-new'
    assert resolution.result.executed_entry_price == pytest.approx(101.5)
    assert resolution.result.executed_units == pytest.approx(0.9852216748768473)
    assert resolution.result.executed_notional == pytest.approx(100.0)
    assert broker.order_calls == 1
    assert broker.portfolio_calls == 0


class CloseExecutionBroker(PortfolioBroker):
    def __init__(self, execution):
        super().__init__()
        self.execution = execution
        self.close_execution_calls = []

    def get_close_execution(self, close_order_id: str, position_id: str):
        self.close_execution_calls.append((close_order_id, position_id))
        if isinstance(self.execution, Exception):
            raise self.execution
        return self.execution


def test_reconciliation_fetches_fill_only_after_portfolio_absence():
    executed_at = datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)
    fill = BrokerCloseExecution(
        position_id='p-2',
        close_order_id='close-2',
        executed_exit_price=98.75,
        executed_at=executed_at,
        units=10.0,
        conversion_rate=1.0,
        amount=987.5,
        broker_response={'positions': []},
    )
    broker = CloseExecutionBroker(fill)

    result = reconcile_positions_and_close_executions(
        broker,
        ['p-1', 'p-2'],
        {'p-1': 'close-1', 'p-2': 'close-2'},
    )

    assert result.open_states == {'p-1': True, 'p-2': False}
    assert result.close_executions == {'p-2': fill}
    assert result.close_execution_unavailable == {}
    assert broker.close_execution_calls == [('close-2', 'p-2')]


def test_reconciliation_records_explicit_broker_fill_absence():
    broker = CloseExecutionBroker(None)

    result = reconcile_positions_and_close_executions(
        broker,
        ['p-2'],
        {'p-2': 'close-2'},
    )

    assert result.close_executions == {}
    assert result.close_execution_unavailable == {
        'p-2': 'broker_fill_not_returned'
    }


def test_reconciliation_keeps_portfolio_confirmation_when_fill_lookup_fails():
    broker = CloseExecutionBroker(RuntimeError('temporary lookup failure'))

    result = reconcile_positions_and_close_executions(
        broker,
        ['p-2'],
        {'p-2': 'close-2'},
    )

    assert result.open_states == {'p-2': False}
    assert result.close_executions == {}
    assert result.close_execution_unavailable == {
        'p-2': 'lookup_failed:temporary lookup failure'
    }
