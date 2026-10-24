from datetime import datetime, timezone
import pytest
from app.evidence import Bundle


@pytest.mark.parametrize('frames,recent,kind', [(True, False, 'code_fix'), (True, True, 'both'), (False, False, 'mitigation')])
def test_remediation_hint_branches(frames, recent, kind):
    from app.router import remediation_hint
    b = Bundle('prd', 'payment')
    b.code_refs = {'frames': [{'file': '/app/PaymentService.py', 'line': 42, 'symbol': 'pay', 'language': 'python'}] if frames else []}
    b.deployment = {'images': ['new'], 'current_revision': 2, 'recent_rollout_at': datetime.now(timezone.utc).isoformat() if recent else '2000-01-01T00:00:00Z', 'revisions': [{'revision': 1, 'images': ['old']}]}
    b.events = [{'reason': 'OOMKilled'}]
    out = remediation_hint(b)
    assert out['kind'] == kind and out['source'] == 'rules'
    assert out['repro'] is None


def test_remediation_refs_reject_forged_locations():
    from app.remediation import remediation_errors
    evidence = {'E8_code_refs': {'frames': [{'file': '/app/x.py', 'line': 2, 'symbol': 'f'}]}}
    good = {'kind': 'code_fix', 'why': 'observed stack', 'code_refs': [{'file': '/app/x.py', 'line': 2, 'symbol': 'f'}], 'repro': None}
    assert remediation_errors(good, evidence) == []
    assert remediation_errors({**good, 'code_refs': [{'file': '/app/x.py', 'line': 999, 'symbol': 'f'}]}, evidence)


def test_schemas_and_workflow_prompt_include_remediation():
    from app.llm import AGENT_SCHEMA
    from app.agent_groups import GROUP_SCHEMA, GROUP_OUTPUT_RULES
    from app.workflows import OUTPUT_RULES
    assert 'remediation' in AGENT_SCHEMA['properties']
    assert 'remediation' in GROUP_SCHEMA['properties']
    assert 'E8_code_refs' in GROUP_OUTPUT_RULES and 'remediation' in OUTPUT_RULES


def test_simple_verifier_rejects_fabricated_remediation_refs():
    from app.workflows import build_simple_graph, AgentCall
    message = {'summary': 'bad rollout', 'next_action': 'rollout_undo', 'confidence': 0.99,
               'remediation': {'kind': 'both', 'why': 'stack', 'code_refs': [{'file': '/app/forged.py', 'line': 9, 'symbol': 'f'}], 'repro': None}}
    state = {'incident': {'workload': 'payment'}, 'evidence': {'E8_code_refs': {'frames': []}}, 'features': {},
             'has_previous_revision': True, 'rag': [], 'allowlist': ['rollout_undo']}
    out = build_simple_graph(lambda *a: AgentCall(True, message, 1)).invoke(state)
    assert out['outcome'] == 'handoff'
    assert 'remediation' in out['reason']


def test_code_refs_are_not_lost_to_prompt_truncation():
    from app.workflows import _ctx
    from app.agent_groups import EvidenceStore, ALL_E
    evidence = {'E7_error_logs': ['x' * 14000], 'E8_code_refs': {'frames': [{'file': '/app/important.py', 'line': 42, 'symbol': 'pay'}]}}
    text = _ctx({'incident': {}, 'features': {}, 'evidence': evidence})
    assert '/app/important.py' in text
    assert '/app/important.py' in EvidenceStore(evidence).view(ALL_E)
