"""Offline checks for the bounded observability configuration."""

import json
import pathlib
import subprocess
import tempfile
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parent


def objects(path):
    return list(yaml.safe_load_all((ROOT / path).read_text()))


def configmap(path, name):
    return next(obj["data"] for obj in objects(path) if obj["kind"] == "ConfigMap" and obj["metadata"]["name"] == name)


class ObservabilityConfigTest(unittest.TestCase):
    def test_prometheus_limits_and_target_scope(self):
        docs = objects("prometheus/prometheus.yaml")
        data = configmap("prometheus/prometheus.yaml", "prometheus-config")
        cfg = yaml.safe_load(data["prometheus.yml"])
        jobs = {job["job_name"]: job for job in cfg["scrape_configs"]}
        self.assertEqual(jobs["kubernetes-pods"]["kubernetes_sd_configs"][0]["namespaces"]["names"], ["liveprobe-target"])
        self.assertIn({"source_labels": ["__meta_kubernetes_pod_label_app"], "action": "keep", "regex": "order|inventory"}, jobs["kubernetes-pods"]["relabel_configs"])
        self.assertEqual(jobs["kubernetes-pods"]["sample_limit"], 5000)
        self.assertEqual(jobs["kube-state-metrics"]["sample_limit"], 10000)
        args = next(doc["spec"]["template"]["spec"]["containers"][0]["args"] for doc in docs if doc["kind"] == "Deployment")
        self.assertIn("--storage.tsdb.retention.size=1500MB", args)
        self.assertIn("--storage.tsdb.retention.time=2d", args)
        self.assertEqual(yaml.safe_load(data["alerts.yml"])["groups"][0]["name"], "liveprobe-target")

    def test_oom_alert_requires_kubernetes_oomkilled_reason_without_pending_window(self):
        data = configmap("prometheus/prometheus.yaml", "prometheus-config")
        alerts = yaml.safe_load(data["alerts.yml"])["groups"][0]["rules"]
        rule = next(rule for rule in alerts if rule["alert"] == "TargetWorkloadOOMKilled")
        self.assertIn('reason="OOMKilled"', rule["expr"])
        self.assertNotIn('reason="Error"', rule["expr"])
        self.assertEqual(rule["for"], "0s")

    def test_grafana_provisioning_is_declarative(self):
        data = configmap("grafana/grafana.yaml", "grafana-dashboards")
        dashboard = json.loads(data["liveprobe.json"])
        provider = yaml.safe_load(data["dashboards.yaml"])["providers"][0]
        deployment = next(doc for doc in objects("grafana/grafana.yaml") if doc["kind"] == "Deployment")
        volumes = {v["name"]: v for v in deployment["spec"]["template"]["spec"]["volumes"]}
        mounts = {m["name"]: m["mountPath"] for m in deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]}
        self.assertEqual(dashboard["uid"], "liveprobe-overview")
        self.assertEqual({p["datasource"]["uid"] for p in dashboard["panels"]}, {"prometheus", "loki"})
        self.assertEqual(provider["options"]["path"], mounts["dashboard-files"])
        self.assertEqual(volumes["dashboard-files"]["configMap"]["name"], "grafana-dashboards")
        self.assertEqual(volumes["dashboard-provider"]["configMap"]["name"], "grafana-dashboards")
        self.assertEqual({source["uid"] for source in yaml.safe_load(configmap("grafana/grafana.yaml", "grafana-datasources")["datasources.yaml"])["datasources"]}, {"prometheus", "loki"})

    def test_alloy_source_is_target_only(self):
        alloy = configmap("alloy/alloy.yaml", "alloy-config")["config.alloy"]
        self.assertIn('names = ["liveprobe-target"]', alloy)
        self.assertIn('regex         = "order|inventory"', alloy)
        self.assertNotIn('"liveprobe-system"', alloy)
        self.assertIn('target_label  = "pod"', alloy)
        self.assertIn('target_label  = "container"', alloy)

    def test_alloy_format(self):
        alloy = configmap("alloy/alloy.yaml", "alloy-config")["config.alloy"]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.alloy"
            path.write_text(alloy)
            proc = subprocess.run(["docker", "run", "--rm", "--entrypoint", "/bin/alloy", "-v", f"{directory}:/work:ro", "grafana/alloy:v1.19.2", "fmt", "/work/config.alloy"], capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_overlay_render_schema(self):
        for overlay in ("rancher-desktop", "portable-example"):
            with self.subTest(overlay=overlay):
                rendered = subprocess.run(["kubectl", "kustomize", str(ROOT.parents[1] / "overlays" / overlay)], capture_output=True, text=True, timeout=60, check=True).stdout
                validation = subprocess.run(["docker", "run", "--rm", "-i", "ghcr.io/yannh/kubeconform:v0.7.0", "-strict", "-summary", "-kubernetes-version", "1.33.0", "-"], input=rendered, capture_output=True, text=True, timeout=90)
                self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)

    def test_kubelet_cadvisor_scope_and_least_privilege(self):
        docs = objects("prometheus/prometheus.yaml")
        cfg = yaml.safe_load(configmap("prometheus/prometheus.yaml", "prometheus-config")["prometheus.yml"])
        jobs = [job for job in cfg["scrape_configs"] if job["job_name"] == "kubelet-cadvisor"]
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual((job["scheme"], job["metrics_path"]), ("https", "/metrics/cadvisor"))
        self.assertNotIn("insecure_skip_verify", json.dumps(job["tls_config"]).replace('"insecure_skip_verify": false', ""))
        self.assertLessEqual(job["sample_limit"], 5000)
        self.assertIn({"source_labels": ["namespace"], "action": "keep", "regex": "liveprobe-target"}, job["metric_relabel_configs"])
        role = next(doc for doc in docs if doc["kind"] == "ClusterRole")
        resources = {r for rule in role["rules"] for r in rule["resources"]}
        verbs = {v for rule in role["rules"] for v in rule["verbs"]}
        self.assertIn("nodes/metrics", resources)
        self.assertNotIn("nodes/proxy", resources)
        self.assertNotIn("*", resources | verbs)
        self.assertEqual({"get", "list", "watch"}, verbs)
        [kubelet_rule] = [rule for rule in role["rules"] if "nodes/metrics" in rule["resources"]]
        self.assertEqual((kubelet_rule["resources"], kubelet_rule["verbs"]), (["nodes/metrics"], ["get"]))
        policy = next(doc for doc in docs if doc["kind"] == "NetworkPolicy")
        self.assertEqual(policy["spec"]["podSelector"], {"matchLabels": {"app": "prometheus"}})
        self.assertEqual(policy["spec"]["egress"], [{"to": [{"ipBlock": {"cidr": "0.0.0.0/32"}}], "ports": [{"protocol": "TCP", "port": 10250}]}])

    def test_scoped_overlay_contains_only_observability(self):
        base = yaml.safe_load((ROOT.parent / "kustomization.yaml").read_text())["resources"]
        local = yaml.safe_load((ROOT / "kustomization.yaml").read_text())["resources"]
        self.assertEqual([r for r in base if r.startswith("observability/")], ["observability/" + r for r in local])
        rendered = subprocess.run(["kubectl", "kustomize", str(ROOT.parents[1] / "overlays" / "rancher-desktop-observability")], capture_output=True, text=True, timeout=60, check=True).stdout
        docs = [doc for doc in yaml.safe_load_all(rendered) if doc]
        cluster_scoped = {(d["kind"], d["metadata"]["name"]) for d in docs if "namespace" not in d["metadata"]}
        self.assertEqual({d["metadata"].get("namespace") for d in docs} - {None}, {"liveprobe-observability"})
        self.assertEqual(cluster_scoped, {(k, f"liveprobe-{n}") for k in ("ClusterRole", "ClusterRoleBinding") for n in ("prometheus", "alloy", "kube-state-metrics")})
        names = {d["metadata"]["name"] for d in docs}
        self.assertFalse(any(name.startswith("rollback-") or name.startswith("write-guard") for name in names))
        policy = next(d for d in docs if d["kind"] == "NetworkPolicy")
        self.assertEqual(policy["spec"]["egress"][0]["to"][0]["ipBlock"]["cidr"], "192.168.5.15/32")

    def test_promtool(self):
        data = configmap("prometheus/prometheus.yaml", "prometheus-config")
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            sa = root / "sa"
            sa.mkdir()
            (sa / "token").write_text("offline-placeholder")
            (sa / "ca.crt").write_text("")
            (root / "prometheus.yml").write_text(data["prometheus.yml"].replace("/etc/prometheus/rules/*.yml", "/work/alerts.yml").replace("/var/run/secrets/kubernetes.io/serviceaccount/", "/work/sa/"))
            (root / "alerts.yml").write_text(data["alerts.yml"])
            for args in [("check", "config", "/work/prometheus.yml"), ("check", "rules", "/work/alerts.yml")]:
                proc = subprocess.run(["docker", "run", "--rm", "--entrypoint", "/bin/promtool", "-v", f"{root}:/work:ro", "prom/prometheus:v3.15.0", *args], capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
