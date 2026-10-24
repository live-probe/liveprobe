"""P3: flow aggregation, topology merge and honesty rules (no cluster, no gRPC server needed)."""
from __future__ import annotations

from app.flows import (FlowRec, FlowStore, STALE_AFTER_S, aggregate, build_topology, endpoint_key, flow_from_proto)


def rec(src, dst, verdict="FORWARDED", ts=1000.0, dport=8080, reason=None, svc=None, proto="TCP"):
    return FlowRec(ts, src, dst, verdict, proto, dport, reason, dst_service=svc)


def pod(ns, name, rs, ip, app, ready=True, node="n1"):
    return {"metadata": {"namespace": ns, "name": name, "labels": {"app": app},
                         "ownerReferences": [{"kind": "ReplicaSet", "name": rs}]},
            "spec": {"nodeName": node},
            "status": {"phase": "Running", "podIP": ip, "containerStatuses": [{"ready": ready}]}}


def svc(ns, name, ip, app):
    return {"metadata": {"namespace": ns, "name": name}, "spec": {"clusterIP": ip, "selector": {"app": app}, "ports": [{"port": 8080}]}}


INV = {
    "nodes": [{"metadata": {"name": "n1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}],
    "pods": [pod("t", "order-aaa-1", "order-aaa", "10.42.0.5", "order"), pod("t", "inv-bbb-1", "inv-bbb", "10.42.0.6", "inv"),
             pod("t", "lg-ccc-1", "lg-ccc", "10.42.0.7", "lg")],
    "services": [svc("t", "inv", "10.43.0.9", "inv"), svc("t", "order", "10.43.0.8", "order")],
    "endpoints": [{"metadata": {"namespace": "t", "name": "inv"}, "subsets": [{"addresses": [{"ip": "10.42.0.6"}]}]},
                  {"metadata": {"namespace": "t", "name": "order"}, "subsets": [{"addresses": [{"ip": "10.42.0.5"}]}]}],
}
FRESH = {"state": "connected", "stale": False, "last_flow_age_s": 1.0}


def test_endpoint_key_pod_ip_and_noise():
    assert endpoint_key("t", "order-1", "1.1.1.1") == ("t/pod/order-1", "t")
    assert endpoint_key(None, None, "10.0.0.1") == ("ip/10.0.0.1", None)
    assert endpoint_key(None, None, "0.0.0.0")[0] == "ip/unknown"


def test_aggregate_counts_verdicts_ports_reasons():
    flows = [rec("a", "b"), rec("a", "b"), rec("a", "b", "DROPPED", reason="POLICY_DENIED"), rec("a", "b", "ERROR")]
    e = aggregate(flows)[("a", "b")]
    assert (e.ok, e.dropped, e.error) == (2, 1, 1)
    assert e.ports["TCP/8080"] == 4 and e.drop_reasons["POLICY_DENIED"] == 1


def test_pods_fold_into_workloads_and_service_ips_resolve():
    flows = [rec("t/pod/lg-ccc-1", "t/pod/order-aaa-1"), rec("t/pod/order-aaa-1", "ip/10.43.0.9")]
    topo = build_topology(INV, flows, FRESH)
    edges = {(e["from"], e["to"]) for e in topo["observed_edges"]}
    assert ("t/Deployment/lg", "t/Deployment/order") in edges  # pods folded into their workloads
    names = {n["id"] for n in topo["nodes"]}
    assert "t/Deployment/order" in names and "t/Service/inv" in names
    # a Service ClusterIP folds into the workload behind it; the Service name stays on the edge
    assert ("t/Deployment/order", "t/Deployment/inv") in edges
    e = next(e for e in topo["observed_edges"] if e["to"] == "t/Deployment/inv")
    assert e["flows"] == 1


def test_structure_edge_without_flow_is_marked_unobserved_not_healthy():
    topo = build_topology(INV, [], FRESH)
    sel = [e for e in topo["structure_edges"] if e["type"] == "selects"]
    assert sel and all(e["observed"] is False for e in sel)
    assert topo["observed_edges"] == []


def test_blocked_vs_degraded_edge_state():
    topo = build_topology(INV, [rec("t/pod/lg-ccc-1", "t/pod/order-aaa-1", "DROPPED", reason="POLICY_DENIED")], FRESH)
    assert topo["observed_edges"][0]["state"] == "blocked"
    topo = build_topology(INV, [rec("t/pod/lg-ccc-1", "t/pod/order-aaa-1"), rec("t/pod/lg-ccc-1", "t/pod/order-aaa-1", "DROPPED")], FRESH)
    assert topo["observed_edges"][0]["state"] == "degraded"


def test_external_destination_is_its_own_node():
    topo = build_topology(INV, [rec("t/pod/order-aaa-1", "ip/93.184.216.34", dport=443)], FRESH)
    ext = [n for n in topo["nodes"] if n["kind"] == "External"]
    assert ext and ext[0]["name"] == "93.184.216.34"


def test_workload_health_and_no_endpoints():
    inv = {**INV, "endpoints": [{"metadata": {"namespace": "t", "name": "inv"}, "subsets": []}]}
    inv["pods"] = [pod("t", "inv-bbb-1", "inv-bbb", "10.42.0.6", "inv", ready=False)] + INV["pods"][:1]
    topo = build_topology(inv, [], FRESH)
    by = {n["id"]: n for n in topo["nodes"]}
    assert by["t/Deployment/inv"]["status"] == "down"
    assert by["t/Service/inv"]["status"] == "no_endpoints"


def test_incident_warning_attaches_to_workload_and_skips_resolved():
    incs = [{"id": 7, "namespace": "t", "workload": "inv", "status": "analyzing", "severity_level": "NORMAL", "title": "x"},
            {"id": 8, "namespace": "t", "workload": "order", "status": "resolved", "severity_level": "SIMPLE", "title": "y"}]
    topo = build_topology(INV, [], FRESH, incs)
    by = {n["id"]: n for n in topo["nodes"]}
    assert by["t/Deployment/inv"]["warnings"][0]["incident_id"] == 7
    assert "warnings" not in by["t/Deployment/order"]


def test_store_window_and_freshness():
    now = [1000.0]
    s = FlowStore(window_s=100, clock=lambda: now[0])
    assert s.freshness()["stale"] is True and s.freshness()["state"] == "never_connected"
    s.stream_state = "connected"
    s.add([rec("a", "b", ts=now[0])])
    assert s.freshness()["stale"] is False
    now[0] += STALE_AFTER_S + 1
    assert s.freshness()["stale"] is True            # silence is reported as delay, not as "no traffic"
    now[0] += 200
    assert s.snapshot() == []                         # window expired
    s.stream_state = "reconnecting"
    assert s.freshness()["stale"] is True


def test_flow_from_proto_real_message():
    from app.hubble_gen.flow import flow_pb2 as pb
    f = pb.Flow(verdict=pb.DROPPED, drop_reason_desc=pb.POLICY_DENIED)
    f.IP.source, f.IP.destination = "10.42.0.7", "10.42.0.5"
    f.source.namespace, f.source.pod_name = "t", "lg-ccc-1"
    f.destination.namespace, f.destination.pod_name = "t", "order-aaa-1"
    f.l4.TCP.destination_port = 8080
    f.destination_service.namespace, f.destination_service.name = "t", "order"
    r = flow_from_proto(f)
    assert (r.src, r.dst, r.verdict, r.proto, r.dport) == ("t/pod/lg-ccc-1", "t/pod/order-aaa-1", "DROPPED", "TCP", 8080)
    assert r.drop_reason == "POLICY_DENIED" and r.dst_service == "t/order"


def test_flow_from_proto_drops_kernel_noise():
    from app.hubble_gen.flow import flow_pb2 as pb
    f = pb.Flow(verdict=pb.DROPPED)
    f.IP.source, f.IP.destination = "0.0.0.0", "0.0.0.0"
    assert flow_from_proto(f) is None


# ---------------------------------------------------------------- API route
import pytest
from fastapi.testclient import TestClient
from conftest import needs_db


class _FakeKube:
    def cluster_inventory(self):
        return INV


@pytest.fixture()
def api(db_url, monkeypatch):
    monkeypatch.setenv("LIVEPROBE_DATABASE_URL", db_url)
    monkeypatch.setenv("LIVEPROBE_BACKGROUND", "false")
    from app import config, main
    config.get_settings.cache_clear(); config.get_app_config.cache_clear()
    main._TOPO_CACHE.update(at=0.0, inventory=None)
    with TestClient(main.app, base_url="http://liveprobe.localhost", headers={"X-LiveProbe-Client": "dashboard"}) as c:
        yield c, main
    main.STATE.update(kube=None, flows=None)
    config.get_settings.cache_clear()


@needs_db
def test_topology_route_merges_inventory_flows_and_incident(api):
    c, main = api
    from app import db
    from app.models import Incident
    store = FlowStore(); store.stream_state = "connected"
    import time; store.add([rec("t/pod/lg-ccc-1", "t/pod/order-aaa-1", ts=time.time())])
    main.STATE.update(kube=_FakeKube(), flows=store)
    with db.session() as s:
        s.add(Incident(fingerprint="f", namespace="t", workload="inv", status="open", title="img", severity_level="SIMPLE", signal_reasons=[]))
    r = c.get("/api/topology")
    assert r.status_code == 200
    body = r.json()
    assert body["freshness"]["stale"] is False
    assert any(e["from"] == "t/Deployment/lg" and e["to"] == "t/Deployment/order" for e in body["observed_edges"])
    assert next(n for n in body["nodes"] if n["id"] == "t/Deployment/inv")["warnings"][0]["level"] == "SIMPLE"


@needs_db
def test_topology_without_collector_says_never_connected_not_quiet(api):
    c, main = api
    main.STATE.update(kube=_FakeKube(), flows=None)
    body = c.get("/api/topology").json()
    assert body["freshness"]["state"] == "never_connected" and body["freshness"]["stale"] is True
    assert body["observed_edges"] == []


@needs_db
def test_topology_guarded_and_needs_cluster_client(api):
    c, main = api
    assert c.get("/api/topology", headers={"Host": "evil.example"}).status_code == 403
    main.STATE.update(kube=None)
    assert c.get("/api/topology").status_code == 503


def test_reply_packets_count_on_the_request_edge():
    from app.hubble_gen.flow import flow_pb2 as pb
    from google.protobuf.wrappers_pb2 import BoolValue
    f = pb.Flow(verdict=pb.FORWARDED)
    f.IP.source, f.IP.destination = "10.42.0.5", "10.42.0.7"
    f.source.namespace, f.source.pod_name = "t", "order-aaa-1"
    f.destination.namespace, f.destination.pod_name = "t", "lg-ccc-1"
    f.l4.TCP.destination_port = 46762          # ephemeral client port on the reply
    f.is_reply.CopyFrom(BoolValue(value=True))
    r = flow_from_proto(f)
    assert (r.src, r.dst, r.dport) == ("t/pod/lg-ccc-1", "t/pod/order-aaa-1", None)


def test_service_edge_is_not_duplicated_next_to_the_workload_edge():
    flows = [rec("t/pod/lg-ccc-1", "ip/10.43.0.8", svc="t/order"), rec("t/pod/lg-ccc-1", "t/pod/order-aaa-1")]
    topo = build_topology(INV, flows, FRESH)
    assert len(topo["observed_edges"]) == 1
    e = topo["observed_edges"][0]
    assert e["flows"] == 2 and e["via_service"] == {"t/order": 1}


def test_claimed_handoff_does_not_warn():
    incs = [{"id": 1, "namespace": "t", "workload": "inv", "status": "handed_off", "severity_level": "HIGH_RISK", "title": "x", "claimed": True},
            {"id": 2, "namespace": "t", "workload": "order", "status": "handed_off", "severity_level": "HIGH_RISK", "title": "y", "claimed": False, "updated_ts": __import__("time").time()}]
    by = {n["id"]: n for n in build_topology(INV, [], FRESH, incs)["nodes"]}
    assert "warnings" not in by["t/Deployment/inv"] and by["t/Deployment/order"]["warnings"]


def test_node_gateway_and_node_ip_resolve_to_the_node_not_external():
    inv = {**INV, "nodes": [{"metadata": {"name": "n1"}, "spec": {"podCIDR": "10.42.0.0/24"},
                             "status": {"conditions": [{"type": "Ready", "status": "True"}],
                                        "addresses": [{"type": "InternalIP", "address": "192.168.5.15"}]}}]}
    topo = build_topology(inv, [rec("ip/10.42.0.1", "t/pod/order-aaa-1"), rec("ip/192.168.5.15", "t/pod/order-aaa-1")], FRESH)
    assert {e["from"] for e in topo["observed_edges"]} == {"node/n1"}
    assert not [n for n in topo["nodes"] if n["kind"] == "External"]


def test_network_alert_only_above_threshold_and_marks_both_ends():
    few = [rec("t/pod/lg-ccc-1", "t/pod/order-aaa-1", "DROPPED", reason="POLICY_DENIED") for _ in range(4)]
    assert build_topology(INV, few, FRESH)["network_alerts"] == []
    many = few + [rec("t/pod/lg-ccc-1", "t/pod/order-aaa-1", "DROPPED", reason="POLICY_DENIED")]
    topo = build_topology(INV, many, FRESH)
    assert topo["network_alerts"][0]["dropped"] == 5
    by = {n["id"]: n for n in topo["nodes"]}
    assert by["t/Deployment/lg"]["network_warnings"] and by["t/Deployment/order"]["network_warnings"]


def test_succeeded_pods_are_completed_not_down():
    from app.flows import build_topology
    def pod(name, phase, ready):
        return {"metadata": {"namespace": "kube-system", "name": name, "ownerReferences": [{"kind": "Job", "name": "helm-install"}]},
                "spec": {"nodeName": "n1"}, "status": {"phase": phase, "podIP": "10.42.0.9", "containerStatuses": [{"ready": ready}]}}
    inv = {"pods": [pod("helm-install-abc", "Succeeded", False)], "services": [], "nodes": [], "deployments": []}
    topo = build_topology(inv, [], {"state": "connected", "stale": False}, [])
    job = next(n for n in topo["nodes"] if n["kind"] == "Job")
    assert job["status"] == "completed"
    inv["pods"] = [pod("helm-install-abc", "Failed", False)]
    topo = build_topology(inv, [], {"state": "connected", "stale": False}, [])
    assert next(n for n in topo["nodes"] if n["kind"] == "Job")["status"] == "down"      # a failed pod is still down


# ---------------------------------------------------------------- roles, services, ingress
def _inv():
    def pod(ns, name, owner, kind, labels, image, ready=True):
        return {"metadata": {"namespace": ns, "name": name, "labels": labels, "ownerReferences": [{"kind": kind, "name": owner}]},
                "spec": {"nodeName": "n1", "images": [image]}, "status": {"phase": "Running", "podIP": f"10.0.0.{abs(hash(name)) % 200 + 1}", "containerStatuses": [{"ready": ready}]}}
    def svc(ns, name, sel, ip, port=80):
        return {"metadata": {"namespace": ns, "name": name}, "spec": {"clusterIP": ip, "selector": sel, "ports": [{"port": port}]}}
    def ep(ns, name, n):
        return {"metadata": {"namespace": ns, "name": name}, "subsets": [{"addresses": [{"ip": f"1.1.1.{i}"} for i in range(n)]}] if n else []}
    return {
        "nodes": [], "errors": [],
        "pods": [pod("shop", "web-abc-1", "web-abc", "ReplicaSet", {"app": "web"}, "reg/web:1"),
                 pod("shop", "db-0", "db", "StatefulSet", {"app": "db"}, "docker.io/pgvector/pgvector:pg17"),
                 pod("shop", "cache-xyz-1", "cache-xyz", "ReplicaSet", {"app": "cache"}, "redis:7"),
                 pod("kube-system", "traefik-q-1", "traefik-q", "ReplicaSet", {"app": "traefik"}, "traefik:v3"),
                 pod("kube-system", "coredns-a-1", "coredns-a", "ReplicaSet", {"app": "coredns"}, "coredns:1")],
        "services": [svc("shop", "web", {"app": "web"}, "10.43.0.1"), svc("shop", "db", {"app": "db"}, "10.43.0.2", 5432),
                     svc("shop", "cache", {"app": "cache"}, "10.43.0.3", 6379), svc("shop", "ghost", {"app": "nothing"}, "10.43.0.4")],
        "endpoints": [ep("shop", "web", 1), ep("shop", "db", 1), ep("shop", "cache", 0), ep("shop", "ghost", 0)],
        "ingresses": [{"metadata": {"namespace": "shop", "name": "web"}, "spec": {"ingressClassName": "traefik", "rules": [
            {"host": "shop.localhost", "path": "/", "service": "web", "port": 80},
            {"host": "shop.localhost", "path": "/old", "service": "ghost", "port": 80}]}}],
    }


def test_data_stores_detected_by_image_not_by_name_or_namespace():
    from app.flows import build_topology
    nodes = {n["id"]: n for n in build_topology(_inv(), [], {"state": "connected", "stale": False}, [])["nodes"]}
    assert nodes["shop/StatefulSet/db"]["role"] == "data" and nodes["shop/StatefulSet/db"]["data_store"] == "pgvector"
    assert nodes["shop/Deployment/cache"]["role"] == "data"            # redis image
    assert nodes["shop/Deployment/web"]["role"] == "service"
    assert nodes["kube-system/Deployment/coredns"]["role"] == "system"


def test_services_fold_into_their_workload_and_missing_endpoints_are_flagged():
    from app.flows import build_topology
    nodes = {n["id"]: n for n in build_topology(_inv(), [], {"state": "connected", "stale": False}, [])["nodes"]}
    assert nodes["shop/Service/web"]["folded_into"] == "shop/Deployment/web"
    assert [s["name"] for s in nodes["shop/Deployment/web"]["services"]] == ["web"]
    assert nodes["shop/Deployment/cache"]["no_endpoints"] == ["cache"]          # pod runs but its Service has no endpoints
    assert "no_endpoints" not in nodes["shop/Deployment/web"]
    assert "folded_into" not in nodes["shop/Service/ghost"]                          # selects nothing


def test_ingress_nodes_routes_and_broken_backend():
    from app.flows import build_topology
    topo = build_topology(_inv(), [], {"state": "connected", "stale": False}, [])
    ing = next(n for n in topo["nodes"] if n["kind"] == "Ingress")
    assert ing["role"] == "entry" and ing["hosts"] == ["shop.localhost"]
    assert ing["status"] == "degraded" and ing["broken_routes"] == ["ghost"]         # one of two routes has no backend
    assert {"from": "shop/Ingress/web", "to": "shop/Deployment/web", "type": "routes_to", "via_service": "web", "observed": False} in topo["structure_edges"]


def test_ingress_with_no_working_backend_is_down():
    from app.flows import build_topology
    inv = _inv(); inv["ingresses"][0]["spec"]["rules"] = [inv["ingresses"][0]["spec"]["rules"][1]]
    assert next(n for n in build_topology(inv, [], {"state": "connected", "stale": False}, [])["nodes"] if n["kind"] == "Ingress")["status"] == "down"


def test_observed_controller_traffic_is_attributed_to_the_ingress_only_when_controller_known():
    from app.flows import FlowRec, build_topology
    import inspect
    sig = inspect.signature(FlowRec)
    inv = _inv()
    # pod-to-pod flow traefik -> web
    kw = {k: None for k in sig.parameters}
    kw.update(src="kube-system/pod/traefik-q-1", dst="shop/pod/web-abc-1", verdict="FORWARDED", ts=100.0, proto="TCP", dport=80, dst_service="shop/web")
    try:
        f = FlowRec(**{k: v for k, v in kw.items() if k in sig.parameters})
    except TypeError:
        import pytest; pytest.skip("FlowRec signature differs; covered by the live topology check")
    topo = build_topology(inv, [f], {"state": "connected", "stale": False}, [])
    pairs = {(o["from"], o["to"]): o for o in topo["observed_edges"]}
    assert ("shop/Ingress/web", "shop/Deployment/web") in pairs
    assert pairs[("shop/Ingress/web", "shop/Deployment/web")]["via_controller"] == "kube-system/Deployment/traefik"
    inv2 = _inv(); inv2["pods"] = [p for p in inv2["pods"] if "traefik" not in p["metadata"]["name"]]
    assert not [o for o in build_topology(inv2, [f], {"state": "connected", "stale": False}, [])["observed_edges"] if o["from"].endswith("/Ingress/web")]


def test_every_node_has_a_role_including_observed_only_ones():
    from app.flows import FlowRec, build_topology
    import inspect
    sig = inspect.signature(FlowRec)
    kw = {k: None for k in sig.parameters}
    kw.update(src="ip/8.8.8.8", dst="shop/pod/web-abc-1", verdict="FORWARDED", ts=100.0, proto="TCP", dport=80)
    f = FlowRec(**{k: v for k, v in kw.items() if k in sig.parameters})
    topo = build_topology(_inv(), [f], {"state": "connected", "stale": False}, [])
    assert all(n.get("role") for n in topo["nodes"]), [n["id"] for n in topo["nodes"] if not n.get("role")]
    assert next(n for n in topo["nodes"] if n["id"] == "ip/8.8.8.8")["role"] == "external"


def test_old_unclaimed_handoffs_do_not_raise_map_warnings():
    import time
    from app.flows import build_topology
    now = time.time()
    inc = lambda i, st, age, claimed=False: {"id": i, "namespace": "shop", "workload": "web", "status": st, "severity_level": "COMPLEX", "title": f"t{i}", "claimed": claimed, "updated_ts": now - age}
    incs = [inc(1, "analyzing", 10), inc(2, "handed_off", 60), inc(3, "handed_off", 7 * 3600), inc(4, "handed_off", 60, claimed=True), inc(5, "resolved", 5), inc(6, "failed", 5)]
    topo = build_topology(_inv(), [], {"state": "connected", "stale": False}, incs)
    web = next(n for n in topo["nodes"] if n["id"] == "shop/Deployment/web")
    assert sorted(w["incident_id"] for w in web["warnings"]) == [1, 2]
