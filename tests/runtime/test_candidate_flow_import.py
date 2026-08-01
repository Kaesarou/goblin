from app.runtime import candidate_flow
from app.runtime.async_candidate_execution import (
    AsyncCandidateExecutionCoordinator,
)


def test_runtime_exposes_only_async_candidate_execution_path():
    assert AsyncCandidateExecutionCoordinator is not None
    assert not hasattr(candidate_flow, 'execute_ranked_candidates')
