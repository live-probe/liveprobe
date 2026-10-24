"""P15 operations overview: raw resource metrics + topology + incident location, never inferred zero/health."""
from datetime import datetime, timezone

from conftest import needs_db


def inventory():
    return {
        "nodes": [{"metadata": {"name": "node-1"}, "status": {"capacity": {"cpu": "4", "memory": "8Gi"},
                   "conditions": [{"type": "Ready", "status": "True"}]}}],
        "namespaces": [{"metadata": {"name": "empty"}}],
        "deployments": [{"metadata": {"namespace": "apps", "name": "api"},
                         "spec": {"replicas": 2}, "status": {"readyReplicas": 1}},
                        {"metadata": {"namespace": "empty", "name": "asleep"},
                         "spec": {"replicas": 0}, "status": {"readyReplicas": 0}}],
        "pods": [{"metadata": {"namespace": "apps", "name": "api-abc-1",
                               "ownerReferences": [{"kind": "ReplicaSet", "name": "api-abc"}]},
                  "spec": {"nodeName": "node-1"}, "status": {"phase": "Running", "containerStatuses": [{"ready": True}]}},
                 {"metadata": {"namespace": "apps", "name": "api-abc-2",
                               "ownerReferences": [{"kind": "ReplicaSet", "name": "api-abc"}]},
                  "spec": {"nodeName": "node-1"}, "status": {"phase": "Pending"}},
                 {"metadata": {"namespace": "apps", "name": "finished",
                               "ownerReferences": [{"kind": "Job", "name": "one-shot"}]},
                  "spec": {"nodeName": "node-1"}, "status": {"phase": "Succeeded"}}],
        "services": [], "endpoints": [], "ingresses": [], "errors": [],
    }


def sample(labels, value):
    return {"metric": labels, "value": [1, str(value)]}


def test_overview_raw_metrics_and_incident_location():
    from app.overview import assemble
    inv = inventory()
    inc = {"id": 55, "namespace": "apps", "workload": "api", "status": "analyzing", "severity_level": "NORMAL",
           "stage": {"key": "analyzing", "label": "Agent 분석 중"}, "created_at": "2026-10-19T00:00:00Z"}
    values = {
        "pod_cpu": [sample({"namespace": "apps", "pod": "api-abc-1"}, 0.123)],
        "pod_mem": [sample({"namespace": "apps", "pod": "api-abc-1"}, 123456)],
        "pod_restarts": [sample({"namespace": "apps", "pod": "api-abc-1"}, 3)],
        "node_cpu": [sample({"node": "node-1"}, 1.23)],
        "node_mem": [sample({"node": "node-1"}, 456789)],
        "req_rate": [sample({"namespace": "apps", "app": "api"}, 2.4)],
        "err_rate": [sample({"namespace": "apps", "app": "api"}, 0.25)],
        "p95_ms": [sample({"namespace": "apps", "app": "api"}, "NaN")],
    }
    ev = [{"metadata": {"namespace": "apps"}, "type": "Warning", "reason": "Failed", "count": 2,
           "involvedObject": {"kind": "Pod", "name": "api-abc-2"}, "lastTimestamp": "2026-10-19T00:00:01Z",
           "message": 'Failed to pull image "verbatim:bad"'}]
    d = assemble(inv, values, events=ev, incidents=[inc], today={"total": 1, "resolved": 0, "handed_off": 0, "failed": 0})
    w = next(x for x in d["workloads"] if x["id"] == "apps/Deployment/api")
    assert w["desired"] == 2 and w["ready"] == 1 and w["incident"]["id"] == 55
    assert w["cpu"] == 0.123 and w["mem"] == 123456 and w["restarts_1h"] == 3
    assert w["req_rate"] == 2.4 and w["err_rate"] == 0.25 and w["p95_ms"] is None
    n = d["cluster"]["nodes"][0]
    assert n["cpu_cores"] == 4 and n["mem_bytes"] == 8 * 1024**3
    assert n["cpu_used_cores"] == 1.23 and n["mem_used_bytes"] == 456789 and n["pods"] == 2
    ns = next(n for n in d["cluster"]["namespaces"] if n["name"] == "apps")
    assert ns["pods_running"] == 1 and ns["pods_pending"] == 1 and ns["pods_failed"] == 0
    assert ns["workloads_unhealthy"] == 1          # completed Job is NOT unhealthy
    empty = next(w for w in d["workloads"] if w["name"] == "asleep")
    assert empty["desired"] == 0 and empty["ready"] == 0   # zero-replica workloads must not disappear
    assert empty["cpu"] is None and empty["restarts_1h"] is None
    assert d["events"][0]["message"] == ev[0]["message"]
    assert next(n for n in d["topology"]["nodes"] if n["id"] == w["id"])["warnings"][0]["incident_id"] == 55


def test_missing_metrics_are_null_and_source_failure_is_visible():
    from app.overview import assemble
    d = assemble(inventory(), {}, errors={"prometheus": "HTTPStatusError", "events": "TimeoutError"})
    assert d["sources"]["prometheus"] != "ok" and "TimeoutError" in d["sources"]["kubernetes"]
    assert d["cluster"]["nodes"][0]["cpu_used_cores"] is None
    assert all(w["cpu"] is None for w in d["workloads"])
    assert d["sources"]["hubble"] != "connected" and d["sources"]["envoy"] != "ok"


def test_envoy_edge_metrics_join_only_observed_workloads():
    from app.overview import assemble
    labels = {"source_workload_namespace": "apps", "source_workload": "api",
              "destination_workload_namespace": "empty", "destination_workload": "asleep"}
    d = assemble(inventory(), {"edge_req": [sample(labels, 1.1)], "edge_err": [sample(labels, 0.4)]})
    e = next(e for e in d["topology"]["observed_edges"] if e.get("req_rate") is not None)
    assert e["from"] == "apps/Deployment/api" and e["to"] == "empty/Deployment/asleep"
    assert e["req_rate"] == 1.1 and e["err_rate"] == 0.4
    assert e["state"] == "degraded" and d["sources"]["envoy"] == "ok"


@needs_db
def test_overview_endpoint_loads_real_incident_stage(db_url, monkeypatch):
    from fastapi.testclient import TestClient
    from app import config, db, main, overview
    from app.models import Incident
    monkeypatch.setenv("LIVEPROBE_DATABASE_URL", db_url)
    monkeypatch.setenv("LIVEPROBE_BACKGROUND", "false")
    config.get_settings.cache_clear()
    config.get_app_config.cache_clear()
    with db.session() as s:
        i = Incident(fingerprint="overview", namespace="apps", workload="api", title="bad image", status="analyzing")
        s.add(i)
        s.flush()
        iid = i.id
    monkeypatch.setattr(overview, "fetch_metrics", lambda settings: ({}, {}))
    class K:
        def cluster_inventory(self):
            return inventory()
        def cluster_warning_events(self):
            return []
    with TestClient(main.app, base_url="http://liveprobe.localhost") as c:
        monkeypatch.setitem(main.STATE, "kube", K())
        monkeypatch.setitem(main.STATE, "flows", None)
        main._TOPO_CACHE.clear()
        r = c.get("/api/overview")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["incidents"]["open"][0]["id"] == iid
        assert d["incidents"]["open"][0]["stage"]["label"]
        assert d["incidents"]["today"]["total"] == 1
        assert d["cluster"]["namespaces"] and d["sources"]["kubernetes"] == "ok"
    config.get_settings.cache_clear()


def test_p95_falls_back_to_destination_envoy_latency_only_when_app_histogram_missing():
    """앱 히스토그램이 없으면 서버측 Envoy(destination) 지연 분포에서 p95를 읽는다. 둘 다 없으면 미관측(None)."""
    from app.overview import QUERIES, assemble
    assert "istio_request_duration_milliseconds_bucket" in QUERIES["p95_ms"] and 'reporter="destination"' in QUERIES["p95_ms"]
    assert "http_server_requests_seconds_bucket" in QUERIES["p95_ms"]
    inv = inventory()
    values = {"p95_ms": [sample({"namespace": "apps", "app": "api"}, 42.5)]}
    d = assemble(inv, values, events=[], incidents=[], today={"total": 0, "resolved": 0, "handed_off": 0, "failed": 0})
    assert next(x for x in d["workloads"] if x["id"] == "apps/Deployment/api")["p95_ms"] == 42.5
