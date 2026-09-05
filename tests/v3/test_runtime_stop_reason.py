import pytest

from app.v3.runtime import GoblinV3Runtime


class RecordingJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def write(self, event_type, payload):
        self.events.append((event_type, payload))


class InterruptingFeed:
    def next_event(self, timeout_seconds):
        raise KeyboardInterrupt


def test_keyboard_interrupt_sets_explicit_interrupted_stop_reason():
    runtime = object.__new__(GoblinV3Runtime)
    runtime._started = True
    runtime._stop_requested = False
    runtime.stop_reason = None
    runtime.loop_id = 0
    runtime.trade_journal = RecordingJournal()
    runtime.live_market_data = InterruptingFeed()

    runtime._refresh_sessions = lambda now: None
    runtime._drain_broker_tasks = lambda: None
    runtime._maybe_schedule_equity_refresh = lambda monotonic_now: None
    runtime._run_position_fallback_if_due = lambda now, monotonic_now: None
    runtime._schedule_close_confirmation_checks = lambda monotonic_now: None
    runtime.stop = lambda: setattr(runtime, "_started", False)

    runtime.run(timeout_seconds=0.0)

    assert runtime.stop_reason == "interrupted"
    assert runtime._started is False
    assert [event for event, _ in runtime.trade_journal.events] == [
        "v3_runtime_interrupted"
    ]


@pytest.mark.parametrize("failure_stage", ["startup", "loop", "stop"])
def test_unexpected_error_is_reraised_with_error_stop_reason(failure_stage):
    runtime = object.__new__(GoblinV3Runtime)
    runtime._started = failure_stage != "startup"
    runtime._stop_requested = failure_stage == "stop"
    runtime.stop_reason = None
    runtime.loop_id = 0
    def fail(*args, **kwargs):
        raise RuntimeError("unexpected failure")
    runtime.startup = fail
    runtime._refresh_sessions = fail
    runtime.stop = fail if failure_stage == "stop" else lambda: None
    with pytest.raises(RuntimeError, match="unexpected failure"):
        runtime.run()
    assert runtime.stop_reason == "error"
