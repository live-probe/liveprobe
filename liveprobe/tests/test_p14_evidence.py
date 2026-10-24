"""P14 B1-B3 offline evidence contracts; never contact a cluster."""
from __future__ import annotations

import json
import pytest

from app import evidence as E
from app import k8s as K
from app.config import AppConfig, Settings
from conftest import family1_kube
from test_evidence import fake_http

NS = "liveprobe-target"


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(E.httpx, "get", fake_http(log_line="ERROR unavailable"))
    return Settings(prometheus_url="http://prom.test", loki_url="http://loki.test")


def node(name):
    return {"metadata": {"name": name, "annotations": {"private": "NEVER-ANNOTATION"}},
            "status": {"conditions": [{"type": t, "status": "True", "reason": "Observed",
                                        "message": "NEVER-MESSAGE", "lastTransitionTime": "2026-10-18T01:00:00Z"}
                                       for t in ("Ready", "MemoryPressure", "DiskPressure", "PIDPressure",
                                                 "NetworkUnavailable", "PrivateCondition")],
                       "allocatable": {"cpu": "4", "memory": "8Gi", "pods": "110"},
                       "addresses": [{"address": "NEVER-ADDRESS"}]},
            "spec": {"taints": [{"key": "pressure", "value": "true", "effect": "NoSchedule"}]}}


def test_only_workload_assigned_nodes_become_e8_evidence(offline):
    k = family1_kube()
    k.nodes = {n: node(n) for n in ("target-node", "other-node")}
    k.pods[0]["spec"] = {"nodeName": "target-node"}
    k.pods[1]["spec"] = {"nodeName": "target-node"}
    k.pods[2]["spec"] = {"nodeName": "other-node"}
    b = E.collect(k, AppConfig(), offline, NS, "inventory")
    nodes = E.compact_summary(b)["E8_node"]
    assert [n["name"] for n in nodes] == ["target-node"]
    assert set(nodes[0]["conditions"]) == {"Ready", "MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"}
    assert nodes[0]["allocatable"] == {"cpu": "4", "memory": "8Gi", "pods": "110"}
    assert nodes[0]["taints"] == [{"key": "pressure", "value": "true", "effect": "NoSchedule"}]
    assert not any(s in json.dumps(nodes) for s in ("NEVER-ANNOTATION", "NEVER-MESSAGE", "NEVER-ADDRESS", "PrivateCondition"))
    item = next(i for i in b.items if i.source == "node")
    assert item.target == f"{NS}/Node/target-node" and item.payload == nodes[0]
    assert item.collected_at.tzinfo is not None and item.error is None


def test_e9_reads_only_template_configmaps_and_returns_value_free_approval_evidence(offline):
    from app.actions import data_hash
    k = family1_kube()
    tpl = k.deployments["inventory"]["spec"]["template"]
    tpl["spec"]["containers"][0]["envFrom"] = [{"configMapRef": {"name": "app-config"}},
                                                 {"secretRef": {"name": "NEVER-SECRET"}}]
    tpl["spec"]["initContainers"] = [{"env": [{"valueFrom": {"configMapKeyRef": {"name": "init-config", "key": "x"}}}]}]
    tpl["spec"]["volumes"] = [{"configMap": {"name": "volume-config"}},
                                  {"projected": {"sources": [{"configMap": {"name": "projected-config"}},
                                                              {"secret": {"name": "NEVER-SECRET"}}]}}]
    cm = {"metadata": {"name": "app-config", "creationTimestamp": "2026-10-01T00:00:00Z",
                       "managedFields": [{"time": "2026-10-18T00:00:00Z"}],
                       "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "NEVER-ANNOTATION"}},
          "data": {"application.yaml": "NEVER-PLAINTEXT"}}
    k.configmaps = {name: {**cm, "metadata": {**cm["metadata"], "name": name}}
                    for name in ("app-config", "init-config", "volume-config", "projected-config", "unreferenced")}
    reads = []
    # Exercise the real read wrapper; forbid broad list calls and Secret APIs.
    from types import SimpleNamespace
    api = object.__new__(K.Kube)
    api.to_dict = lambda obj: obj
    def read(name, ns, **kwargs):
        reads.append((ns, name))
        assert kwargs["_request_timeout"] > 0
        return k.configmaps[name]
    api.core = SimpleNamespace(read_namespaced_config_map=read)
    k.configmap_meta = lambda ns, name: api.configmap_meta(ns, name)
    cfg = AppConfig(safe_actions={"configmap_versions": {"app-config": [{"name": "app-config-v1", "sha256": data_hash(cm["data"])}]}})
    b = E.collect(k, cfg, offline, NS, "inventory")
    configs = E.compact_summary(b)["E9_config"]
    assert [c["name"] for c in configs] == ["app-config", "init-config", "projected-config", "volume-config"]
    assert reads == [(NS, c["name"]) for c in configs]
    assert configs[0]["keys"] == ["application.yaml"]
    assert configs[0]["data_sha256"] == data_hash(cm["data"])
    assert configs[0]["last_changed"] == "2026-10-18T00:00:00Z"
    assert configs[0]["approved_version_matches"] is True
    assert configs[1]["approved_version_matches"] is None
    text = json.dumps({"summary": E.compact_summary(b), "items": [i.__dict__ for i in b.items]}, default=str)
    assert not any(s in text for s in ("NEVER-PLAINTEXT", "NEVER-ANNOTATION", "NEVER-SECRET", "unreferenced"))
    items = [i for i in b.items if i.source == "configmap"]
    assert len(items) == 4 and all(i.error is None and i.collected_at.tzinfo for i in items)


def test_e8_e9_are_visible_citable_and_fabricated_suffixes_are_rejected():
    from app.agent_groups import ALL_E, EvidenceStore, check_citations
    from test_agent_groups import Fake
    summary = {"E8_node": [{"name": "worker-a", "conditions": {"MemoryPressure": {"status": "True"}}}],
               "E9_config": [{"name": "app-config", "approved_version_matches": False}]}
    store = EvidenceStore(summary)
    msg = {"evidence": [{"ref": "E8", "quote": "MemoryPressure"}, {"ref": "E9_config", "quote": "app-config"}],
           "root_cause_candidates": [{"evidence_refs": ["E8", "E9_config"]}]}
    rep = check_citations(store, msg)
    assert rep.clean and rep.grounded == [0]
    assert store.key("E9_invented") is None
    assert "E8_node" in store.view(ALL_E) and "E9_config" in store.view(ALL_E)
    # The NORMAL Kubernetes analyst needs the new health/change evidence too.
    from app.agent_groups import GroupController, run_group
    fake = Fake({})
    ctl = GroupController("NORMAL", AppConfig(), fake, lambda *_: None)
    run_group("NORMAL", ctl, store, {"workload": "inventory"}, {})
    ix = next(i for i, c in enumerate(fake.calls) if c[0] == "k8s_analyst")
    assert "E8_node" in fake.prompts[ix] and "E9_config" in fake.prompts[ix]


@pytest.mark.parametrize("source", ["node", "configmap"])
def test_node_config_failures_are_isolated_and_do_not_echo_resource_bodies(offline, source):
    k = family1_kube()
    for p in k.pods[:2]:
        p["spec"] = {"nodeName": "a-broken" if p is k.pods[0] else "z-good"}
    k.nodes["z-good"] = node("z-good")
    k.deployments["inventory"]["spec"]["template"]["spec"]["volumes"] = [
        {"configMap": {"name": name}} for name in ("a-broken", "z-good")]
    k.configmaps["z-good"] = {"metadata": {"name": "z-good"}, "data": {"x": "NEVER-VALUE"}}
    method = "get_node" if source == "node" else "configmap_meta"
    original = getattr(k, method)
    def failing(*args):
        if args[-1] == "a-broken":
            raise RuntimeError("NEVER-RESOURCE-BODY")
        return original(*args)
    setattr(k, method, failing)
    b = E.collect(k, AppConfig(), offline, NS, "inventory")
    err = next(i for i in b.items if i.source == source and i.error)
    assert "a-broken" in err.target and err.collected_at.tzinfo is not None
    assert "RuntimeError" in err.error and "NEVER-RESOURCE-BODY" not in err.error
    summary = E.compact_summary(b)
    assert any(s.startswith(source + ":") for s in summary["collection_errors"])
    assert summary["E8_node"] and summary["E9_config"]
    assert b.deployment and b.metrics and b.logs


def test_empty_node_and_config_refs_do_not_trigger_reads_or_errors(offline):
    k = family1_kube()
    def unexpected(*_):
        raise AssertionError("must not read unrelated resources")
    k.get_node = k.configmap_meta = unexpected
    b = E.collect(k, AppConfig(), offline, NS, "inventory")
    assert E.compact_summary(b)["E8_node"] == []
    assert E.compact_summary(b)["E9_config"] == []
    assert not any(i.error for i in b.items)


def test_binary_configmap_changes_are_hashed_and_cannot_claim_data_only_approval(offline):
    from app.actions import data_hash
    k = family1_kube()
    k.deployments["inventory"]["spec"]["template"]["spec"]["volumes"] = [{"configMap": {"name": "cm"}}]
    k.configmaps["cm"] = {"metadata": {"name": "cm"}, "data": {"x": "same"}, "binaryData": {"cert": "QQ=="}}
    cfg = AppConfig(safe_actions={"configmap_versions": {"cm": [{"name": "v1", "sha256": data_hash({"x": "same"})}]}})
    a = E.compact_summary(E.collect(k, cfg, offline, NS, "inventory"))["E9_config"][0]
    k.configmaps["cm"]["binaryData"]["cert"] = "Qg=="
    b = E.compact_summary(E.collect(k, cfg, offline, NS, "inventory"))["E9_config"][0]
    assert a["content_sha256"] != b["content_sha256"]
    assert a["approved_version_matches"] is False
    assert "QQ==" not in json.dumps(a) and "Qg==" not in json.dumps(b)


def test_configmap_rbac_is_target_namespaced_get_list_only_and_no_secrets():
    from pathlib import Path
    import yaml
    docs = list(yaml.safe_load_all((Path(__file__).resolve().parents[2] / "deploy/base/liveprobe/liveprobe.yaml").read_text()))
    cm_roles = [d for d in docs if d and d.get("kind") in ("Role", "ClusterRole")
                and any("configmaps" in r.get("resources", []) or "*" in r.get("resources", []) for r in d.get("rules", []))]
    assert len(cm_roles) == 1
    role = cm_roles[0]
    assert role["kind"] == "Role" and role["metadata"]["namespace"] == NS
    assert role["rules"] == [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "list"]}]
    binding = next(d for d in docs if d and d.get("kind") == "RoleBinding" and d["roleRef"]["name"] == role["metadata"]["name"])
    assert binding["metadata"]["namespace"] == NS
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "liveprobe", "namespace": "liveprobe-system"}]
    assert all(not {"secrets", "*"} & set(r.get("resources", [])) for d in docs if d
               and d.get("kind") in ("Role", "ClusterRole") for r in d.get("rules", []))


def test_event_duplicates_sum_counts_before_limit_and_keep_latest_time(offline):
    k = family1_kube()
    def event(message, count, last):
        return {"involvedObject": {"kind": "Pod", "name": "inventory-bbb-1"}, "reason": "BackOff",
                "message": message, "count": count, "lastTimestamp": last}
    long_message = "pull failed " * 25
    k.events = [event(long_message, 3, "2026-10-18T01:00:00Z"),
                event(long_message, 2, "2026-10-18T01:00:01Z"),
                event(long_message, None, "2026-10-18T01:00:02Z"),
                event("other", 4, "2026-10-18T00:00:00Z")]
    b = E.collect(k, AppConfig(), offline, NS, "inventory")
    assert len(b.events) == 2
    assert b.events[0]["count"] == 6 and b.events[0]["last"] == "2026-10-18T01:00:02Z"
    assert b.events[1]["message"] == "other" and b.events[1]["count"] == 4
    before = json.dumps([{"last": e["lastTimestamp"], "type": e.get("type"), "reason": e["reason"],
                          "object": "Pod/inventory-bbb-1", "message": e["message"][:E.EVENT_MESSAGE_CHARS],
                          "count": e["count"] or 1} for e in k.events])
    after = json.dumps(b.events)
    print(f"event prompt chars {len(before)} -> {len(after)}; estimated tokens (~4 chars/token) {(len(before)+3)//4} -> {(len(after)+3)//4}")
    assert len(after) < len(before) / 2


def test_log_duplicates_ignore_timestamps_keep_app_and_stable_latest_order(monkeypatch, offline):
    from test_evidence import _Resp
    rows = [["300", "2026-10-18T10:00:02Z ERROR connection refused"],
            ["200", "2026-10-18T10:00:01Z ERROR different"],
            ["100", "2026-10-18T10:00:00Z ERROR connection refused"]]
    streams = [{"stream": {"app": "inventory"}, "values": rows},
               {"stream": {"app": "order"}, "values": [["250", "ERROR connection refused"]]}]
    monkeypatch.setattr(E.httpx, "get", lambda *a, **k: _Resp({"data": {"result": streams}}))
    start, end = E.loki_window(None)
    lines = E._loki(offline, NS, ["inventory", "order"], start, end)
    assert lines == ["[inventory] ERROR connection refused (repeated 2 times)",
                     "[order] ERROR connection refused", "[inventory] ERROR different"]
    streams.reverse()
    rows.reverse()
    assert E._loki(offline, NS, ["inventory", "order"], start, end) == lines
    streams[:] = [{"stream": {"app": "inventory"}, "values": [rows[0]] * 50}]
    compressed = E._loki(offline, NS, ["inventory"], start, end)
    assert len(compressed) == 1 and "repeated 50 times" in compressed[0]
    before = json.dumps(["[inventory] " + rows[0][1]] * 50)
    after = json.dumps(compressed)
    print(f"log prompt chars {len(before)} -> {len(after)}; estimated tokens (~4 chars/token) {(len(before)+3)//4} -> {(len(after)+3)//4}")
    assert len(after) < len(before) / 10


def test_independent_kube_prom_loki_queries_overlap_with_private_results(monkeypatch, offline):
    import threading
    k = family1_kube()
    barrier = threading.Barrier(3, timeout=1)
    workers, bundles = {}, {}
    lock = threading.Lock()
    def meet(source):
        with lock:
            workers[source] = threading.get_ident()
        barrier.wait()
    pods = k.list_pods
    def list_pods(ns):
        meet("kube")
        return pods(ns)
    k.list_pods = list_pods
    original_prom = E._prom
    first_prom = True
    def prom(*args):
        nonlocal first_prom
        if first_prom:
            first_prom = False
            meet("prom")
        return original_prom(*args)
    monkeypatch.setattr(E, "_prom", prom)
    original_loki = E._loki
    def loki(*args, **kwargs):
        meet("loki")
        return original_loki(*args, **kwargs)
    monkeypatch.setattr(E, "_loki", loki)
    original_add = E.Bundle.add
    def add(self, *args, **kwargs):
        with lock:
            bundles.setdefault(threading.get_ident(), set()).add(id(self))
        return original_add(self, *args, **kwargs)
    monkeypatch.setattr(E.Bundle, "add", add)
    b = E.collect(k, AppConfig(), offline, NS, "inventory")
    assert b.pods and b.metrics and b.logs
    assert not any(i.error for i in b.items)
    assert len(set(workers.values())) == 3
    result_ids = [bundles[tid] for tid in workers.values()]
    assert all(a.isdisjoint(c) for i, a in enumerate(result_ids) for c in result_ids[i+1:])
    assert [i.source for i in b.items] == ["deployment", "pod_status", "service", "endpoints", "k8s_event", "prometheus", "loki"]
    assert list(E.compact_summary(b))[:10] == ["note", "E1_deployment", "E2_pods", "E3_service", "E4_callers",
                                             "E5_recent_events", "E6_metrics", "E7_error_logs", "E8_node", "E9_config"]


@pytest.mark.parametrize("slow", ["kube", "prometheus", "loki"])
def test_slow_source_times_out_without_waiting_or_mutating_returned_bundle(monkeypatch, offline, slow):
    import threading
    import time
    import copy
    k = family1_kube()
    release, finished = threading.Event(), threading.Event()
    monkeypatch.setattr(E, "COLLECT_TIMEOUT_S", 0.05, raising=False)
    if slow == "kube":
        original = k.list_pods
        def slow_read(*args):
            release.wait(0.6)
            finished.set()
            return original(*args)
        k.list_pods = slow_read
    else:
        attr = "_prom" if slow == "prometheus" else "_loki"
        original = getattr(E, attr)
        def slow_read(*args, **kwargs):
            release.wait(0.6)
            finished.set()
            return original(*args, **kwargs)
        monkeypatch.setattr(E, attr, slow_read)
    t0 = time.monotonic()
    try:
        b = E.collect(k, AppConfig(), offline, NS, "inventory")
        elapsed = time.monotonic() - t0
        assert elapsed < 0.3
        bad = {i.source for i in b.items if i.error}
        assert (slow in bad) if slow != "kube" else {"deployment", "pod_status", "k8s_event"} <= bad
        assert all("TimeoutError" in i.error for i in b.items if i.error)
        assert b.logs if slow != "loki" else b.logs == []
        assert b.metrics if slow != "prometheus" else b.metrics == {}
        assert b.pods if slow != "kube" else b.pods == []
        snapshot = copy.deepcopy(b)
    finally:
        release.set()
    assert finished.wait(1)
    time.sleep(0.03)
    assert b == snapshot  # Late workers can mutate only their private result.


@pytest.mark.parametrize("slow", ["kube", "prometheus"])
def test_timed_out_workers_stop_starting_followup_requests(monkeypatch, offline, slow):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    release = threading.Event()
    futures, calls, bounds = [], [], []
    class Pool(ThreadPoolExecutor):
        def __init__(self, max_workers, **kwargs):
            bounds.append(max_workers)
            super().__init__(max_workers=max_workers, **kwargs)
        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            futures.append(future)
            return future
    monkeypatch.setattr(E, "ThreadPoolExecutor", Pool)
    monkeypatch.setattr(E, "COLLECT_TIMEOUT_S", 0.03)
    k = family1_kube()
    if slow == "kube":
        original = k.list_pods
        def blocked(ns):
            calls.append("pods")
            release.wait(1)
            return original(ns)
        k.list_pods = blocked
        get = k.get_deployment
        def get_deployment(*args):
            calls.append("deployment")
            return get(*args)
        k.get_deployment = get_deployment
    else:
        original = E._prom
        def blocked(*args):
            calls.append("query")
            release.wait(1)
            return original(*args)
        monkeypatch.setattr(E, "_prom", blocked)
    try:
        E.collect(k, AppConfig(), offline, NS, "inventory")
    finally:
        release.set()
    for f in futures:
        f.result(timeout=1)
    assert bounds == [3]
    assert len(calls) == 1


@pytest.mark.parametrize("method,api_method,args,is_list", [
    ("list_pods", "list_namespaced_pod", (NS,), True),
    ("get_deployment", "read_namespaced_deployment", (NS, "inventory"), False),
    ("list_replicasets", "list_namespaced_replica_set", (NS,), True),
    ("get_service", "read_namespaced_service", (NS, "inventory"), False),
    ("get_endpoints", "read_namespaced_endpoints", (NS, "inventory"), False),
    ("list_events", "list_namespaced_event", (NS,), True),
    ("get_node", "read_node", ("worker",), False),
    ("configmap_meta", "read_namespaced_config_map", (NS, "app-config"), False),
])
def test_evidence_kubernetes_requests_have_socket_timeouts(method, api_method, args, is_list):
    from types import SimpleNamespace
    calls = []
    def read(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(items=[]) if is_list else {}
    api = object.__new__(K.Kube)
    api.to_dict = lambda obj: obj
    api.core = api.apps = SimpleNamespace(**{api_method: read})
    api.client = SimpleNamespace(ApiException=RuntimeError)
    getattr(api, method)(*args)
    assert len(calls) == 1 and calls[0].get("_request_timeout") == 8


def test_parallel_collection_reduces_measured_offline_latency(monkeypatch, offline):
    import time
    k = family1_kube()
    original_pods, original_prom, original_loki = k.list_pods, E._prom, E._loki
    def pods(ns):
        time.sleep(0.06)
        return original_pods(ns)
    k.list_pods = pods
    def prom(settings, q):
        if q == E.PROM_QUERIES["http_5xx"] % {"ns": NS}:
            time.sleep(0.06)
        return original_prom(settings, q)
    monkeypatch.setattr(E, "_prom", prom)
    def loki(*args, **kwargs):
        time.sleep(0.06)
        return original_loki(*args, **kwargs)
    monkeypatch.setattr(E, "_loki", loki)
    start = time.monotonic()
    deadline = start + E.COLLECT_TIMEOUT_S
    E._collect_kube(k, AppConfig(), NS, "inventory", deadline)
    E._collect_prom(offline, NS, "inventory", ["inventory", "order"], deadline)
    E._collect_loki(offline, NS, "inventory", ["inventory", "order"], None)
    serial = time.monotonic() - start
    start = time.monotonic()
    b = E.collect(k, AppConfig(), offline, NS, "inventory")
    parallel = time.monotonic() - start
    assert b.pods and b.metrics and b.logs
    print(f"offline evidence latency serial={serial:.4f}s parallel={parallel:.4f}s ratio={parallel / serial:.3f}")
    assert parallel < serial * 0.75
