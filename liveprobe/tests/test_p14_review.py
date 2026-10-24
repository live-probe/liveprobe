"""Independent-review regressions: auxiliary observations must not change risk."""
from datetime import datetime, timezone

import pytest

from app import evidence as E
from app.config import AppConfig, Settings
from app.router import decide
from conftest import family1_kube
from test_evidence import fake_http

NS = "liveprobe-target"


def test_healthy_assigned_node_does_not_turn_image_pull_into_high_risk(monkeypatch):
    monkeypatch.setattr(E.httpx, "get", fake_http())
    k = family1_kube(created_bad=datetime.now(timezone.utc).isoformat())
    k.pods[0]["spec"] = {"nodeName": "worker"}
    k.nodes["worker"] = {"metadata": {"name": "worker"}, "status": {"conditions": [
        {"type": "Ready", "status": "True"}, {"type": "MemoryPressure", "status": "False"}]}}
    bundle = E.collect(k, AppConfig(), Settings(), NS, "inventory")
    assert bundle.nodes and any(i.target == f"{NS}/Node/worker" for i in bundle.items)
    decision = decide(bundle, 15)
    assert decision.features["protected_resource"] is False
    assert decision.level == "SIMPLE"


@pytest.mark.parametrize("kind", ["Node", "Secret", "Namespace"])
def test_actual_protected_resource_target_stays_high_risk(kind):
    from test_router_workflows import family1
    bundle = family1()
    bundle.add("incident", f"{NS}/{kind}/protected", {"reason": "resource incident"})
    assert decide(bundle, 15).level == "HIGH_RISK"


@pytest.mark.parametrize("slow", ["node", "configmap"])
@pytest.mark.parametrize("service_exists", [True, False])
def test_slow_optional_read_preserves_completed_core_and_prior_optional_results(monkeypatch, slow, service_exists):
    import copy
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(E.httpx, "get", fake_http())
    monkeypatch.setattr(E, "COLLECT_TIMEOUT_S", 0.06)
    k = family1_kube()
    if not service_exists:
        k.services.clear()
    for pod, name in zip(k.pods, ["a-good", "z-slow"]):
        pod["spec"] = {"nodeName": name}
        k.nodes[name] = {"metadata": {"name": name}, "status": {}}
    k.deployments["inventory"]["spec"]["template"]["spec"]["volumes"] = [
        {"configMap": {"name": name}} for name in ("a-good", "z-slow")]
    k.configmaps = {name: {"metadata": {"name": name}, "data": {"x": "value"}}
                    for name in ("a-good", "z-slow")}
    k.events = [{"involvedObject": {"kind": "Pod", "name": "inventory-bbb-1"},
                 "reason": "BackOff", "message": "image pull failed"}]
    release, entered = threading.Event(), threading.Event()
    futures = []
    class Pool(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            futures.append(future)
            return future
    monkeypatch.setattr(E, "ThreadPoolExecutor", Pool)
    method = "get_node" if slow == "node" else "configmap_meta"
    original = getattr(k, method)
    def blocked(*args):
        if args[-1] == "z-slow":
            entered.set()
            release.wait(1)
        return original(*args)
    setattr(k, method, blocked)
    try:
        started = time.monotonic()
        bundle = E.collect(k, AppConfig(), Settings(), NS, "inventory")
        assert entered.is_set() and time.monotonic() - started < 0.4
        assert bundle.deployment and bundle.pods and bundle.events
        assert bool(bundle.service) == service_exists
        assert not {i.source for i in bundle.items if i.error} & {
            "deployment", "pod_status", "service", "endpoints", "k8s_event"}
        good = [i for i in bundle.items if i.source == slow and not i.error]
        assert len(good) == 1 and good[0].target.endswith("/a-good")
        assert any(i.source == slow and i.error and "TimeoutError" in i.error for i in bundle.items)
        assert list(E.compact_summary(bundle))[:10] == [
            "note", "E1_deployment", "E2_pods", "E3_service", "E4_callers", "E5_recent_events",
            "E6_metrics", "E7_error_logs", "E8_node", "E9_config"]
        snapshot = copy.deepcopy(bundle)
    finally:
        release.set()
        for future in futures:
            future.result(timeout=2)
    assert bundle == snapshot


@pytest.mark.parametrize("method,api_method,is_list", [
    ("get_replicaset", "read_namespaced_replica_set", False),
    ("list_services", "list_namespaced_service", True),
])
def test_network_owner_and_service_requests_have_finite_socket_timeouts(method, api_method, is_list):
    from types import SimpleNamespace
    from app import k8s as K
    calls = []
    def read(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(items=[]) if is_list else {}
    kube = object.__new__(K.Kube)
    kube.core = kube.apps = SimpleNamespace(**{api_method: read})
    kube.client = SimpleNamespace(ApiException=RuntimeError)
    kube.to_dict = lambda value: value
    getattr(kube, method)(*(NS,) if is_list else (NS, "inventory-bbb"))
    assert calls[0].get("_request_timeout") == 8
