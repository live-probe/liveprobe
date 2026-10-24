"""Network-flow collector and cluster topology builder (P3).

Two sources are kept strictly apart, because "configured" is not "observed":

* inventory  - what exists and how it is wired (Kubernetes API: nodes, workloads, services, selectors).
               Drawn as dashed *structure* edges.
* flows      - what Hubble (Retina) actually saw on the wire. Drawn as solid *observed* edges.

A pair that has structure but no observed flow is reported as `unobserved`, never as "healthy" or
"no traffic": the collector only sees what its agent sees, and the response says how fresh that is.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

VERDICT_FORWARDED, VERDICT_DROPPED, VERDICT_ERROR = "FORWARDED", "DROPPED", "ERROR"
EXTERNAL = "external"          # anything outside the cluster (no namespace/pod identity)
WORLD_NS = "_external"
DROP_ALERT_MIN = 5
HANDOFF_FRESH_S = 6 * 3600     # an unclaimed hand-off raises a map warning for this long            # blocked/errored flows on one edge within the window that raise a map warning
STALE_AFTER_S = 60.0           # no flow received for this long => the view says "delayed"
WINDOW_S = 300.0               # edge statistics cover the last five minutes


@dataclass(frozen=True)
class FlowRec:
    """One observed flow reduced to the fields the map needs (no payload, no headers)."""
    ts: float
    src: str            # node key, e.g. "liveprobe-target/pod/order-xxx" or "ip/10.43.0.10"
    dst: str
    verdict: str        # FORWARDED | DROPPED | ERROR
    proto: str          # TCP | UDP | ICMP | OTHER
    dport: int | None
    drop_reason: str | None = None
    src_ns: str | None = None
    dst_ns: str | None = None
    src_name: str | None = None
    dst_name: str | None = None
    dst_service: str | None = None   # "ns/service" when Hubble resolved it
    is_reply: bool = False
    flow_id: str | None = None  # Hubble UUID: distinguish observations from reconnect replay


def endpoint_key(ns: str | None, pod: str | None, ip: str | None) -> tuple[str, str | None]:
    """Stable key + namespace for an endpoint. Pods are keyed by name, everything else by IP."""
    if pod:
        return f"{ns or '?'}/pod/{pod}", ns or None
    if ip and ip not in ("0.0.0.0", ""):
        return f"ip/{ip}", None
    return "ip/unknown", None


def flow_from_proto(f: Any) -> FlowRec | None:
    """Convert a Hubble `Flow` message. Returns None for flows that carry no usable identity."""
    from .hubble_gen.flow import flow_pb2 as pb  # local import: stubs are only needed at runtime
    sip, dip = (f.IP.source or None), (f.IP.destination or None)
    sk, sns = endpoint_key(f.source.namespace, f.source.pod_name, sip)
    dk, dns = endpoint_key(f.destination.namespace, f.destination.pod_name, dip)
    if sk == dk == "ip/unknown":
        return None  # kernel-level noise (0.0.0.0 -> 0.0.0.0), no endpoint to draw
    which = f.l4.WhichOneof("protocol")
    proto, dport = "OTHER", None
    if which == "TCP":
        proto, dport = "TCP", f.l4.TCP.destination_port or None
    elif which == "UDP":
        proto, dport = "UDP", f.l4.UDP.destination_port or None
    elif which in ("ICMPv4", "ICMPv6"):
        proto = "ICMP"
    verdict = pb.Verdict.Name(f.verdict)
    if verdict not in (VERDICT_FORWARDED, VERDICT_DROPPED, VERDICT_ERROR):
        verdict = VERDICT_FORWARDED if verdict in ("TRANSLATED", "TRACED", "REDIRECTED", "AUDIT") else verdict
    reason = pb.DropReason.Name(f.drop_reason_desc) if verdict == VERDICT_DROPPED and f.drop_reason_desc else None
    svc = f"{f.destination_service.namespace}/{f.destination_service.name}" if f.destination_service.name else None
    ts = f.time.seconds + f.time.nanos / 1e9 if f.HasField("time") else time.time()
    reply = bool(f.is_reply.value) if f.HasField("is_reply") else False
    if reply:  # a reply packet belongs to the request edge: swap so both directions count as one connection
        return FlowRec(ts, dk, sk, verdict, proto, None, reason, dns, sns,
                       f.destination.pod_name or dip, f.source.pod_name or sip, svc, True, f.uuid or None)
    return FlowRec(ts, sk, dk, verdict, proto, dport, reason, sns, dns,
                   f.source.pod_name or sip, f.destination.pod_name or dip, svc, flow_id=f.uuid or None)


# --------------------------------------------------------------------------- aggregation
@dataclass
class EdgeStat:
    src: str
    dst: str
    ok: int = 0
    dropped: int = 0
    error: int = 0
    last_ts: float = 0.0
    ports: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    drop_reasons: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    via_service: dict[str, int] = field(default_factory=lambda: defaultdict(int))


class FlowStore:
    """Bounded in-memory window of recent flows aggregated per (src, dst). Thread-safe."""

    def __init__(self, window_s: float = WINDOW_S, max_flows: int = 50_000, clock: Callable[[], float] = time.time):
        self.window_s, self.max_flows, self.clock = window_s, max_flows, clock
        self._flows: list[FlowRec] = []
        self._lock = threading.Lock()
        self.last_received: float | None = None
        self.received_total = 0
        self.stream_state = "never_connected"   # connected | reconnecting | never_connected
        self.last_error: str | None = None
        self.nodes_connected: int | None = None

    def add(self, flows: Iterable[FlowRec]) -> None:
        batch = list(flows)
        with self._lock:
            self._flows.extend(batch)
            self.received_total += len(batch)
            if batch:
                self.last_received = self.clock()
            self._trim()

    def _trim(self) -> None:
        cutoff = self.clock() - self.window_s
        if self._flows and (self._flows[0].ts < cutoff or len(self._flows) > self.max_flows):
            self._flows = [f for f in self._flows if f.ts >= cutoff][-self.max_flows:]

    def snapshot(self) -> list[FlowRec]:
        with self._lock:
            self._trim()
            return list(self._flows)

    def freshness(self) -> dict:
        now = self.clock()
        age = None if self.last_received is None else round(now - self.last_received, 1)
        stale = self.stream_state != "connected" or age is None or age > STALE_AFTER_S
        return {"state": self.stream_state, "last_flow_age_s": age, "stale": stale,
                "stale_after_s": STALE_AFTER_S, "window_s": self.window_s, "received_total": self.received_total,
                "nodes_connected": self.nodes_connected, "error": self.last_error}


def aggregate(flows: Iterable[FlowRec]) -> dict[tuple[str, str], EdgeStat]:
    edges: dict[tuple[str, str], EdgeStat] = {}
    for f in flows:
        e = edges.setdefault((f.src, f.dst), EdgeStat(f.src, f.dst))
        if f.verdict == VERDICT_DROPPED:
            e.dropped += 1
            if f.drop_reason:
                e.drop_reasons[f.drop_reason] += 1
        elif f.verdict == VERDICT_ERROR:
            e.error += 1
        else:
            e.ok += 1
        e.last_ts = max(e.last_ts, f.ts)
        if f.dport:
            e.ports[f"{f.proto}/{f.dport}"] += 1
        if f.dst_service:
            e.via_service[f.dst_service] += 1
    return edges


# --------------------------------------------------------------------------- topology
def _wl_name(pod: dict) -> str:
    """Workload name for a pod: owning Deployment/StatefulSet/DaemonSet, else the pod itself."""
    labels = (pod.get("metadata") or {}).get("labels") or {}
    for o in (pod.get("metadata") or {}).get("ownerReferences") or []:
        kind, name = o.get("kind"), o.get("name", "")
        if kind == "ReplicaSet" and "-" in name:
            return "Deployment/" + name.rsplit("-", 1)[0]
        if kind in ("StatefulSet", "DaemonSet", "Job"):
            return f"{kind}/{name}"
    return "Pod/" + ((pod.get("metadata") or {}).get("name") or labels.get("app", "?"))


# --------------------------------------------------------------------------- roles, services, ingress
INFRA_NAMESPACES = ("kube-system", "kube-public", "kube-node-lease", "liveprobe-observability")
DATA_IMAGE_HINTS = ("postgres", "pgvector", "mysql", "mariadb", "redis", "valkey", "mongo", "memcached", "cassandra",
                    "elasticsearch", "opensearch", "kafka", "rabbitmq", "etcd", "clickhouse", "influxdb", "minio")


def data_store_of(kind: str, name: str, imgs: set[str]) -> str | None:
    """A workload is a data store when an image says so. A bare StatefulSet is only a candidate (not proof)."""
    for img in imgs:
        base = img.rsplit("/", 1)[-1].lower()
        for h in DATA_IMAGE_HINTS:
            if h in base:
                return h
    return None


def _annotate_roles(nodes: dict, structure: list, svc_to_wl: dict, images: dict, inventory: dict) -> None:
    """Adds, in place: Services attached to the workload behind them, Ingress nodes with their routes, and a `role`
    (entry | service | data | system | job) on every node. Services stay in `nodes` (flows still resolve to them), but
    carry `folded_into` so the map can draw one card per workload instead of a card for each Service."""
    by_wl: dict[str, list[dict]] = {}
    for sid, wid in svc_to_wl.items():
        s = nodes.get(sid)
        if not s or wid not in nodes:
            continue
        s["folded_into"] = wid
        by_wl.setdefault(wid, []).append({"name": s["name"], "ports": s.get("ports") or [], "endpoints_ready": s.get("endpoints_ready")})
    for wid, svcs in by_wl.items():
        w = nodes[wid]
        w["services"] = svcs
        if w.get("status") != "completed" and w.get("pods") and any(x["endpoints_ready"] == 0 for x in svcs):
            w["no_endpoints"] = [x["name"] for x in svcs if x["endpoints_ready"] == 0]

    controller_ids: dict[str, str] = {}      # ingress class -> controller workload id
    for ing in inventory.get("ingresses", []):
        md, sp = ing.get("metadata") or {}, ing.get("spec") or {}
        ns, name = md.get("namespace"), md.get("name")
        iid = f"{ns}/Ingress/{name}"
        routes, hosts, bad = [], [], 0
        for r in sp.get("rules") or []:
            sid = f"{ns}/Service/{r.get('service')}"
            wid = svc_to_wl.get(sid)
            svc = nodes.get(sid)
            ok = bool(svc) and (svc.get("endpoints_ready") or 0) > 0
            bad += 0 if ok else 1
            if r.get("host") and r["host"] not in hosts:
                hosts.append(r["host"])
            routes.append({"host": r.get("host"), "path": r.get("path"), "service": r.get("service"), "port": r.get("port"),
                           "workload": wid, "backend_ready": ok, "backend_found": bool(svc)})
            if wid:
                structure.append({"from": iid, "to": wid, "type": "routes_to", "via_service": r.get("service")})
        nodes[iid] = {"id": iid, "kind": "Ingress", "name": name, "namespace": ns, "status": "degraded" if bad and bad < len(routes) else ("down" if bad else "ready"),
                      "hosts": hosts, "routes": routes, "ingress_class": sp.get("ingressClassName"), "role": "entry"}
        nodes[iid]["broken_routes"] = [r["service"] for r in routes if not r["backend_ready"]]

    for nid, n in nodes.items():
        if n.get("role"):
            continue
        k, ns = n["kind"], n.get("namespace")
        if k in ("Node",):
            n["role"] = "node"
        elif k in ("External", "Unresolved"):
            n["role"] = "external"
        elif k == "Service":
            n["role"] = "service"
        else:
            store = data_store_of(k, n["name"], images.get(nid, set()))
            if store:
                n["role"], n["data_store"] = "data", store
            elif k == "Job":
                n["role"] = "job"
            elif ns in INFRA_NAMESPACES:
                n["role"] = "system"
            else:
                n["role"] = "service"
        if n["role"] == "data" and ns in INFRA_NAMESPACES:
            n["role"] = "data"


def ingress_controller_of(nodes: dict, ingress_class: str | None) -> str | None:
    """Workload that serves an Ingress class (name match on the class, else the sole controller-looking workload)."""
    cands = [n for n in nodes.values() if n.get("kind") in ("Deployment", "DaemonSet") and n.get("namespace") in INFRA_NAMESPACES
             and any(h in n["name"] for h in ("traefik", "nginx", "haproxy", "ingress", "istio-ingress", "envoy", "contour"))
             and not n["name"].startswith("svclb")]
    if ingress_class:
        for n in cands:
            if ingress_class.lower() in n["name"].lower():
                return n["id"]
    return cands[0]["id"] if len(cands) == 1 else None



def build_topology(inventory: dict, flows: list[FlowRec], freshness: dict, incidents: list[dict] | None = None, cfg=None) -> dict:
    """Merge Kubernetes inventory with observed flows into one drawable graph.

    inventory = {"nodes": [...], "pods": [...], "services": [...], "endpoints": [...]} (plain dicts).
    Pod-level flow endpoints are folded into their workload; Service ClusterIPs are resolved to the
    Service node when the inventory knows them.
    """
    from .config import AppConfig
    cfg = cfg or AppConfig()
    nodes: dict[str, dict] = {}
    structure: list[dict] = []
    pod_to_wl: dict[str, str] = {}
    ip_to_node: dict[str, str] = {}
    images: dict[str, set[str]] = {}

    for n in inventory.get("nodes", []):
        md = n.get("metadata") or {}
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in (n.get("status") or {}).get("conditions") or [])
        nodes[f"node/{md.get('name')}"] = {"id": f"node/{md.get('name')}", "kind": "Node", "name": md.get("name"),
                                           "namespace": None, "status": "ready" if ready else "not_ready"}
    import ipaddress
    for n in inventory.get("nodes", []):  # node addresses and the pod-network gateway (cni0) are the node, not "external"
        nid = f"node/{(n.get('metadata') or {}).get('name')}"
        for a in (n.get("status") or {}).get("addresses") or []:
            if a.get("type") == "InternalIP":
                ip_to_node[f"ip/{a.get('address')}"] = nid
        for cidr in ((n.get("spec") or {}).get("podCIDRs") or [(n.get("spec") or {}).get("podCIDR")]):
            try:
                ip_to_node[f"ip/{next(ipaddress.ip_network(cidr).hosts())}"] = nid
            except (ValueError, TypeError, StopIteration):
                pass
    for p in inventory.get("pods", []):
        md, st = p.get("metadata") or {}, p.get("status") or {}
        ns, name = md.get("namespace"), md.get("name")
        wl = _wl_name(p)
        wid = f"{ns}/{wl}"
        pod_to_wl[f"{ns}/pod/{name}"] = wid
        if st.get("podIP"):
            ip_to_node[f"ip/{st['podIP']}"] = wid
        w = nodes.setdefault(wid, {"id": wid, "kind": wl.split("/")[0], "name": wl.split("/", 1)[1], "namespace": ns,
                                   "system": (md.get("labels") or {}).get("app.kubernetes.io/part-of") or None,
                                   "pods": 0, "ready": 0, "status": "unknown", "node": (p.get("spec") or {}).get("nodeName")})
        for img in (p.get("spec") or {}).get("images") or []:
            if img:
                images.setdefault(wid, set()).add(img)
        cs = st.get("containerStatuses") or []
        if st.get("phase") == "Succeeded":     # a finished Job/one-shot pod is neither ready nor broken
            w["completed"] = w.get("completed", 0) + 1
            continue
        w["pods"] += 1
        if st.get("phase") == "Running" and cs and all(c.get("ready") for c in cs):
            w["ready"] += 1
    # Deployment identity/labels take precedence over stale Pod-template labels.
    for d in inventory.get("deployments", []):
        md, sp, st = d.get("metadata") or {}, d.get("spec") or {}, d.get("status") or {}
        ns, name = md.get("namespace"), md.get("name")
        wid = f"{ns}/Deployment/{name}"
        w = nodes.setdefault(wid, {"id": wid, "kind": "Deployment", "name": name, "namespace": ns,
                                   "pods": 0, "ready": 0, "status": "unknown"})
        w["system"] = (md.get("labels") or {}).get("app.kubernetes.io/part-of") or None
        w["desired"] = sp.get("replicas")
    for w in nodes.values():
        if "pods" in w:
            if w["pods"] == 0 and w.get("completed"):
                w["status"] = "completed"
            else:
                w["status"] = "ready" if w["ready"] == w["pods"] and w["pods"] else ("degraded" if w["ready"] else "down")
            if w.get("node") and f"node/{w['node']}" in nodes:
                structure.append({"from": w["id"], "to": f"node/{w['node']}", "type": "runs_on"})
    for s in inventory.get("services", []):
        md, sp = s.get("metadata") or {}, s.get("spec") or {}
        ns, name = md.get("namespace"), md.get("name")
        sid = f"{ns}/Service/{name}"
        nodes[sid] = {"id": sid, "kind": "Service", "name": name, "namespace": ns, "status": "unknown",
                      "system": (md.get("labels") or {}).get("app.kubernetes.io/part-of") or None,
                      "cluster_ip": sp.get("clusterIP"), "ports": [p.get("port") for p in sp.get("ports") or []]}
        if sp.get("clusterIP") and sp["clusterIP"] != "None":
            ip_to_node[f"ip/{sp['clusterIP']}"] = sid
        sel = sp.get("selector") or {}
        if sel:
            for p in inventory.get("pods", []):
                pmd = p.get("metadata") or {}
                if pmd.get("namespace") == ns and all((pmd.get("labels") or {}).get(k) == v for k, v in sel.items()):
                    structure.append({"from": sid, "to": f"{ns}/{_wl_name(p)}", "type": "selects"})
    structure = [dict(t) for t in {(e["from"], e["to"], e["type"]): e for e in structure}.values()]
    for ep in inventory.get("endpoints", []):
        md = ep.get("metadata") or {}
        sid = f"{md.get('namespace')}/Service/{md.get('name')}"
        if sid in nodes:
            ready = sum(len(s.get("addresses") or []) for s in ep.get("subsets") or [])
            nodes[sid]["endpoints_ready"] = ready
            nodes[sid]["status"] = "ready" if ready else "no_endpoints"

    svc_to_wl: dict[str, str] = {}
    for e in structure:
        if e["type"] == "selects":
            svc_to_wl.setdefault(e["from"], e["to"])
    _annotate_roles(nodes, structure, svc_to_wl, images, inventory)

    def fold(key: str) -> str:
        n = pod_to_wl.get(key) or ip_to_node.get(key) or key
        return svc_to_wl.get(n, n)   # Service endpoints (ClusterIP) -> the workload that backs them

    # observed edges
    folded: dict[tuple[str, str], EdgeStat] = {}
    for (s, d), e in aggregate(flows).items():
        fs, fd = fold(s), fold(d)
        if fs == fd:
            continue
        agg = folded.setdefault((fs, fd), EdgeStat(fs, fd))
        agg.ok += e.ok; agg.dropped += e.dropped; agg.error += e.error; agg.last_ts = max(agg.last_ts, e.last_ts)
        for k, v in e.ports.items(): agg.ports[k] += v
        for k, v in e.drop_reasons.items(): agg.drop_reasons[k] += v
        for k, v in e.via_service.items(): agg.via_service[k] += v
    observed = []
    for (s, d), e in folded.items():
        for key in (s, d):
            if key not in nodes:
                ext = key.startswith("ip/")
                nodes[key] = {"id": key, "kind": "External" if ext else "Unresolved", "name": key.split("/", 1)[-1],
                              "namespace": None, "status": "observed_only", "role": "external"}
        total = e.ok + e.dropped + e.error
        observed.append({"from": s, "to": d, "flows": total, "ok": e.ok, "dropped": e.dropped, "errors": e.error,
                         "state": "blocked" if e.dropped and not e.ok else "degraded" if e.dropped or e.error else "ok",
                         "last_seen": e.last_ts, "ports": dict(sorted(e.ports.items(), key=lambda kv: -kv[1])[:5]),
                         "drop_reasons": dict(e.drop_reasons), "via_service": dict(e.via_service)})
    # An Ingress is not a workload and never appears in a flow. Traffic that its controller was observed sending to a
    # route's backend is attributed to the Ingress, labelled as such; nothing is drawn when the controller is unknown.
    seen_pairs = {(o["from"], o["to"]): o for o in observed}
    for n in [x for x in nodes.values() if x["kind"] == "Ingress"]:
        ctrl = ingress_controller_of(nodes, n.get("ingress_class"))
        if not ctrl:
            continue
        for r in n["routes"]:
            o = seen_pairs.get((ctrl, r["workload"])) if r.get("workload") else None
            if o:
                observed.append({**o, "from": n["id"], "to": r["workload"], "via_controller": ctrl})
    # structure edges that have no observed traffic in either direction are reported, not hidden
    seen = {(o["from"], o["to"]) for o in observed} | {(o["to"], o["from"]) for o in observed}
    for e in structure:
        e["observed"] = (e["from"], e["to"]) in seen or any(
            o["via_service"] and e["from"].split("/Service/")[-1] in "".join(o["via_service"]) for o in observed)

    warnings = {}
    for inc in incidents or []:
        if inc.get("status") in ("resolved", "failed", "closed"):
            continue
        if inc.get("status") == "handed_off":
            # An old hand-off nobody picked up is history, not a live problem; it stays in the incident list.
            if inc.get("claimed") or time.time() - float(inc.get("updated_ts") or 0) > HANDOFF_FRESH_S:
                continue
        wid = f"{inc.get('namespace')}/Deployment/{inc.get('workload')}"
        warnings.setdefault(wid, []).append({"incident_id": inc["id"], "level": inc.get("severity_level"), "status": inc.get("status"), "title": inc.get("title")})
    for wid, items in warnings.items():
        if wid in nodes:
            nodes[wid]["warnings"] = items
    # network warnings come from observed drops/errors only; they never claim a cause
    network_alerts = [{"kind": "network_drop", "from": o["from"], "to": o["to"], "dropped": o["dropped"], "errors": o["errors"],
                       "drop_reasons": o["drop_reasons"], "last_seen": o["last_seen"]}
                      for o in observed if o["dropped"] + o["errors"] >= DROP_ALERT_MIN]
    for a in network_alerts:
        for key in (a["from"], a["to"]):
            if key in nodes:
                nodes[key].setdefault("network_warnings", []).append({"peer": a["to"] if key == a["from"] else a["from"],
                                                                    "dropped": a["dropped"], "errors": a["errors"]})
    groups = {}
    for n in nodes.values():
        n.setdefault("system", None)
        n["environment"] = cfg.environment(n.get("namespace"))
        if n["environment"] and n["system"]:
            gid = f"{n['environment']}/system/{n['system']}"
            n["group"] = gid
            groups[gid] = {"id": gid, "kind": "system", "environment": n["environment"], "system": n["system"], "label": n["system"]}
        elif n.get("namespace") and not n["environment"]:
            ns = n["namespace"]
            groups.setdefault(ns, {"id": ns, "kind": "namespace", "label": ns})
    incident_rows = [{**i, "system": i.get("system"), "environment": cfg.environment(i.get("namespace"))} for i in incidents or []]
    return {"groups": list(groups.values()), "incidents": incident_rows,
            "generated_at": time.time(), "freshness": freshness, "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
            "structure_edges": structure, "observed_edges": sorted(observed, key=lambda e: -e["flows"]),
            "network_alerts": network_alerts}


# --------------------------------------------------------------------------- Hubble client
class HubbleCollector:
    """Background gRPC stream from Hubble Relay into a FlowStore. Reconnects with backoff."""

    def __init__(self, store: FlowStore, target: str, channel_factory: Callable[[str], Any] | None = None):
        self.store, self.target = store, target
        self._channel_factory = channel_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="hubble-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import grpc
        from .hubble_gen.observer import observer_pb2 as o, observer_pb2_grpc as og
        delay = 1.0
        while not self._stop.is_set():
            try:
                ch = (self._channel_factory or grpc.insecure_channel)(self.target)
                stub = og.ObserverStub(ch)
                st = stub.ServerStatus(o.ServerStatusRequest(), timeout=10)
                self.store.nodes_connected = st.num_connected_nodes.value
                req = o.GetFlowsRequest(follow=True, since=_ts(time.time() - 120))
                self.store.stream_state, self.store.last_error, delay = "connected", None, 1.0
                batch: list[FlowRec] = []
                last_flush = time.monotonic()
                for resp in stub.GetFlows(req):
                    if self._stop.is_set():
                        break
                    if resp.WhichOneof("response_types") == "flow":
                        rec = flow_from_proto(resp.flow)
                        if rec:
                            batch.append(rec)
                    if batch and (len(batch) >= 200 or time.monotonic() - last_flush > 1.0):
                        self.store.add(batch); batch, last_flush = [], time.monotonic()
                if batch:
                    self.store.add(batch)
            except Exception as e:  # noqa: BLE001 - any stream failure is surfaced as stale, never as "no traffic"
                self.store.stream_state, self.store.last_error = "reconnecting", type(e).__name__
                log.warning("hubble stream failed: %s", type(e).__name__)
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 30.0)


def _ts(epoch: float):
    from google.protobuf.timestamp_pb2 import Timestamp
    t = Timestamp()
    t.FromNanoseconds(int(epoch * 1e9))
    return t
