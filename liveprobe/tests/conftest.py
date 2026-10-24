"""Shared fixtures: a disposable pgvector DB and fake Kubernetes / LLM objects."""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB_ADMIN = os.environ.get("LIVEPROBE_TEST_DB_ADMIN", "postgresql+psycopg://test:test@localhost:55432/lptest")


def _db_available() -> bool:
    try:
        with create_engine(TEST_DB_ADMIN).connect() as c:
            c.execute(text("select 1"))
        return True
    except Exception:
        return False


DB_AVAILABLE = _db_available()
needs_db = pytest.mark.skipif(not DB_AVAILABLE, reason="test postgres (pgvector) not reachable")


@pytest.fixture()
def db_url():
    """Fresh schema per test: drop+recreate public, run Alembic migrations, init session factory."""
    if not DB_AVAILABLE:
        pytest.skip("test postgres not reachable")
    eng = create_engine(TEST_DB_ADMIN, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    eng.dispose()
    from app import db
    db.run_migrations(TEST_DB_ADMIN)
    db.init_db(TEST_DB_ADMIN)
    yield TEST_DB_ADMIN
    db.engine().dispose()


# ---------------------------------------------------------------- fake kube
def mk_template(image: str, app: str = "inventory") -> dict:
    return {"metadata": {"labels": {"app": app}},
            "spec": {"containers": [{"name": app, "image": image, "env": [{"name": "X", "value": "secret-ish"}]}]}}


def mk_rs(name: str, dep: str, rev: int, image: str, replicas: int = 1, available: int = 1,
          created: str = "2026-09-25T10:00:00Z") -> dict:
    t = mk_template(image, dep)
    t["metadata"]["labels"]["pod-template-hash"] = name.rsplit("-", 1)[-1]
    return {"metadata": {"name": name, "annotations": {"deployment.kubernetes.io/revision": str(rev)},
                         "ownerReferences": [{"kind": "Deployment", "name": dep}], "creationTimestamp": created},
            "spec": {"template": t}, "status": {"replicas": replicas, "availableReplicas": available}}


def mk_dep(name: str, rev: int, image: str, replicas: int = 1, status: dict | None = None) -> dict:
    return {"metadata": {"name": name, "annotations": {"deployment.kubernetes.io/revision": str(rev)},
                         "generation": 3},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                     "template": mk_template(image, name)},
            "status": status or {"observedGeneration": 3, "replicas": replicas, "updatedReplicas": replicas,
                                 "availableReplicas": replicas, "readyReplicas": replicas}}


def mk_pod(name: str, app: str, rs: str | None, waiting: str | None = None, ready: bool = True,
           image: str = "img") -> dict:
    cs = {"name": app, "image": image, "ready": ready and not waiting, "restartCount": 0,
          "state": {"waiting": {"reason": waiting, "message": "Back-off pulling image"}} if waiting else {"running": {}}}
    return {"metadata": {"name": name, "labels": {"app": app},
                         "ownerReferences": [{"kind": "ReplicaSet", "name": rs}] if rs else [],
                         "creationTimestamp": "2026-09-25T09:00:00Z"},
            "status": {"phase": "Pending" if waiting else "Running", "containerStatuses": [cs]}}


class FakeKube:
    def __init__(self):
        self.deployments: dict[str, dict] = {}
        self.replicasets: list[dict] = []
        self.pods: list[dict] = []
        self.services: dict[str, dict] = {}
        self.endpoints: dict[str, dict] = {}
        self.events: list[dict] = []
        self.nodes: dict[str, dict] = {}
        self.configmaps: dict[str, dict] = {}
        self.patches: list[tuple] = []
        self.after_patch = None  # callable(fake) to simulate controller reaction

    def get_deployment(self, ns, name):
        d = self.deployments.get(name)
        return copy.deepcopy(d) if d else None

    def list_deployments(self, ns):
        return [copy.deepcopy(d) for d in self.deployments.values()]

    def list_replicasets(self, ns):
        return copy.deepcopy(self.replicasets)

    def get_replicaset(self, ns, name):
        return next((copy.deepcopy(r) for r in self.replicasets if r["metadata"]["name"] == name), None)

    def list_pods(self, ns, selector=None):
        out = self.pods
        if selector:
            out = [p for p in out if all(p["metadata"]["labels"].get(k) == v for k, v in selector.items())]
        return copy.deepcopy(out)

    def get_pod(self, ns, name):
        return next((copy.deepcopy(p) for p in self.pods if p["metadata"]["name"] == name), None)

    def get_service(self, ns, name):
        return copy.deepcopy(self.services.get(name))

    def list_services(self, ns):
        return copy.deepcopy(list(self.services.values()))

    def get_endpoints(self, ns, name):
        return copy.deepcopy(self.endpoints.get(name))

    def configmap_meta(self, ns, name):
        from app.k8s import configmap_fingerprint
        cm = self.configmaps.get(name)
        return configmap_fingerprint(copy.deepcopy(cm)) if cm is not None else None

    def get_node(self, name):
        return copy.deepcopy(self.nodes.get(name))

    def list_events(self, ns):
        return copy.deepcopy(self.events)

    def request_rollback(self, ns, name, **provenance):
        # Transport double only. Security decisions are exercised against
        # plan_undo and admission with separate adversarial tests.
        old = self.replicasets[0]["spec"]["template"]["spec"]["containers"][0]["image"]
        self.patches.append((ns, name, [{"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": old}]))
        self.deployments[name]["spec"]["template"]["spec"]["containers"][0]["image"] = old
        if self.after_patch:
            self.after_patch(self)
        return {"to_revision": 1, "to_images": [old], "to_replicaset": self.replicasets[0]["metadata"]["name"],
                "before": {"revision": 2}}


def family1_kube(image_ok="liveprobe/inventory:dev", image_bad="liveprobe/inventory:does-not-exist",
                 created_bad="2026-09-25T10:00:00Z") -> FakeKube:
    k = FakeKube()
    k.deployments["inventory"] = mk_dep("inventory", 2, image_bad, status={
        "observedGeneration": 3, "replicas": 2, "updatedReplicas": 1, "availableReplicas": 1, "readyReplicas": 1,
        "unavailableReplicas": 1})
    k.replicasets = [mk_rs("inventory-aaa", "inventory", 1, image_ok),
                     mk_rs("inventory-bbb", "inventory", 2, image_bad, available=0, created=created_bad)]
    k.pods = [mk_pod("inventory-aaa-1", "inventory", "inventory-aaa", image=image_ok),
              mk_pod("inventory-bbb-1", "inventory", "inventory-bbb", waiting="ImagePullBackOff", image=image_bad),
              mk_pod("order-ccc-1", "order", "order-ccc")]
    k.services["inventory"] = {"metadata": {"name": "inventory"}, "spec": {"selector": {"app": "inventory"}}}
    k.endpoints["inventory"] = {"subsets": [{"addresses": [{"ip": "10.0.0.1"}]}]}
    return k


def family4_kube() -> FakeKube:
    k = FakeKube()
    k.deployments["inventory"] = mk_dep("inventory", 1, "liveprobe/inventory:dev")
    k.replicasets = [mk_rs("inventory-aaa", "inventory", 1, "liveprobe/inventory:dev", created="2026-09-24T10:00:00Z")]
    k.pods = [mk_pod("inventory-aaa-1", "inventory", "inventory-aaa"), mk_pod("order-ccc-1", "order", "order-ccc")]
    k.services["inventory"] = {"metadata": {"name": "inventory"}, "spec": {"selector": {"app": "inventory-broken"}}}
    k.endpoints["inventory"] = {"subsets": []}
    return k
