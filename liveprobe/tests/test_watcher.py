"""KAN-39: signal deduplication, ownership and endpoint grace."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import db
from app.config import AppConfig
from app.evidence import deployment_summary
from app.incidents import Signal, fingerprint, ingest
from app.k8s import deployment_for_pod
from app.models import Incident
from app.watcher import Watchers
from conftest import FakeKube, family1_kube, family4_kube, needs_db

NS = "liveprobe-target"


def test_caller_5xx_merges_to_callee_fingerprint():
    cfg = AppConfig()
    assert fingerprint(Signal(NS, "App", "order", "OrderHigh5xxRate", "alertmanager"), cfg) == (
        f"{NS}/inventory", "inventory")


@needs_db
def test_stale_caller_5xx_does_not_open_new_incident(db_url):
    with db.session() as s:
        inc, outcome = ingest(s, AppConfig(), Signal(NS, "App", "order", "OrderHigh5xxRate", "alertmanager"))
        assert inc is None and outcome == "suppressed"


@needs_db
def test_signal_dedup_and_window(db_url):
    cfg = AppConfig()
    now = datetime.now(timezone.utc)
    with db.session() as s:
        a, outcome = ingest(s, cfg, Signal(NS, "Deployment", "inventory", "ImagePullBackOff", "pod_watch"), now)
        first = a.id
        assert outcome == "created"
        b, outcome = ingest(s, cfg, Signal(NS, "Deployment", "inventory", "ErrImagePull", "event_watch"),
                            now + timedelta(seconds=1))
        assert outcome == "merged" and b.id == first and b.signal_count == 2
        assert s.scalar(select(Incident).where(Incident.id == first)).signal_reasons == [
            "ImagePullBackOff", "ErrImagePull"]
        # P15 live run: one OOM fault whose analysis stalled (quota) opened 14 incidents, one per Alertmanager repeat.
        # While the earlier incident is still open, the same reason class keeps merging past the window.
        c, outcome = ingest(s, cfg, Signal(NS, "Deployment", "inventory", "ImagePullBackOff", "pod_watch"),
                            now + timedelta(seconds=cfg.merge_window_seconds + 1))
        assert outcome == "merged" and c.id == first
        # A different reason class past the window is a new occurrence.
        d, outcome = ingest(s, cfg, Signal(NS, "Deployment", "inventory", "TargetWorkloadOOMKilled", "alertmanager"),
                            now + timedelta(seconds=cfg.merge_window_seconds + 2))
        assert outcome == "created" and d.id != first


@needs_db
def test_restart_interruption_suppresses_same_fault_beyond_normal_cooldown(db_url):
    cfg = AppConfig()
    now = datetime.now(timezone.utc)
    sig = Signal(NS, "Service", "inventory", "NoEndpoints", "endpoint_poller")
    with db.session() as s:
        inc, outcome = ingest(s, cfg, sig, now)
        assert outcome == "created"
        inc.status = "failed"
        inc.status_reason = "LiveProbe restarted while incident was 'analyzing'; marked failed (no replay)"
        inc.closed_at = now
        same, outcome = ingest(s, cfg, sig, now + timedelta(seconds=cfg.close_cooldown_seconds + 20))
        assert outcome == "suppressed" and same.id == inc.id


def test_pod_owner_resolution_and_signal():
    k = family1_kube()
    pod = k.pods[1]
    assert deployment_for_pod(k, NS, pod) == "inventory"
    emitted = []
    w = Watchers(k, AppConfig(), emitted.append)
    w.handle_pod(pod)
    assert len(emitted) == 1 and emitted[0].reason == "ImagePullBackOff"
    pod["metadata"]["deletionTimestamp"] = "2026-09-25T11:00:00Z"
    w.handle_pod(pod)
    assert len(emitted) == 1


def test_deployment_summary_tracks_reused_replicaset_rollout():
    k = family1_kube(created_bad="2026-09-24T10:00:00Z")
    k.deployments["inventory"]["status"]["conditions"] = [
        {"type": "Progressing", "lastUpdateTime": "2026-09-25T13:31:37Z"}]
    d = deployment_summary(k, NS, "inventory")
    assert d["recent_rollout_at"] == "2026-09-25T13:31:37+00:00"


def test_endpoint_grace_then_signal_and_restore():
    k = family4_kube()
    emitted = []
    w = Watchers(k, AppConfig(), emitted.append)
    w.poll_endpoints_once(now=10)
    assert not emitted
    w.poll_endpoints_once(now=41)
    assert len(emitted) == 1 and emitted[0].reason == "NoEndpoints"
    k.endpoints["inventory"] = {"subsets": [{"addresses": [{"ip": "10.0.0.1"}]}]}
    w.poll_endpoints_once(now=55)
    assert "inventory" not in w._empty_since


@pytest.mark.parametrize("intervening", [1, 55])
def test_stale_open_merge_finds_matching_older_reason(db_url, intervening):
    """독립 검토 재현: 최신 열린 사건만 보면 그보다 오래된 같은 원인 사건을 놓친다."""
    from app import db
    from app.config import AppConfig
    from app.incidents import Signal, ingest
    cfg = AppConfig()
    now = datetime.now(timezone.utc)
    image = Signal("apps", "Deployment", "api", "ImagePullBackOff", "pod_watch")
    oom = Signal("apps", "Deployment", "api", "TargetWorkloadOOMKilled", "alertmanager")
    with db.session() as s:
        a, _ = ingest(s, cfg, image, now)
        assert a is not None
        b, status = ingest(s, cfg, oom, now + timedelta(seconds=cfg.merge_window_seconds + 1))
        assert status == "created" and b.id != a.id
        for i in range(intervening - 1):
            s.add(Incident(fingerprint=a.fingerprint, namespace=a.namespace, workload=a.workload,
                           title="다른 원인", status="open", signal_reasons=[f"different-{i}"],
                           created_at=now + timedelta(seconds=cfg.merge_window_seconds + 1)))
        s.flush()
        again, status = ingest(s, cfg, image, now + timedelta(seconds=2 * cfg.merge_window_seconds + 2))
        assert (again.id, status) == (a.id, "merged")
