"""KAN-74: Event/Pod watch reconnect, webhook/watch duplicate correlation and restart (host-only isolation).

These tests drive the *real* ``kubernetes.watch.Watch`` stream parser against a fake API
function, so BOOKMARK / ERROR(410) handling is exercised exactly as the client library
delivers it. No cluster, Secret or network access is involved.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import db
from app.config import AppConfig
from app.incidents import Signal, ingest
from app.main import alert_signal
from app.models import AuditLog, Incident
from app.watcher import Watchers
from conftest import family1_kube, family4_kube, mk_pod, mk_rs, needs_db

NS = "liveprobe-target"


# ------------------------------------------------------------------ fake watch transport
class _Resp:
    def __init__(self, events: list[dict]):
        self._lines = [json.dumps(e).encode() + b"\n" for e in events]

    def stream(self, amt=None, decode_content=False):
        yield from self._lines

    def close(self):
        pass

    def release_conn(self):
        pass


def _pod_obj(pod: dict, rv: str) -> dict:
    out = json.loads(json.dumps(pod))
    out.setdefault("apiVersion", "v1")
    out.setdefault("kind", "Pod")
    out["metadata"]["resourceVersion"] = rv
    for o in out["metadata"].get("ownerReferences") or []:
        o.setdefault("apiVersion", "apps/v1")
        o.setdefault("uid", "rs-uid")
    out["spec"] = {"containers": [{"name": c["name"], "image": c["image"]}
                                  for c in out["status"]["containerStatuses"]]}
    for c in out["status"]["containerStatuses"]:
        c.setdefault("imageID", "")  # required by the typed client model
    return out


class _FakeCore:
    """list_namespaced_pod / list_namespaced_event double: list call or scripted watch responses."""

    def __init__(self, list_items: list[dict], list_rv: str | list[str], watches: list, on_exhausted):
        self.list_items = list_items
        self.list_rvs = [list_rv] if isinstance(list_rv, str) else list(list_rv)
        self.watches = list(watches)       # each: list[event] or an Exception to raise
        self.on_exhausted = on_exhausted
        self.list_calls = 0
        self.watch_rvs: list[str | None] = []

        def list_namespaced_pod(namespace, **kw):
            """:rtype: V1PodList"""
            return self._call(kw, "V1PodList")

        def list_namespaced_event(namespace, **kw):
            """:rtype: CoreV1EventList"""
            return self._call(kw, "CoreV1EventList")

        self.list_namespaced_pod = list_namespaced_pod
        self.list_namespaced_event = list_namespaced_event

    def _call(self, kw, list_type):
        from kubernetes import client
        if not kw.get("watch"):
            rv = self.list_rvs[min(self.list_calls, len(self.list_rvs) - 1)]
            self.list_calls += 1
            api = client.ApiClient()

            class _Raw:
                data = json.dumps({"metadata": {"resourceVersion": rv}, "items": self.list_items})
            return api.deserialize(_Raw(), list_type)
        self.watch_rvs.append(kw.get("resource_version"))
        if not self.watches:
            self.on_exhausted()
            return _Resp([])
        nxt = self.watches.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return _Resp(nxt)


class _KubeWithCore:
    """FakeKube plus a ``core`` double and real ``to_dict`` serialisation."""

    def __init__(self, fake, core):
        from kubernetes import client
        self._fake, self.core, self._api = fake, core, client.ApiClient()

    def to_dict(self, obj):
        return self._api.sanitize_for_serialization(obj)

    def __getattr__(self, item):
        return getattr(self._fake, item)


def _bookmark(rv: str) -> dict:
    return {"type": "BOOKMARK", "object": {"kind": "Pod", "apiVersion": "v1", "metadata": {"resourceVersion": rv}}}


def _gone() -> dict:
    return {"type": "ERROR", "object": {"kind": "Status", "apiVersion": "v1", "status": "Failure",
                                        "message": "too old resource version", "reason": "Expired", "code": 410}}


# ------------------------------------------------------------------ watch reconnect
def test_pod_watch_bookmark_advances_resource_version_without_reconnect():
    k = family1_kube()
    failing = k.pods[1]
    emitted: list[Signal] = []
    w = Watchers(None, AppConfig(target_namespace="liveprobe-target", ), emitted.append)
    core = _FakeCore([], "100", [[_bookmark("150")], [{"type": "MODIFIED", "object": _pod_obj(failing, "160")}]],
                     on_exhausted=w.stop)
    w.kube = _KubeWithCore(k, core)
    w._pod_loop()  # must not raise on BOOKMARK (whose object is a raw dict, not a V1Pod)
    assert core.list_calls == 1                       # no relist
    assert core.watch_rvs[:3] == ["100", "150", "160"]  # bookmark and event RVs are resumed from
    assert [s.reason for s in emitted] == ["ImagePullBackOff"] and emitted[0].source == "pod_watch"


def test_pod_watch_410_relists_and_resumes_from_new_list_rv():
    k = family1_kube()
    emitted: list[Signal] = []
    w = Watchers(None, AppConfig(target_namespace="liveprobe-target", ), emitted.append)
    list_items = [_pod_obj(k.pods[1], "90")]
    core = _FakeCore(list_items, ["100", "500"], [[_gone()]], on_exhausted=w.stop)
    w.kube = _KubeWithCore(k, core)
    t = threading.Thread(target=w._guard, args=("pod_watch", w._pod_loop), daemon=True)
    start = time.monotonic()
    t.start()
    t.join(5)
    assert not t.is_alive()
    assert core.list_calls == 2                     # 410 -> fresh list
    assert core.watch_rvs[0] == "100" and core.watch_rvs[1] == "500"  # resumes from the relist RV
    assert time.monotonic() - start < 0.9           # relist is immediate (no error backoff)
    # the relist re-observes the still-failing pod: same signal re-emitted, which ingest merges
    assert [s.reason for s in emitted] == ["ImagePullBackOff", "ImagePullBackOff"]
    assert w.status["pod_watch"]["state"] == "running"


def test_event_watch_410_relists_without_replaying_history():
    k = family1_kube()
    emitted: list[Signal] = []
    w = Watchers(None, AppConfig(target_namespace="liveprobe-target", ), emitted.append)
    old_event = {"metadata": {"name": "old", "resourceVersion": "10"}, "type": "Warning", "reason": "BackOff",
                 "message": "Back-off pulling image", "involvedObject": {"kind": "Pod", "name": "inventory-bbb-1"}}
    core = _FakeCore([old_event], "100", [[_gone()]], on_exhausted=w.stop)
    w.kube = _KubeWithCore(k, core)
    t = threading.Thread(target=w._guard, args=("event_watch", w._event_loop), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive() and core.list_calls == 2
    assert emitted == []   # listed (historic) events are never replayed as signals


def test_watch_transport_error_is_reported_and_retried_with_backoff():
    k = family1_kube()
    w = Watchers(None, AppConfig(target_namespace="liveprobe-target", ), lambda s: None)
    from kubernetes.client import ApiException
    core = _FakeCore([], "100", [ApiException(status=500, reason="boom")], on_exhausted=w.stop)
    w.kube = _KubeWithCore(k, core)
    states = []
    orig_wait = w._stop.wait

    def wait(t):
        states.append(dict(w.status["pod_watch"]))
        return orig_wait(0)
    w._stop.wait = wait
    w._guard("pod_watch", w._pod_loop)
    assert states and states[0]["state"] == "reconnecting" and "ApiException" in states[0]["error"]
    assert core.list_calls == 2


# ------------------------------------------------------------------ owner-aware correlation
def test_event_for_recreated_pod_with_same_name_is_ignored():
    k = family1_kube()
    k.pods[1]["metadata"]["uid"] = "new-uid"
    emitted: list[Signal] = []
    w = Watchers(k, AppConfig(target_namespace="liveprobe-target", ), emitted.append)
    ev = {"type": "Warning", "reason": "BackOff", "message": "Back-off pulling image \"x\"",
          "involvedObject": {"kind": "Pod", "name": "inventory-bbb-1", "uid": "old-uid"}}
    w.handle_event(ev)
    assert emitted == []
    ev["involvedObject"]["uid"] = "new-uid"
    w.handle_event(ev)
    assert [(s.name, s.reason, s.source) for s in emitted] == [("inventory", "ImagePullBackOff", "event_watch")]


def test_owner_chain_wins_over_name_and_unowned_pods_are_not_guessed():
    k = family1_kube()
    # a pod *named* like inventory but owned (via RS) by the order Deployment
    k.replicasets.append(mk_rs("order-ddd", "order", 1, "bad"))
    k.pods.append(mk_pod("inventory-lookalike-1", "inventory", "order-ddd", waiting="ImagePullBackOff"))
    k.pods.append(mk_pod("inventory-bare", "inventory", None, waiting="ImagePullBackOff"))       # no owner
    k.pods.append(mk_pod("inventory-orphan-1", "inventory", "gone-rs", waiting="ImagePullBackOff"))  # RS missing
    emitted: list[Signal] = []
    w = Watchers(k, AppConfig(target_namespace="liveprobe-target", ), emitted.append)
    for p in k.pods[3:]:
        w.handle_pod(p)
    assert [s.name for s in emitted] == ["order"]
    alert = {"status": "firing", "labels": {"alertname": "TargetWorkloadImagePullFailing", "namespace": NS,
                                            "pod": "inventory-bare"}}
    assert alert_signal(alert, AppConfig(target_namespace="liveprobe-target", ), k) is None


def test_ambiguous_caller_5xx_is_not_merged_into_a_guessed_callee():
    cfg = AppConfig(target_namespace="liveprobe-target", dependencies={"order": ["inventory", "payments"]})
    from app.incidents import fingerprint
    assert fingerprint(Signal(NS, "App", "order", "OrderHigh5xxRate", "alertmanager"), cfg) == (f"{NS}/order", "order")


# ------------------------------------------------------------------ webhook/watch duplicates (DB)
def _count(s, fp=f"{NS}/inventory"):
    return len(list(s.scalars(select(Incident).where(Incident.fingerprint == fp))))


def _alert_sig(k, name="TargetWorkloadImagePullFailing"):
    return alert_signal({"status": "firing", "labels": {"alertname": name, "namespace": NS,
                                                        "pod": "inventory-bbb-1"}}, AppConfig(target_namespace="liveprobe-target", ), k)


@needs_db
@pytest.mark.parametrize("order", ["watch_first", "webhook_first"])
def test_webhook_and_watch_in_either_order_make_one_incident(db_url, order):
    cfg, k = AppConfig(target_namespace="liveprobe-target", ), family1_kube()
    watch_sigs: list[Signal] = []
    Watchers(k, cfg, watch_sigs.append).handle_pod(k.pods[1])
    sigs = [watch_sigs[0], _alert_sig(k)]
    if order == "webhook_first":
        sigs.reverse()
    now = datetime.now(timezone.utc)
    outcomes = []
    for i, sig in enumerate(sigs):
        with db.session() as s:
            outcomes.append(ingest(s, cfg, sig, now + timedelta(seconds=i))[1])
    assert outcomes == ["created", "merged"]
    with db.session() as s:
        inc = s.scalars(select(Incident)).one()
        assert inc.family_hint == "image_pull" and inc.signal_count == 2
        assert set(inc.signal_reasons) == {"ImagePullBackOff", "TargetWorkloadImagePullFailing"}
        # webhook replay (Alertmanager re-sends while firing) only increments the count
        ingest(s, cfg, _alert_sig(k), now + timedelta(seconds=5))
    with db.session() as s:
        assert _count(s) == 1 and s.scalars(select(Incident)).one().signal_count == 3


@needs_db
def test_out_of_order_caller_5xx_then_dependency_signal(db_url):
    cfg = AppConfig(target_namespace="liveprobe-target", )
    now = datetime.now(timezone.utc)
    with db.session() as s:
        assert ingest(s, cfg, Signal(NS, "App", "order", "OrderHigh5xxRate", "alertmanager"), now)[1] == "suppressed"
    with db.session() as s:
        assert ingest(s, cfg, Signal(NS, "Service", "inventory", "NoEndpoints", "endpoint_poller"),
                      now + timedelta(seconds=2))[1] == "created"
    with db.session() as s:
        inc, out = ingest(s, cfg, Signal(NS, "App", "order", "OrderHigh5xxRate", "alertmanager"),
                          now + timedelta(seconds=3))
        assert out == "merged" and inc.signal_reasons == ["NoEndpoints", "OrderHigh5xxRate"]
    with db.session() as s:
        assert _count(s) == 1


@needs_db
def test_concurrent_webhook_and_watch_sessions_create_exactly_one_incident(db_url):
    """Two sessions (webhook thread + watch thread) must not both miss each other's uncommitted insert."""
    cfg, k = AppConfig(target_namespace="liveprobe-target", ), family1_kube()
    first_flushed = threading.Event()
    results: dict[str, str] = {}
    errors: list[BaseException] = []

    def watch_side():
        try:
            with db.session() as s:
                _, results["watch"] = ingest(s, cfg, Signal(NS, "Deployment", "inventory", "ImagePullBackOff",
                                                            "pod_watch"))
                first_flushed.set()
                time.sleep(0.5)  # hold the transaction open: the webhook arrives before our commit
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
            first_flushed.set()

    def webhook_side():
        try:
            first_flushed.wait(5)
            with db.session() as s:
                _, results["webhook"] = ingest(s, cfg, _alert_sig(k))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=watch_side), threading.Thread(target=webhook_side)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert not errors
    assert results == {"watch": "created", "webhook": "merged"}
    with db.session() as s:
        assert _count(s) == 1
        assert s.scalars(select(Incident)).one().signal_count == 2


@needs_db
def test_many_concurrent_duplicate_signals_stay_one_incident(db_url):
    cfg = AppConfig(target_namespace="liveprobe-target", )
    barrier = threading.Barrier(8)
    outcomes: list[str] = []
    lock = threading.Lock()

    def go(i):
        barrier.wait(5)
        with db.session() as s:
            src = "alertmanager" if i % 2 else "event_watch"
            _, out = ingest(s, cfg, Signal(NS, "Deployment", "inventory", "ErrImagePull", src))
            time.sleep(0.05)
        with lock:
            outcomes.append(out)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(15)
    assert sorted(outcomes) == ["created"] + ["merged"] * 7
    with db.session() as s:
        assert _count(s) == 1 and s.scalars(select(Incident)).one().signal_count == 8
        assert len(list(s.scalars(select(AuditLog).where(AuditLog.event == "incident_created")))) == 1


@needs_db
def test_restart_relist_merges_into_surviving_open_incident(db_url):
    cfg, k = AppConfig(target_namespace="liveprobe-target", ), family1_kube()
    now = datetime.now(timezone.utc)
    with db.session() as s:
        first, _ = ingest(s, cfg, Signal(NS, "Deployment", "inventory", "ImagePullBackOff", "pod_watch"), now)
        fid = first.id
    # "restart": a brand-new Watchers instance relists and re-emits the still-failing pod
    sigs: list[Signal] = []
    Watchers(k, cfg, sigs.append).handle_pod(k.pods[1])
    with db.session() as s:
        inc, out = ingest(s, cfg, sigs[0], now + timedelta(seconds=30))
        assert out == "merged" and inc.id == fid
    with db.session() as s:
        assert _count(s) == 1


@needs_db
def test_distinct_workloads_are_never_merged(db_url):
    cfg = AppConfig(target_namespace="liveprobe-target", )
    now = datetime.now(timezone.utc)
    with db.session() as s:
        a, _ = ingest(s, cfg, Signal(NS, "Deployment", "inventory", "ImagePullBackOff", "pod_watch"), now)
        b, out = ingest(s, cfg, Signal(NS, "Deployment", "order", "ImagePullBackOff", "pod_watch"), now)
        assert out == "created" and a.id != b.id


def test_endpoint_poller_family4_signal_matches_webhook_fingerprint():
    k = family4_kube()
    sigs: list[Signal] = []
    w = Watchers(k, AppConfig(target_namespace="liveprobe-target", ), sigs.append)
    w.poll_endpoints_once(now=0)
    w.poll_endpoints_once(now=31)
    alert = alert_signal({"status": "firing", "labels": {"alertname": "TargetServiceNoEndpoints", "namespace": NS,
                                                         "endpoint": "inventory"}}, AppConfig(target_namespace="liveprobe-target", ), k)
    from app.incidents import fingerprint
    assert fingerprint(sigs[0], AppConfig(target_namespace="liveprobe-target", )) == fingerprint(alert, AppConfig(target_namespace="liveprobe-target", ))
