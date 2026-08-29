from app.brokers.etoro.get_rate_governor import EtoroGetRateGovernor


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def test_rate_governor_holds_requests_below_rolling_window_limit():
    clock = FakeClock()
    governor = EtoroGetRateGovernor(
        max_requests=3,
        window_seconds=60.0,
        clock=clock.now,
        sleeper=clock.sleep,
    )

    governor.acquire()
    governor.acquire()
    governor.acquire()
    assert clock.value == 0.0

    governor.acquire()

    assert clock.value == 60.0
    assert clock.sleeps == [60.0]
    snapshot = governor.snapshot()
    assert snapshot['requests_in_window'] == 1
    assert snapshot['max_requests'] == 3


def test_global_cooldown_blocks_all_following_gets_until_due():
    clock = FakeClock()
    governor = EtoroGetRateGovernor(
        max_requests=10,
        window_seconds=60.0,
        clock=clock.now,
        sleeper=clock.sleep,
    )

    governor.acquire()
    governor.defer(17.0)
    governor.acquire()

    assert clock.value == 17.0
    assert clock.sleeps == [17.0]
    assert governor.snapshot()['cooldown_remaining_seconds'] == 0.0
