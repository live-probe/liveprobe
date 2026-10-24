"""P15 observability scope: retain monitoring, collect raw all-namespace data, carry Envoy metrics to main Prometheus."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
OBS = ROOT / "deploy/base/observability"


def objects(path):
    return list(yaml.safe_load_all(path.read_text()))


def test_main_prometheus_covers_cluster_and_envoy_with_bounded_retention():
    docs = objects(OBS / "prometheus/prometheus.yaml")
    cm = next(o for o in docs if o["kind"] == "ConfigMap")
    cfg = yaml.safe_load(cm["data"]["prometheus.yml"])
    jobs = {j["job_name"]: j for j in cfg["scrape_configs"]}
    assert "envoy-stats" in jobs
    assert all(0 < j["sample_limit"] <= 30000 for j in jobs.values())
    pod_sd = jobs["kubernetes-pods"]["kubernetes_sd_configs"][0]
    assert not pod_sd.get("namespaces")
    assert not any(r.get("regex") == "order|inventory" for r in jobs["kubernetes-pods"]["relabel_configs"])
    cad = jobs["kubelet-cadvisor"]["metric_relabel_configs"]
    assert not any(r.get("regex") == "liveprobe-target" for r in cad)
    assert not any(r.get("action") == "labeldrop" and "id" in r.get("regex", "").split("|") for r in cad)
    env = jobs["envoy-stats"]
    assert env["metrics_path"] == "/stats/prometheus" and env["label_limit"] >= 40
    keep = next(r["regex"] for r in env["metric_relabel_configs"] if r.get("action") == "keep")
    assert "istio_requests_total" in keep and "request_duration_milliseconds" in keep
    # Istio는 사이드카 Pod의 prometheus.io 주석을 15020 병합 Envoy 엔드포인트로 바꾼다.
    # 그 Envoy 지표는 envoy-stats(label_limit 상향)가 수집하므로 kubernetes-pods에서 중복으로 받지 않는다.
    drop = [r for r in jobs["kubernetes-pods"]["relabel_configs"] if r.get("action") == "drop"]
    assert any(r.get("regex") == "15020" and "__meta_kubernetes_pod_annotation_prometheus_io_port" in r.get("source_labels", []) for r in drop)
    dep = next(o for o in docs if o["kind"] == "Deployment")
    args = dep["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--storage.tsdb.retention.time=2d" in args and "--storage.tsdb.retention.size=1500MB" in args


def test_ksm_all_namespaces_and_no_secret_collection():
    docs = objects(OBS / "kube-state-metrics/kube-state-metrics.yaml")
    dep = next(o for o in docs if o["kind"] == "Deployment")
    args = dep["spec"]["template"]["spec"]["containers"][0]["args"]
    assert not any(a.startswith("--namespaces=") for a in args)
    assert "secrets" not in next(a for a in args if a.startswith("--resources="))
    role = next(o for o in docs if o["kind"] == "ClusterRole")
    assert not any("secrets" in r["resources"] for r in role["rules"])


def test_alloy_all_namespaces_preserves_bounded_labels_no_hostpath():
    docs = objects(OBS / "alloy/alloy.yaml")
    config = next(o for o in docs if o["kind"] == "ConfigMap")["data"]["config.alloy"]
    assert 'names = ["liveprobe-target"]' not in config and 'regex         = "order|inventory"' not in config
    assert 'values = ["namespace", "app", "pod", "container"]' in config
    assert "loki.source.kubernetes" in config
    dep = next(o for o in docs if o["kind"] == "Deployment")
    assert not any("hostPath" in v for v in dep["spec"]["template"]["spec"].get("volumes", []))


def test_prometheus_egress_reaches_annotated_cluster_metrics_ports():
    """주석으로 알린 클러스터 구성요소 지표 포트까지만 egress를 연다: Pod 네트워크(istiod 15014, traefik 9100),
    hostNetwork retina-agent(10093)는 기존 노드 /32 kubelet 정책에 추가(외부 ipBlock 확대 없음)."""
    docs = [o for o in objects(ROOT / "deploy/base/network/network-policies.yaml") if o and o["kind"] == "NetworkPolicy"]
    pol = next(o for o in docs if o["metadata"]["name"] == "prometheus")
    open_ports = {p["port"] for e in pol["spec"]["egress"] if e.get("to") == [{"namespaceSelector": {}}] for p in e["ports"]}
    assert {15014, 9100} <= open_ports
    assert not any("ipBlock" in str(e) for e in pol["spec"]["egress"])
    prom = [o for o in objects(OBS / "prometheus/prometheus.yaml") if o and o["kind"] == "NetworkPolicy"]
    node = next(o for o in prom if o["metadata"]["name"] == "prometheus-kubelet-egress")
    assert all(b["ipBlock"]["cidr"].endswith("/32") for e in node["spec"]["egress"] for b in e["to"])
    assert {10250, 10093} == {p["port"] for e in node["spec"]["egress"] for p in e["ports"]}
