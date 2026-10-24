"""P15 cluster operations overview. Measured series and Kubernetes facts, not a synthetic health score."""
from __future__ import annotations

import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import httpx
from kubernetes.utils.quantity import parse_quantity

from .flows import FlowStore, _wl_name, build_topology

# Preserve missing series as None. A ratio's denominator may be zero; NaN is not a healthy zero.
_POD_CPU = ('sum by(namespace,pod)(rate(container_cpu_usage_seconds_total{pod!="",container!="",container!="POD"}[2m])) '
            'or on(namespace,pod) max by(namespace,pod)(rate(container_cpu_usage_seconds_total{pod!="",container=""}[2m]))')
_POD_MEM = ('sum by(namespace,pod)(container_memory_working_set_bytes{pod!="",container!="",container!="POD"}) '
            'or on(namespace,pod) max by(namespace,pod)(container_memory_working_set_bytes{pod!="",container=""})')
_PAIR = "source_workload_namespace,source_workload,destination_workload_namespace,destination_workload"
QUERIES = {
    "pod_cpu": _POD_CPU,
    "pod_mem": _POD_MEM,
    "pod_restarts": 'sum by(namespace,pod)(increase(kube_pod_container_status_restarts_total[1h]))',
    "node_cpu": 'max by(node)(rate(container_cpu_usage_seconds_total{id="/"}[2m]))',
    "node_mem": 'max by(node)(container_memory_working_set_bytes{id="/"})',
    "req_rate": 'sum by(namespace,app)(rate(http_server_requests_seconds_count[2m]))',
    "err_rate": ('sum by(namespace,app)(rate(http_server_requests_seconds_count{status=~"5.."}[2m])) / '
                 'sum by(namespace,app)(rate(http_server_requests_seconds_count[2m]))'),
    "p95_ms": ('1000 * histogram_quantile(0.95,sum by(namespace,app,le)('
               'rate(http_server_requests_seconds_bucket{uri!~"/actuator.*"}[2m]))) '
               # Fallback only where the app exposes no histogram: server-side Envoy latency (raw, milliseconds).
               'or on(namespace,app) histogram_quantile(0.95,sum by(namespace,app,le)('
               'label_replace(rate(istio_request_duration_milliseconds_bucket{reporter="destination"}[2m]),'
               '"app","$1","destination_app","(.+)")))'),
    "edge_req": f'sum by({_PAIR})(rate(istio_requests_total{{reporter="destination"}}[2m]))',
    "edge_err": (f'sum by({_PAIR})(rate(istio_requests_total{{reporter="destination",response_code=~"5.."}}[2m])) / '
                 f'sum by({_PAIR})(rate(istio_requests_total{{reporter="destination"}}[2m]))'),
}


def fetch_metrics(settings) -> tuple[dict, dict]:
    """Bounded parallel raw Prometheus queries. Failures are returned as source metadata, never replaced by zeros."""
    def query(item):
        key, q = item
        try:
            r = httpx.get(f"{settings.prometheus_url}/api/v1/query", params={"query": q}, timeout=4)
            r.raise_for_status()
            b = r.json()
            if b.get("status") != "success" or b.get("data", {}).get("resultType") != "vector":
                return key, [], "invalid_response"
            return key, b["data"]["result"], None
        except Exception as e:  # noqa: BLE001 - one failed source cannot blank the cluster map
            return key, [], type(e).__name__
    values, errors = {}, {}
    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="overview-prom") as pool:
        for key, rows, err in pool.map(query, QUERIES.items()):
            values[key] = rows
            if err:
                errors[key] = err
    return values, ({"prometheus": ", ".join(f"{k}:{v}" for k, v in errors.items())} if errors else {})


def _number(v: Any) -> float | None:
    try:
        n = float(v)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _quantity(v: Any) -> float | None:
    try:
        return float(parse_quantity(v)) if v is not None else None
    except (ValueError, TypeError, ArithmeticError):
        return None


def _index(rows: list, labels: tuple[str, ...]) -> dict:
    out = {}
    for r in rows:
        metric = r.get("metric") or {}
        val = r.get("value") or []
        if len(val) == 2:
            out[tuple(metric.get(k, "") for k in labels)] = _number(val[1])
    return out


def _sum_seen(values) -> float | None:
    seen = [v for v in values if v is not None]
    return sum(seen) if seen else None


def _event(e: dict) -> dict:
    md = e.get("metadata") or {}
    ob = e.get("involvedObject") or e.get("involved_object") or e.get("regarding") or {}
    at = (e.get("lastTimestamp") or e.get("last_timestamp") or e.get("eventTime") or e.get("event_time") or
          (e.get("series") or {}).get("lastObservedTime") or md.get("creationTimestamp") or md.get("creation_timestamp"))
    return {"at": str(at) if at else None, "namespace": md.get("namespace"),
            "object": f"{ob.get('kind', '')}/{ob.get('name', '')}", "reason": e.get("reason"), "type": e.get("type"),
            "message": e.get("message") or e.get("note") or "", "count": e.get("count") or (e.get("series") or {}).get("count") or 1}


def assemble(inventory: dict, values: dict, *, events: list | None = None, incidents: list | None = None,
             today: dict | None = None, errors: dict | None = None, store: FlowStore | None = None) -> dict:
    """Assemble one response. `inventory` has no Secret values; raw Warning messages are preserved verbatim."""
    incidents, errors = incidents or [], errors or {}
    store = store or FlowStore()
    freshness = store.freshness()
    topo = build_topology(inventory, store.snapshot(), freshness, incidents)
    tnodes = {n["id"]: n for n in topo["nodes"]}
    # Inventory also includes deployments with no pods (scaled to zero or still waiting for scheduling).
    for d in inventory.get("deployments", []):
        md, sp, st = d.get("metadata") or {}, d.get("spec") or {}, d.get("status") or {}
        wid = f"{md.get('namespace')}/Deployment/{md.get('name')}"
        ready = st.get("readyReplicas", st.get("ready_replicas", 0))
        desired = sp.get("replicas")
        n = tnodes.setdefault(wid, {"id": wid, "namespace": md.get("namespace"), "name": md.get("name"),
                                   "kind": "Deployment", "role": "service", "pods": 0, "ready": ready})
        n["desired"], n["ready"] = desired, ready
        n["status"] = "unknown" if desired is None else "scaled_down" if desired == 0 else (
            "ready" if ready >= desired else "degraded" if ready else "down")
    podmap: dict[str, list[tuple]] = defaultdict(list)
    pods_by_ns: dict[str, list[dict]] = defaultdict(list)
    pods_by_node: dict[str, int] = defaultdict(int)
    for p in inventory.get("pods", []):
        md, st = p.get("metadata") or {}, p.get("status") or {}
        ns, pod = md.get("namespace"), md.get("name")
        pods_by_ns[ns].append(p)
        wid = f"{ns}/{_wl_name(p)}"
        if st.get("phase") not in ("Succeeded", "Failed"):
            pods_by_node[(p.get("spec") or {}).get("nodeName")] += 1
        podmap[wid].append((ns, pod))
    indexes = {k: _index(values.get(k) or [], ("namespace", "pod")) for k in ("pod_cpu", "pod_mem", "pod_restarts")}
    apps = {k: _index(values.get(k) or [], ("namespace", "app")) for k in ("req_rate", "err_rate", "p95_ms")}
    latest = {}
    for inc in incidents:
        latest.setdefault((inc.get("namespace"), inc.get("workload")), inc)
    workloads = []
    for n in sorted(tnodes.values(), key=lambda x: x["id"]):
        if n["kind"] not in ("Deployment", "StatefulSet", "DaemonSet", "Job", "Pod"):
            continue
        wid, pair = n["id"], (n.get("namespace"), n.get("name"))
        inc = latest.get(pair)
        metric = {"cpu": _sum_seen(indexes["pod_cpu"].get(p) for p in podmap[wid]),
                  "mem": _sum_seen(indexes["pod_mem"].get(p) for p in podmap[wid]),
                  "restarts_1h": _sum_seen(indexes["pod_restarts"].get(p) for p in podmap[wid]),
                  **{k: apps[k].get(pair) for k in apps}}
        w = {"id": wid, "namespace": n.get("namespace"), "name": n["name"], "kind": n["kind"],
             "ready": n.get("ready"), "desired": n.get("desired", n.get("pods")), **metric,
             "incident": {"id": inc["id"], "level": inc.get("severity_level"), "status": inc["status"], "stage": inc.get("stage")} if inc else None}
        workloads.append(w)
        n.update(metric)
        if inc and not n.get("warnings"):
            n["warnings"] = [{"incident_id": inc["id"], "level": inc.get("severity_level"), "status": inc["status"], "title": inc.get("title")}]
    all_ns = set(pods_by_ns) | {n.get("namespace") for n in tnodes.values() if n.get("namespace")} | {
        (n.get("metadata") or {}).get("name") for n in inventory.get("namespaces", [])}
    ns_rows = []
    for ns in sorted(n for n in all_ns if n):
        ps, ws = pods_by_ns[ns], [w for w in workloads if w["namespace"] == ns]
        ns_rows.append({"name": ns,
                        **{f"pods_{phase.lower()}": sum((p.get("status") or {}).get("phase") == phase for p in ps)
                           for phase in ("Running", "Pending", "Failed")},
                        "restarts_1h": _sum_seen(w["restarts_1h"] for w in ws),
                        "cpu_cores": _sum_seen(w["cpu"] for w in ws), "mem_bytes": _sum_seen(w["mem"] for w in ws),
                        "workloads": len(ws), "workloads_unhealthy": sum(tnodes[w["id"]].get("status") in ("down", "degraded", "not_ready") for w in ws)})
    node_cpu = _index(values.get("node_cpu") or [], ("node",))
    node_mem = _index(values.get("node_mem") or [], ("node",))
    nodes = []
    for n in inventory.get("nodes", []):
        name, st = (n.get("metadata") or {}).get("name"), n.get("status") or {}
        conditions = st.get("conditions") or []
        ready = next((c.get("status") == "True" for c in conditions if c.get("type") == "Ready"), None)
        capacity = st.get("capacity") or {}
        nodes.append({"name": name, "ready": ready, "cpu_cores": _quantity(capacity.get("cpu")),
                      "mem_bytes": _quantity(capacity.get("memory")), "cpu_used_cores": node_cpu.get((name,)),
                      "mem_used_bytes": node_mem.get((name,)), "pods": pods_by_node[name]})
    labels = ("source_workload_namespace", "source_workload", "destination_workload_namespace", "destination_workload")
    er, ee = _index(values.get("edge_req") or [], labels), _index(values.get("edge_err") or [], labels)
    observed = {(e["from"], e["to"]): e for e in topo["observed_edges"]}
    pair_nodes = {(n.get("namespace"), n.get("name")): n["id"] for n in tnodes.values() if n["kind"] in ("Deployment", "StatefulSet", "DaemonSet", "Pod")}
    for pair, req in er.items():
        src, dst = pair_nodes.get(pair[:2]), pair_nodes.get(pair[2:])
        if not src or not dst or src == dst or req is None:
            continue
        e = observed.setdefault((src, dst), {"from": src, "to": dst, "flows": 0, "ok": 0, "dropped": 0, "errors": 0,
                                            "last_seen": None, "ports": {}, "drop_reasons": {}, "via_service": {},
                                            "state": "unknown"})
        e.update(req_rate=req, err_rate=ee.get(pair))
        if (ee.get(pair) or 0) > 0:
            e["state"] = "degraded"
    topo["nodes"] = sorted(tnodes.values(), key=lambda n: n["id"])
    topo["observed_edges"] = list(observed.values())
    kube_errors = list(inventory.get("errors") or []) + [f"{k}:{v}" for k, v in errors.items() if k != "prometheus"]
    sources = {"prometheus": f"error: {errors['prometheus']}" if errors.get("prometheus") else "ok",
               "kubernetes": "error: " + "; ".join(kube_errors) if kube_errors else "ok",
               "hubble": freshness.get("stream_state") or freshness.get("state") or "never_connected",
               "envoy": "ok" if er else "미관측"}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cluster": {"nodes": nodes, "namespaces": ns_rows},
            "workloads": workloads, "events": sorted((_event(e) for e in events or []), key=lambda e: e["at"] or "", reverse=True)[:80],
            "incidents": {"open": incidents, "today": today or {"total": 0, "resolved": 0, "handed_off": 0, "failed": 0}},
            "topology": topo, "sources": sources}
