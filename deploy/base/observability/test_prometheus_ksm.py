"""KAN-64 offline checks: Prometheus scrape bounds, KSM series allowlist, rule files and rule unit tests.

Run: python3 -m unittest discover -s deploy/base/observability -p 'test_prometheus_ksm.py' -v
Renders deploy/base/observability with `kubectl kustomize` and validates the *rendered* prometheus-config
with promtool from the pinned prom/prometheus image (docker). No cluster access.
"""

import pathlib
import re
import subprocess
import tempfile
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parent
PROM_IMAGE = "prom/prometheus:v3.15.0"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount/"
# Series the plan / LiveProbe evidence / Grafana / alert rules depend on; they must survive the KSM allowlist.
REQUIRED_KSM = [
    "kube_pod_status_phase", "kube_pod_status_ready", "kube_pod_container_status_restarts_total",
    "kube_pod_container_status_waiting_reason", "kube_pod_container_status_terminated_reason",
    "kube_pod_container_status_last_terminated_reason", "kube_pod_container_resource_limits",
    "kube_deployment_spec_replicas", "kube_deployment_status_replicas_available",
    "kube_deployment_status_replicas_unavailable", "kube_endpoint_info", "kube_endpoint_address",
]
REQUIRED_CADVISOR = ["container_memory_working_set_bytes", "container_spec_memory_limit_bytes",
                     "container_oom_events_total", "container_cpu_usage_seconds_total"]


def render():
    out = subprocess.run(["kubectl", "kustomize", str(ROOT)], capture_output=True, text=True, timeout=60, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def prom_configmap(docs):
    return next(d["data"] for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "prometheus-config")


def promtool(files, *args):
    with tempfile.TemporaryDirectory() as directory:
        root = pathlib.Path(directory)
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        return subprocess.run(["docker", "run", "--rm", "--entrypoint", "/bin/promtool", "-v", f"{root}:/work:ro",
                               "-w", "/work", PROM_IMAGE, *args], capture_output=True, text=True, timeout=120)


class PrometheusKsmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render()
        cls.data = prom_configmap(cls.docs)
        cls.cfg = yaml.safe_load(cls.data["prometheus.yml"])
        cls.jobs = {j["job_name"]: j for j in cls.cfg["scrape_configs"]}

    def test_rendered_config_files(self):
        self.assertEqual(set(self.data), {"prometheus.yml", "alerts.yml", "recording.yml"})
        dep = next(d for d in self.docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "prometheus")
        mounts = {m["mountPath"]: m.get("subPath") for m in dep["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]}
        self.assertEqual(mounts["/etc/prometheus/rules/alerts.yml"], "alerts.yml")
        self.assertEqual(mounts["/etc/prometheus/rules/recording.yml"], "recording.yml")
        self.assertEqual(self.cfg["rule_files"], ["/etc/prometheus/rules/*.yml"])

    def test_every_job_is_bounded(self):
        g = self.cfg["global"]
        self.assertEqual((g["scrape_interval"], g["scrape_timeout"]), ("15s", "10s"))
        self.assertLessEqual(g["label_limit"], 30)
        self.assertLessEqual(g["label_value_length_limit"], 512)
        self.assertLessEqual(g["label_name_length_limit"], 128)
        self.assertEqual(set(self.jobs), {"prometheus", "kube-state-metrics", "kubelet-cadvisor", "kubernetes-pods"})
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                self.assertTrue(0 < job.get("sample_limit", 0) <= 10000)
                interval = int(job.get("scrape_interval", g["scrape_interval"]).rstrip("s"))
                timeout = int(job.get("scrape_timeout", g["scrape_timeout"]).rstrip("s"))
                self.assertLess(timeout, interval)
                self.assertGreaterEqual(interval, 15)
        # total budget of samples per scrape round stays well below a 2Gi / 1500MB TSDB
        per_round = sum(j["sample_limit"] for n, j in self.jobs.items() if n != "kubernetes-pods") + 2 * self.jobs["kubernetes-pods"]["sample_limit"]
        self.assertLessEqual(per_round, 30000)

    def test_ksm_allowlist_keeps_required_series_only(self):
        [keep] = [r for r in self.jobs["kube-state-metrics"]["metric_relabel_configs"] if r.get("action") == "keep"]
        self.assertEqual(keep["source_labels"], ["__name__"])
        pattern = re.compile(f"^(?:{keep['regex']})$")  # Prometheus anchors relabel regexes
        for name in REQUIRED_KSM:
            self.assertTrue(pattern.match(name), name)
        for dropped in ("kube_replicaset_created", "kube_pod_tolerations", "kube_pod_ips", "kube_node_status_addresses",
                        "kube_pod_status_qos_class", "kube_secret_info", "kube_configmap_info"):
            self.assertFalse(pattern.match(dropped), dropped)
        ksm = next(d for d in self.docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "kube-state-metrics")
        args = ksm["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertIn("--namespaces=liveprobe-target,liveprobe-system", args)
        resources = next(a for a in args if a.startswith("--resources="))
        self.assertNotIn("secrets", resources)
        self.assertNotIn("configmaps", resources)

    def test_cadvisor_allowlist(self):
        rules = self.jobs["kubelet-cadvisor"]["metric_relabel_configs"]
        keep = next(r for r in rules if r.get("source_labels") == ["__name__"])
        pattern = re.compile(f"^(?:{keep['regex']})$")
        for name in REQUIRED_CADVISOR:
            self.assertTrue(pattern.match(name), name)
        self.assertFalse(pattern.match("container_network_receive_bytes_total"))
        self.assertIn({"regex": "id|name|image", "action": "labeldrop"}, rules)

    def test_pods_job_relabels_to_bounded_labels(self):
        targets = {r["target_label"] for r in self.jobs["kubernetes-pods"]["relabel_configs"] if "target_label" in r}
        self.assertEqual(targets, {"__metrics_path__", "__address__", "namespace", "pod", "app"})

    def test_recording_rules_are_plumbing_only(self):
        groups = yaml.safe_load(self.data["recording.yml"])["groups"]
        rules = [r for g in groups for r in g["rules"]]
        self.assertTrue(rules)
        for rule in rules:
            with self.subTest(rule=rule.get("record")):
                self.assertNotIn("alert", rule)  # KAN-66 owns alerts
                self.assertTrue(rule["record"].startswith("liveprobe:"))
                self.assertIn('namespace="liveprobe-target"', rule["expr"])
                self.assertRegex(rule["expr"], r"(sum|max) by \(")
        # Preserve the KAN-34 alert names while extending to four Families (KAN-66).
        alerts = {r["alert"] for g in yaml.safe_load(self.data["alerts.yml"])["groups"] for r in g["rules"]}
        self.assertEqual(alerts, {"TargetWorkloadImagePullFailing", "TargetServiceNoEndpoints", "OrderHigh5xxRate",
                                  "TargetWorkloadCrashLooping", "TargetWorkloadOOMKilled", "TargetHighLatency",
                                  "TargetConnectionPoolExhausted", "InventoryHigh5xxRate"})

    def test_promtool_check_config_rules_and_unit_tests(self):
        cfg = self.data["prometheus.yml"].replace("/etc/prometheus/rules/*.yml", "/work/rules/*.yml").replace(SA_DIR, "/work/sa/")
        files = {"prometheus.yml": cfg, "sa/token": "offline-placeholder", "sa/ca.crt": "",
                 "rules/alerts.yml": self.data["alerts.yml"], "rules/recording.yml": self.data["recording.yml"],
                 "alerts.yml": self.data["alerts.yml"], "recording.yml": self.data["recording.yml"],
                 "rules_test.yml": (ROOT / "prometheus" / "rules_test.yml").read_text()}
        for args in (("check", "config", "/work/prometheus.yml"),
                     ("check", "rules", "/work/rules/alerts.yml", "/work/rules/recording.yml"),
                     ("test", "rules", "/work/rules_test.yml")):
            with self.subTest(args=args):
                proc = promtool(files, *args)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_rule_unit_tests_cover_every_rule(self):
        tests = (ROOT / "prometheus" / "rules_test.yml").read_text()
        for g in yaml.safe_load(self.data["recording.yml"])["groups"]:
            for r in g["rules"]:
                self.assertIn(r["record"], tests)
        for g in yaml.safe_load(self.data["alerts.yml"])["groups"]:
            for r in g["rules"]:
                self.assertIn(f"alertname: {r['alert']}", tests)


if __name__ == "__main__":
    unittest.main()
