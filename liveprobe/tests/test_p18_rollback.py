"""P18 rollback authority is bound to environment as well as revision."""
import json
import pytest
from app import rollback_controller as rc, rollback_grant as rg
from test_rollback_controller import resources, SAFE as GOOD
from conftest import mk_rs
BAD = 'registry.example/payment@sha256:' + 'b' * 64


def test_namespace_is_signed_and_cannot_be_replayed():
    key = b'x' * 48
    grant = rg.issue(key, namespace='prd', incident_id=1, action_id=2, name='payment', expected_revision=2, expected_source='payment-abc', now=100)
    rg.verify(key, grant, now=101)
    with pytest.raises(rg.InvalidGrant):
        rg.verify(key, {**grant, 'namespace': 'dev'}, now=101)


def test_approved_images_are_namespace_keyed(monkeypatch):
    env = {'ROLLBACK_NAMESPACES': 'dev,stg,prd', 'ROLLBACK_APPROVED_IMAGES': json.dumps({'prd/payment': [GOOD], 'dev/payment': [BAD]})}
    assert rc.trusted_images(env) == {'prd/payment': [GOOD], 'dev/payment': [BAD]}
    with pytest.raises(rc.Rejected):
        rc.trusted_images({**env, 'ROLLBACK_APPROVED_IMAGES': json.dumps({'outside/payment': [GOOD]})})


def test_plan_picks_latest_approved_previous_image():
    dep = resources()[0]
    dep['metadata']['name'] = 'payment'
    dep['spec']['template']['metadata']['labels']['app'] = 'payment'
    dep['spec']['template']['spec']['containers'][0]['name'] = 'payment'
    prev = mk_rs('payment-old', 'payment', 1, GOOD)
    prev['metadata']['ownerReferences'][0].update(uid=dep['metadata']['uid'], controller=True)
    prev['metadata']['ownerReferences'][0]['name'] = 'payment'
    prev['spec']['template']['spec']['containers'][0]['name'] = 'payment'
    ops, revision, name = rc.plan_undo(dep, [prev], 'payment', [GOOD])
    assert revision == 1 and name == 'payment-old'
    assert ops[-1]['value'] == GOOD


def test_admission_is_namespace_bound(monkeypatch):
    import copy
    from test_rollback_controller import review
    monkeypatch.setenv('ROLLBACK_NAMESPACES', 'dev,stg,prd')
    dep, _ = resources()
    new = copy.deepcopy(dep)
    new['spec']['template']['spec']['containers'][0]['image'] = GOOD
    request = review(dep, new)
    request['request']['namespace'] = 'prd'
    assert rc.validate_admission(request, {'prd/inventory': [GOOD]})['response']['allowed']
    request['request']['namespace'] = 'dev'
    assert not rc.validate_admission(request, {'prd/inventory': [GOOD]})['response']['allowed']


def test_plan_skips_unapproved_newer_image_but_not_non_image_changes():
    import copy
    dep, old = resources()
    dep['metadata']['annotations']['deployment.kubernetes.io/revision'] = '4'
    mid = copy.deepcopy(old)
    mid['metadata']['name'] = 'inventory-mid'
    mid['metadata']['annotations']['deployment.kubernetes.io/revision'] = '3'
    mid['spec']['template']['spec']['containers'][0]['image'] = BAD
    assert rc.plan_undo(dep, [old, mid], 'inventory', [GOOD])[2] == old['metadata']['name']
