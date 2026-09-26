"""Research integration must not alter the active V3 trading trace."""

import ast
from datetime import timedelta
from pathlib import Path

from app.journal.serialization import serialize_value
from app.market.models import MarketSnapshot
from app.market_data.models import MarketDataEvent, MarketDataSource
from tests.v3.test_runtime import NOW
from tests.v3.test_runtime_equity_authority import (
    decision_batch,
    runtime_for_test,
    trailing_inventory,
)


class RecordingResearchSidecar:
    sampling_cadence_minutes = 5

    def __init__(self):
        self.accepted_snapshots = []
        self.states = []

    def observe_accepted_snapshot(self, snapshot, *, source):
        self.accepted_snapshots.append((snapshot, source))

    def emit_boundary(self, *, symbols, state_at, session_decisions):
        self.states.extend(
            {"symbol": symbol, "state_at": state_at,
             "session_decision": session_decisions.get(symbol)}
            for symbol in symbols
        )


def _run(runtime):
    trailing_inventory(runtime)
    for index, (seconds, price) in enumerate(
        ((5, 100.0), (20, 100.1), (40, 100.2), (65, 100.3))
    ):
        observed_at = NOW + timedelta(seconds=seconds)
        snapshot = MarketSnapshot(
            symbol="AAPL", bid=price - 0.01, ask=price + 0.01, last=price,
            timestamp=observed_at, received_at=observed_at,
        )
        event = MarketDataEvent(
            symbol="AAPL", source=MarketDataSource.WEBSOCKET,
            received_at=observed_at, snapshot=snapshot,
            message_id=f"m-{index}", connection_id="c-1", price_changed=True,
        )
        runtime._handle_event(event, observed_at)
        runtime._finalize_clocked_candles(observed_at)
        runtime._emit_due_research_state(observed_at)

    # Exercise the real inventory planner with the same causal decision window.
    # No subsequent quote dispatches these intents to a broker.
    runtime._process_decision_window(decision_batch())
    return serialize_value({
        "candles": runtime.candle_journal.events,
        "features": runtime.latest_features,
        "inventories": runtime.book.inventories,
        "intents": runtime.intent_book.snapshot(),
        "trade_journal": runtime.trade_journal.events,
        "market_journal": runtime.market_journal.events,
        "metrics": runtime.metrics,
        "pending_actions": sorted(runtime.executor._pending_actions),
    })


def test_same_market_data_sequence_has_identical_trading_trace(tmp_path):
    disabled = runtime_for_test(tmp_path / "disabled")
    enabled = runtime_for_test(tmp_path / "enabled")
    enabled.research_pipeline = RecordingResearchSidecar()

    disabled_trace = _run(disabled)
    enabled_trace = _run(enabled)

    assert enabled_trace == disabled_trace
    assert enabled_trace["candles"]
    assert enabled_trace["intents"]  # real reduce-only inventory exit
    assert len(enabled.research_pipeline.accepted_snapshots) == 4
    assert len(enabled.research_pipeline.states) == 1


def test_core_market_and_trading_packages_do_not_import_research_runtime():
    repository = Path(__file__).resolve().parents[2]
    forbidden_roots = (
        repository / 'app' / 'market_data',
        repository / 'app' / 'brokers',
        repository / 'app' / 'strategies',
        repository / 'app' / 'execution',
        repository / 'app' / 'risk',
    )
    offenders = []
    for root in forbidden_roots:
        for source in root.rglob('*.py'):
            tree = ast.parse(source.read_text(encoding='utf-8'))
            imports_research = any(
                (
                    isinstance(node, ast.ImportFrom)
                    and (node.module or '').startswith('app.research')
                )
                or (
                    isinstance(node, ast.Import)
                    and any(
                        alias.name.startswith('app.research')
                        for alias in node.names
                    )
                )
                for node in ast.walk(tree)
            )
            if imports_research:
                offenders.append(source.relative_to(repository).as_posix())

    assert offenders == []



def test_fixed_cadence_scheduler_does_not_require_a_quote_or_candle(tmp_path):
    runtime = runtime_for_test(tmp_path)
    runtime.research_pipeline = RecordingResearchSidecar()
    due_at = NOW + timedelta(minutes=5, seconds=1.1)

    runtime._emit_due_research_state(due_at)
    runtime._emit_due_research_state(due_at + timedelta(milliseconds=100))

    assert len(runtime.research_pipeline.states) == 1
    assert runtime.research_pipeline.states[0]["symbol"] == "AAPL"
    assert runtime.research_pipeline.states[0]["state_at"] == NOW + timedelta(minutes=5)
