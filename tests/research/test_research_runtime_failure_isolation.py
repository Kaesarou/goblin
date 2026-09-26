import logging
from datetime import UTC, datetime, timedelta

from app.v3.runtime import GoblinV3Runtime
from app.runtime.runtime_policy import CANDLE_CLOCK_GRACE_SECONDS

BOUNDARY = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)


class FailingResearchPipeline:
    sampling_cadence_minutes = 5

    def __init__(self) -> None:
        self.calls = 0

    def emit_boundary(self, **_kwargs):
        self.calls += 1
        raise RuntimeError('unexpected research failure')


class ResearchIsolationFlow(GoblinV3Runtime):
    def __init__(self) -> None:
        self.research_pipeline = FailingResearchPipeline()
        self.symbols = ['AAPL']
        self.session_decisions = {}
        self._last_research_boundary = None
        self.metrics = {"errors": 0}


def test_unexpected_research_boundary_failure_never_escapes_runtime(caplog):
    flow = ResearchIsolationFlow()
    now = BOUNDARY + timedelta(seconds=CANDLE_CLOCK_GRACE_SECONDS + 0.1)

    with caplog.at_level(logging.ERROR):
        flow._emit_due_research_state(now)

    assert flow.research_pipeline.calls == 1
    assert flow._last_research_boundary == BOUNDARY
    assert 'V3 research boundary failed' in caplog.text
    assert flow.metrics["errors"] == 1

    flow._emit_due_research_state(now + timedelta(milliseconds=100))
    assert flow.research_pipeline.calls == 1
