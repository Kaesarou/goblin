from dataclasses import replace

from app.execution.candidate_readiness import CandidateReadiness
from app.execution.candidate_selector import (
    CandidateSelectionConfig,
    select_evaluated_trade_candidates,
)
from tests.execution.test_candidate_selector import (
    candidate,
    evaluated_candidate_with_profit,
)


def evaluated(
    symbol: str,
    score: float,
    readiness: CandidateReadiness,
    reason: str,
):
    base = evaluated_candidate_with_profit(candidate(symbol))
    return replace(
        base,
        candidate=replace(
            base.candidate,
            tp_probability=0.20,
            touch_probability=0.40,
            direction_probability=0.50,
            direction_edge=score / 1000,
        ),
        readiness=readiness,
        readiness_reason=reason,
    )


def test_hard_rejected_readiness_never_participates_in_top_n():
    rejected = evaluated(
        'REJECT',
        999.0,
        CandidateReadiness.REJECT,
        'invalid_trade_structure',
    )
    tradable = evaluated(
        'NOW',
        120.0,
        CandidateReadiness.TRADABLE_NOW,
        'entry_decision_required',
    )

    result = select_evaluated_trade_candidates(
        [rejected, tradable],
        CandidateSelectionConfig(top_n=1),
    )

    assert [
        item.candidate.symbol for item in result.selected_candidates
    ] == ['NOW']
    assert len(result.rejected_candidates) == 1
    assert result.rejected_candidates[0].reason == (
        'invalid_trade_structure'
    )
