"""P18 release-time receipt and rollout ownership regressions; no external writes."""
import copy
from datetime import timedelta
from types import SimpleNamespace as N

import pytest

from app import db
from app.change_models import ChangeRequest
from app.changes import ChangeError, ChangeService
from app.models import utcnow
from test_p18_changes import setup, built


DIGEST = 'sha256:' + 'a' * 64
IMAGE = 'local/payment@' + DIGEST


def rollout_fixture():
    dep = {'metadata': {'uid': 'approved-deployment', 'generation': 4},
           'spec': {'replicas': 1, 'template': {'spec': {'containers': [{'name': 'payment', 'image': IMAGE}]}}},
           'status': {'observedGeneration': 4, 'replicas': 1, 'updatedReplicas': 1, 'availableReplicas': 1}}
    rs = {'metadata': {'uid': 'current-rs', 'ownerReferences': [{'kind': 'Deployment', 'uid': 'approved-deployment', 'controller': True}]},
          'spec': {'template': copy.deepcopy(dep['spec']['template'])}}
    pod = {'metadata': {'ownerReferences': [{'kind': 'ReplicaSet', 'uid': 'current-rs', 'controller': True}]},
           'spec': {'containers': [{'name': 'payment', 'image': IMAGE}]},
           'status': {'conditions': [{'type': 'Ready', 'status': 'True'}],
                      'containerStatuses': [{'name': 'payment', 'image': IMAGE, 'imageID': 'containerd://' + DIGEST, 'ready': True}]}}
    kube = N(get_deployment=lambda *_: copy.deepcopy(dep), list_replicasets=lambda *_: [copy.deepcopy(rs)],
             list_pods=lambda *_: [copy.deepcopy(pod)])
    service = ChangeService(N(change_rollout_timeout_seconds=0, change_observation_seconds=1), N(), None, kube,
                            sleep=lambda _: None, logs=lambda *_: 0)
    return N(dep=dep, rs=rs, pod=pod, service=service)


@pytest.mark.parametrize('mutation', ['uid', 'rs_owner', 'old_rs', 'pod_owner', 'pod_image', 'image_id', 'not_ready'])
def test_rollout_rejects_replacement_or_unrelated_artifact(mutation):
    x = rollout_fixture()
    if mutation == 'uid': x.dep['metadata']['uid'] = 'replacement-deployment'
    elif mutation == 'rs_owner': x.rs['metadata']['ownerReferences'][0]['uid'] = 'unrelated-deployment'
    elif mutation == 'old_rs': x.rs['spec']['template']['spec']['containers'][0]['image'] = 'local/payment:old'
    elif mutation == 'pod_owner': x.pod['metadata']['ownerReferences'][0]['uid'] = 'unrelated-rs'
    elif mutation == 'pod_image': x.pod['spec']['containers'][0]['image'] = 'local/payment:old'
    elif mutation == 'image_id': x.pod['status']['containerStatuses'][0]['imageID'] = 'containerd://sha256:' + 'b' * 64
    else: x.pod['status']['conditions'][0]['status'] = 'False'
    out = x.service.verify('prd', 'payment', IMAGE, None, expected_uid='approved-deployment')
    assert not out['ok']


def test_verified_rollout_is_owned_ready_and_exact_image():
    x = rollout_fixture()
    out = x.service.verify('prd', 'payment', IMAGE, None, expected_uid='approved-deployment')
    assert out['ok'] and out['health']['status'] == '통과'


def test_readiness_and_identity_are_rechecked_after_observation():
    x = rollout_fixture()
    x.service.sleep = lambda _: x.pod['status']['conditions'][0].update(status='False')
    out = x.service.verify('prd', 'payment', IMAGE, None, expected_uid='approved-deployment')
    assert not out['ok'] and out['health']['status'] == '실패'


def reviewed_production_receipt(x, cid):
    x.service.review(cid, {'repository': 'live-probe/payment', 'pr_number': 4,
        'pr_url': 'https://github.com/live-probe/payment/pull/4', 'head_sha': 'b' * 40,
        'state': 'MERGED', 'merge_sha': 'c' * 40, 'main_sha': 'c' * 40, 'review_completed': True,
        'merge_build': {'commit': 'c' * 40, 'image': 'local/payment:merged', 'image_id': 'sha256:' + 'c' * 64,
                        'source_tree': 'd' * 40, 'tests': {'total': 1, 'failed': 0}}})


def reviewed_production(x):
    c = built(x)
    with db.session() as s: s.get(ChangeRequest, c['id']).status = 'stg_verified'
    reviewed_production_receipt(x, c['id'])
    return c


def age_receipt(cid):
    with db.session() as s:
        row = s.get(ChangeRequest, cid); row.build = {**row.build, 'review_checked_at': (utcnow() - timedelta(seconds=31)).isoformat()}


def assert_blocked_without_writes(x, out):
    # Nothing was written, so the change returns to the last verified stage and can be re-reviewed.
    assert out['status'] == 'stg_verified'
    assert not x.release.calls
    entry = out['stages']['prd']
    assert entry['status'] == 'blocked' and entry['reverted'] is False
    assert entry['rollback']['status'] == '미수행'
    assert x.kube.deployments['prd']['spec']['template']['spec']['containers'][0]['image'] == 'local/payment:base'


def test_expired_merge_receipt_after_confirmation_causes_zero_forward_or_rollback_writes(setup):
    x = setup; c = reviewed_production(x)
    plan = x.service.plan(c['id'], 'prd')
    x.service.promote(c['id'], 'prd', plan['plan_sha256'], True)
    age_receipt(c['id'])
    out = x.service.execute_stage(c['id'], 'prd')
    assert_blocked_without_writes(x, out)
    with pytest.raises(ChangeError, match='github_merge_required'): x.service.plan(c['id'], 'prd')


def test_merge_receipt_is_rechecked_at_transport_write_boundary(setup):
    x = setup; c = reviewed_production(x)
    plan = x.service.plan(c['id'], 'prd')
    x.service.promote(c['id'], 'prd', plan['plan_sha256'], True)
    read = x.kube.get_deployment
    def delayed(ns, name):
        # The receipt expires while the executor is reading the target, after the pre-flight check.
        if ns == 'prd': age_receipt(c['id'])
        return read(ns, name)
    x.kube.get_deployment = delayed
    out = x.service.execute_stage(c['id'], 'prd')
    assert_blocked_without_writes(x, out)


def test_blocked_production_can_be_rereviewed_and_released(setup):
    x = setup; c = reviewed_production(x)
    plan = x.service.plan(c['id'], 'prd')
    x.service.promote(c['id'], 'prd', plan['plan_sha256'], True)
    age_receipt(c['id'])
    x.service.execute_stage(c['id'], 'prd')
    reviewed_production_receipt(x, c['id'])
    plan = x.service.plan(c['id'], 'prd')
    x.service.promote(c['id'], 'prd', plan['plan_sha256'], True)
    out = x.service.execute_stage(c['id'], 'prd')
    assert out['status'] == 'prd_verified' and len(x.release.calls) == 1
    assert out['stages']['prd']['verification']['health']['status'] == '통과'


def test_replaced_production_deployment_after_write_fails_and_is_not_rolled_back_blindly(setup):
    x = setup; c = reviewed_production(x)
    plan = x.service.plan(c['id'], 'prd')
    x.service.promote(c['id'], 'prd', plan['plan_sha256'], True)
    patch = x.release.patch_image
    def replaced(*args):
        patch(*args); x.kube.deployments['prd']['metadata']['uid'] = 'replacement'
    x.release.patch_image = replaced
    out = x.service.execute_stage(c['id'], 'prd')
    assert out['status'] == 'prd_failed' and out['stages']['prd']['reverted'] is False
    assert len(x.release.calls) == 1


def test_future_merge_receipt_is_not_fresh(setup):
    x = setup; c = reviewed_production(x)
    with db.session() as s:
        row = s.get(ChangeRequest, c['id']); row.build = {**row.build, 'review_checked_at': (utcnow() + timedelta(hours=1)).isoformat()}
    with pytest.raises(ChangeError, match='github_merge_required'): x.service.plan(c['id'], 'prd')
