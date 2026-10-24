"""KAN-42 safety policy, revision selection, deny + audit, DB action lock."""
import pytest
from sqlalchemy.exc import IntegrityError

from app import db
from app.config import AppConfig
from app.models import Action, AuditLog, Incident
from app.safety import Denied, check_action, effective_policy, execute, select_previous_revision
from app.rollback_grant import verify, InvalidGrant
from conftest import FakeKube, family1_kube, needs_db

NS = "liveprobe-target"
TARGET = f"{NS}/Deployment/inventory"


def test_non_allowlisted_and_outside_namespace_denied():
    cfg, k = AppConfig(), family1_kube()
    with pytest.raises(Denied, match="allowlist"):
        check_action(cfg, k, "delete_deployment", NS, "Deployment", "inventory")
    with pytest.raises(Denied, match="target namespace"):
        check_action(cfg, k, "rollout_undo", "liveprobe-system", "Deployment", "inventory")


def test_high_risk_cannot_be_overridden():
    assert effective_policy("HIGH_RISK", "auto") == "handoff"
    assert effective_policy("HIGH_RISK", "approval") == "handoff"
    assert effective_policy("SIMPLE", "auto") == "auto"
    assert effective_policy("SIMPLE", "suggest_only") == "suggest_only"


def test_previous_revision_is_different_template_and_owned():
    k = family1_kube()
    dep = k.get_deployment(NS, "inventory")
    prev = select_previous_revision(dep, k.list_replicasets(NS))
    assert prev["metadata"]["name"] == "inventory-aaa"
    assert select_previous_revision(dep, [k.replicasets[1]]) is None


def seed(s, *, level="SIMPLE", action_type="rollout_undo", target=TARGET, status="proposed"):
    i = Incident(fingerprint="f", namespace=NS, workload="inventory", severity_level=level,
                 title="test", status="open", signal_reasons=[])
    s.add(i)
    s.flush()
    a = Action(incident_id=i.id, type=action_type, target=target, params={}, policy_mode="auto", status=status)
    s.add(a)
    s.flush()
    return a.id


@pytest.mark.parametrize("level,action_type,target", [
    ("HIGH_RISK", "rollout_undo", TARGET),
    ("SIMPLE", "delete_deployment", TARGET),
    ("SIMPLE", "rollout_undo", "liveprobe-system/Deployment/inventory"),
])
@needs_db
def test_denials_persist_audit_and_never_patch(db_url, level, action_type, target):
    k = family1_kube()
    with db.session() as s:
        aid = seed(s, level=level, action_type=action_type, target=target)
    result = execute(db.session, AppConfig(), k, aid, "test")
    assert result.status == "denied" and not k.patches
    with db.session() as s:
        assert s.get(Action, aid).status == "denied"
        assert s.query(AuditLog).filter_by(event="action_denied").count() == 1


@needs_db
def test_only_approved_action_mints_bound_grant(db_url, monkeypatch):
    from app import rollback_grant
    key = b"a" * 48
    monkeypatch.setattr(rollback_grant, "load_key", lambda: key)
    class CheckedKube(FakeKube):
        grants = None
        def request_rollback(self, ns, name, **provenance):
            grant = provenance["grant"]
            verify(key, grant)
            assert (grant["incident_id"], grant["action_id"], grant["name"],
                    grant["expected_revision"], grant["expected_source"]) == (
                        self.incident_id, self.action_id, "inventory", 2, "inventory-aaa")
            self.grants = grant
            return super().request_rollback(ns, name, **provenance)
    k = CheckedKube()
    source = family1_kube()
    k.deployments, k.replicasets, k.pods = source.deployments, source.replicasets, source.pods
    with db.session() as s:
        aid = seed(s, level="COMPLEX", status="pending_approval")
        act = s.get(Action, aid)
        act.policy_mode = "approval"
        k.action_id, k.incident_id = aid, act.incident_id
    assert execute(db.session, AppConfig(), k, aid, "test").status == "denied"
    assert k.grants is None and not k.patches
    with db.session() as s:
        db.audit(s, "human", "action_approved", k.incident_id, action_id=aid)
    result = execute(db.session, AppConfig(), k, aid, "test")
    assert result.status in ("succeeded", "failed") and len(k.patches) == 1
    assert k.grants is not None
    with pytest.raises(InvalidGrant):
        verify(key, {**k.grants, "action_id": aid + 1})
    assert execute(db.session, AppConfig(), k, aid, "test").status == "denied"
    assert len(k.patches) == 1


@needs_db
def test_partial_unique_index_prevents_second_active_action(db_url):
    with db.session() as s:
        seed(s, status="executing")
    with pytest.raises(IntegrityError):
        with db.session() as s:
            seed(s, status="approved")
