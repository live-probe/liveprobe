"""P15 overview inventory reads only bounded, non-secret Kubernetes resource summaries."""
from types import SimpleNamespace as N


def test_inventory_keeps_zero_pod_deployments_and_namespace_names_without_templates():
    from app.k8s import Kube
    k = Kube.__new__(Kube)
    calls = []
    def read(name, rows):
        def f(**kw):
            calls.append((name, kw))
            return N(items=rows)
        return f
    k.to_dict = lambda x: x
    k.core = N(list_node=read("nodes", []), list_pod_for_all_namespaces=read("pods", []),
               list_service_for_all_namespaces=read("services", []), list_endpoints_for_all_namespaces=read("endpoints", []),
               list_namespace=read("namespaces", [{"metadata": {"name": "empty", "annotations": {"omit": "x"}}}]))
    k.apps = N(list_deployment_for_all_namespaces=read("deployments", [{"metadata": {"namespace": "empty", "name": "asleep"},
               "spec": {"replicas": 0, "template": {"spec": {"containers": [{"env": [{"value": "not-for-overview"}]}]}}},
               "status": {"readyReplicas": 0}}]))
    k.client = N(NetworkingV1Api=lambda: N(list_ingress_for_all_namespaces=read("ingresses", [])))
    out = k.cluster_inventory()
    assert out["namespaces"] == [{"metadata": {"name": "empty"}}]
    assert out["deployments"][0]["spec"] == {"replicas": 0}
    assert "not-for-overview" not in str(out)
    assert all(c[1].get("_request_timeout") == 8 for c in calls)


def test_warning_events_read_is_bounded_and_excludes_normal_events_at_source():
    from app.k8s import Kube
    k = Kube.__new__(Kube)
    k.to_dict = lambda x: x
    calls = []
    def read(**kw):
        calls.append(kw)
        return N(items=[{"reason": "Failed", "message": "original raw text"}])
    k.core = N(list_event_for_all_namespaces=read)
    assert k.cluster_warning_events()[0]["message"] == "original raw text"
    assert calls == [{"field_selector": "type=Warning", "limit": 500, "_request_timeout": 8}]
