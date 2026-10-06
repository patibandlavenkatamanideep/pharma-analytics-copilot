"""The real multi-turn evaluation runner with a metered fake transport."""
import json
import sys
import pytest
from tests.conftest import needs_db
from tests.unit.test_eval_budget import ev

pytestmark = [pytest.mark.integration, needs_db]


def test_first_turn_violation_stops_remaining_turns_and_questions(ev, tmp_path, monkeypatch):
    from app.config import get_settings
    from tests.unit.test_live_adapter_contract import make_planner, FakeResponse, FakeUsage, valid_plan_block
    planner = make_planner([FakeResponse([valid_plan_block()], FakeUsage(10**7, 24))] * 3)
    monkeypatch.setattr('app.llm.planner.build_planner', lambda: planner)
    question = {'question': 'Total volume this quarter', 'expect': {'type': 'plan', 'plan': {'metric': 'paid_pack_units'}}}
    path = tmp_path / 'terminal.yaml'
    path.write_text(json.dumps({'version': 'probe', 'status': 'regression', 'questions': [
        {'id': 'multi', 'principal': 'exec', 'turns': [question, question]},
        {'id': 'last', 'principal': 'exec', **question}]}))
    monkeypatch.setattr(ev, 'ROOT', tmp_path)
    monkeypatch.setattr(ev, 'RUNS', tmp_path / 'runs')
    monkeypatch.setenv('PAC_LLM_PROVIDER', 'offline')
    monkeypatch.setattr(sys, 'argv', ['run_evals.py', '--provider', 'bedrock', '--questions', str(path),
                                    '--max-input-tokens', str(10**9), '--max-output-tokens', str(10**9)])
    try:
        assert ev.main() == 1
        record = json.loads(next((tmp_path / 'runs').glob('*.json')).read_text())
        assert record['not_run_budget_exhausted'] == ['multi.2', 'last']
        assert len(planner._client.messages.requests) == record['budget']['model_calls'] == 1
        assert record['performance']['usage']['input_tokens'] == 10**7
        assert record['performance']['usage']['output_tokens'] == 24
    finally:
        monkeypatch.setenv('PAC_LLM_PROVIDER', 'offline')
        get_settings.cache_clear()


def test_metered_success_metadata_survives_graph_state_validation(ev, exec_user):
    from app.pipeline import Pipeline
    from tests.unit.test_live_adapter_contract import make_planner, FakeResponse, FakeUsage, valid_plan_block
    pipe = Pipeline(make_planner([FakeResponse([valid_plan_block()], FakeUsage(1234, 12))]))
    pipe.spend = ev.Budget(10**6, 10**6)
    result = pipe.ask(exec_user, 'Top 5 accounts by pack units last quarter')
    assert result.planning['usage']['input_tokens'] == 1234
    assert result.status != 'error'
    assert result.planning['attempts'][0]['charged'] == [1234, 12]
