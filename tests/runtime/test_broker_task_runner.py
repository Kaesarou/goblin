from threading import Event

from app.runtime.broker_task_runner import BrokerTaskLane, BrokerTaskRunner


def test_broker_task_runner_returns_success_completion():
    runner = BrokerTaskRunner()
    gate = Event()

    def succeed():
        gate.wait(1)
        return 42

    runner.submit(
        kind='success',
        task_id='success-1',
        context={'value': 1},
        operation=succeed,
    )
    gate.set()
    runner.close(wait=True)
    completions = runner.drain()

    assert len(completions) == 1
    assert completions[0].kind == 'success'
    assert completions[0].lane == BrokerTaskLane.STANDARD
    assert completions[0].value == 42
    assert completions[0].error is None


def test_broker_task_runner_surfaces_operation_error():
    runner = BrokerTaskRunner()

    def fail():
        raise RuntimeError('boom')

    runner.submit(kind='failure', task_id='failure-1', operation=fail)
    runner.close(wait=True)
    completions = runner.drain()

    assert len(completions) == 1
    assert completions[0].kind == 'failure'
    assert completions[0].lane == BrokerTaskLane.STANDARD
    assert isinstance(completions[0].error, RuntimeError)
    assert str(completions[0].error) == 'boom'


def test_close_lane_is_not_blocked_by_slow_standard_broker_work():
    runner = BrokerTaskRunner()
    standard_started = Event()
    release_standard = Event()
    close_finished = Event()

    def slow_open_confirmation():
        standard_started.set()
        assert release_standard.wait(2)
        return 'open-confirmed'

    def close_position():
        close_finished.set()
        return 'closed'

    runner.submit(
        kind='open_order',
        task_id='open-order-1',
        operation=slow_open_confirmation,
    )
    assert standard_started.wait(1)

    runner.submit(
        kind='close_position',
        task_id='close-position-1',
        operation=close_position,
    )

    assert close_finished.wait(1)
    release_standard.set()
    runner.close(wait=True)
    completions = {item.task_id: item for item in runner.drain()}

    assert completions['close-position-1'].lane == BrokerTaskLane.CLOSE
    assert completions['close-position-1'].value == 'closed'
    assert completions['open-order-1'].lane == BrokerTaskLane.STANDARD
    assert completions['open-order-1'].value == 'open-confirmed'


def test_close_lane_is_not_blocked_by_slow_confirmation_query():
    runner = BrokerTaskRunner()
    query_started = Event()
    release_query = Event()
    close_finished = Event()

    def slow_confirmation_lookup():
        query_started.set()
        assert release_query.wait(2)
        return 'confirmed'

    def close_position():
        close_finished.set()
        return 'closed'

    runner.submit(
        kind='v3_close_execution_lookup',
        task_id='confirm-1',
        operation=slow_confirmation_lookup,
    )
    assert query_started.wait(1)

    runner.submit(
        kind='close_position',
        task_id='close-position-1',
        operation=close_position,
    )

    assert close_finished.wait(1)
    release_query.set()
    runner.close(wait=True)
    completions = {item.task_id: item for item in runner.drain()}

    assert completions['confirm-1'].lane == BrokerTaskLane.QUERY
    assert completions['confirm-1'].value == 'confirmed'
    assert completions['close-position-1'].lane == BrokerTaskLane.CLOSE
    assert completions['close-position-1'].value == 'closed'
