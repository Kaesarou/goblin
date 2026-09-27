from app.runtime import candidate_flow


def test_historical_candidate_flow_does_not_expose_broker_execution():
    assert callable(candidate_flow.select_trade_candidates_with_strategy_profile)
    assert not hasattr(candidate_flow, 'execute_ranked_candidates')
