"""KAN-65 offline checks: Alloy log sources, Loki bounds, Grafana datasource/dashboard provisioning.

Run: python3 -m unittest discover -s deploy/base/observability -p 'test_logs_grafana.py' -v
Validators run the pinned upstream images (same tags as the manifests) with docker; no cluster access.
"""

import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parent
ALLOY_IMAGE = "grafana/alloy:v1.19.2"
LOKI_IMAGE = "grafana/loki:3.7.8"
BOUNDED_LABELS = {"namespace", "app", "pod", "container"}
FORBIDDEN_LABEL_SOURCES = ("__meta_kubernetes_pod_uid", "__meta_kubernetes_pod_container_id",
                           "__meta_kubernetes_pod_container_image", "__meta_kubernetes_pod_node_name",
                           "__meta_kubernetes_pod_ip")


def objects(path):
    return [o for o in yaml.safe_load_all((ROOT / path).read_text()) if o]


def configmap(path, name):
    return next(o["data"] for o in objects(path) if o["kind"] == "ConfigMap" and o["metadata"]["name"] == name)


def workload(path, name):
    return next(o for o in objects(path) if o["kind"] in ("Deployment", "DaemonSet") and o["metadata"]["name"] == name)


def run_image(image, entrypoint, files, args):
    """Run an offline validator image with `files` ({name: text}) mounted read-only at /work."""
    if shutil.which("docker") is None:
        raise unittest.SkipTest("docker not available")
    with tempfile.TemporaryDirectory() as directory:
        for name, text in files.items():
            (pathlib.Path(directory) / name).write_text(text)
        cmd = ["docker", "run", "--rm", "--network", "none"]
        if entrypoint:
            cmd += ["--entrypoint", entrypoint]
        cmd += ["-v", f"{directory}:/work:ro", image, *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def alloy_config():
    return configmap("alloy/alloy.yaml", "alloy-config")["config.alloy"]


def dashboard():
    return json.loads(configmap("grafana/grafana.yaml", "grafana-dashboards")["liveprobe.json"])


def provisioned_datasources():
    data = yaml.safe_load(configmap("grafana/grafana.yaml", "grafana-datasources")["datasources.yaml"])
    return {d["uid"]: d for d in data["datasources"]}


class AlloyLogSourceTest(unittest.TestCase):
    def test_explicit_namespace_and_label_selectors(self):
        cfg = alloy_config()
        self.assertEqual(re.findall(r"names\s*=\s*\[([^\]]*)\]", cfg), ['"liveprobe-target"'])
        self.assertRegex(cfg, r'source_labels = \["__meta_kubernetes_pod_label_app"\]\s*\n\s*action\s*= "keep"\s*\n\s*regex\s*= "order\|inventory"')
        for other in ("liveprobe-system", "liveprobe-observability\"]", "kube-system", "loadgen"):
            self.assertNotIn(other, cfg.split("loki.write")[0])

    def test_bounded_labels_only(self):
        cfg = alloy_config()
        targets = set(re.findall(r'target_label\s*=\s*"([^"]+)"', cfg))
        self.assertEqual(targets, BOUNDED_LABELS)
        for source in FORBIDDEN_LABEL_SOURCES:
            self.assertNotIn(source, cfg)
        keep = re.search(r'stage\.label_keep\s*\{\s*values\s*=\s*\[([^\]]*)\]', cfg)
        self.assertIsNotNone(keep, "loki.process must drop every label outside the bounded set")
        assert keep is not None
        self.assertEqual(set(re.findall(r'"([^"]+)"', keep.group(1))), BOUNDED_LABELS)

    def test_pipeline_ships_to_in_cluster_loki(self):
        cfg = alloy_config()
        self.assertIn("forward_to = [loki.process.bounded.receiver]", cfg)
        self.assertIn("forward_to = [loki.write.default.receiver]", cfg)
        self.assertEqual(re.findall(r'url = "([^"]+)"', cfg), ["http://loki.liveprobe-observability.svc:3100/loki/api/v1/push"])
        self.assertNotRegex(cfg, r"(?i)(password|bearer|basic_auth|api_key|token)")

    def test_alloy_rbac_is_read_only_pod_logs(self):
        role = next(o for o in objects("alloy/alloy.yaml") if o["kind"] == "ClusterRole")
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["pods", "pods/log"], "verbs": ["get", "list", "watch"]}])
        spec = workload("alloy/alloy.yaml", "alloy")["spec"]["template"]["spec"]
        self.assertFalse([v for v in spec["volumes"] if "hostPath" in v], "Alloy must not mount hostPath")
        self.assertEqual(spec["containers"][0]["image"], ALLOY_IMAGE)

    def test_alloy_validate_offline(self):
        cfg = alloy_config()
        fmt = run_image(ALLOY_IMAGE, "/bin/alloy", {"config.alloy": cfg}, ["fmt", "/work/config.alloy"])
        self.assertEqual(fmt.returncode, 0, fmt.stdout + fmt.stderr)
        # Canonical fmt output uses tabs; the embedded YAML block keeps spaces, so only syntax is asserted.
        val = run_image(ALLOY_IMAGE, "/bin/alloy", {"config.alloy": cfg},
                        ["validate", "--stability.level=public-preview", "/work/config.alloy"])
        self.assertEqual(val.returncode, 0, val.stdout + val.stderr)

    def test_alloy_validate_rejects_broken_config(self):
        broken = alloy_config().replace("loki.process.bounded.receiver", "loki.process.missing.receiver")
        val = run_image(ALLOY_IMAGE, "/bin/alloy", {"config.alloy": broken},
                        ["validate", "--stability.level=public-preview", "/work/config.alloy"])
        self.assertNotEqual(val.returncode, 0, "negative control: validator must reject a dangling reference")


class LokiConfigTest(unittest.TestCase):
    def cfg(self):
        return yaml.safe_load(configmap("loki/loki.yaml", "loki-config")["loki.yaml"])

    def test_retention_and_limits_bounded(self):
        cfg = self.cfg()
        limits = cfg["limits_config"]
        self.assertEqual(limits["retention_period"], "48h")
        self.assertTrue(cfg["compactor"]["retention_enabled"])
        self.assertEqual(cfg["compactor"]["delete_request_store"], "filesystem")
        self.assertLessEqual(limits["ingestion_rate_mb"], 4)
        self.assertLessEqual(limits["max_global_streams_per_user"], 1000)
        self.assertLessEqual(limits["max_label_names_per_series"], 15)
        self.assertEqual(limits["max_query_lookback"], "48h")
        self.assertEqual(limits["discover_service_name"], [])
        self.assertFalse(cfg["analytics"]["reporting_enabled"])
        self.assertFalse(cfg["auth_enabled"])  # single-tenant, in-cluster only (ingress is Grafana only)

    def test_single_binary_on_pvc(self):
        docs = objects("loki/loki.yaml")
        pvc = next(o for o in docs if o["kind"] == "PersistentVolumeClaim")
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "2Gi")
        dep = workload("loki/loki.yaml", "loki")
        self.assertEqual(dep["spec"]["replicas"], 1)
        self.assertIn("-target=all", dep["spec"]["template"]["spec"]["containers"][0]["args"])
        vols = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
        self.assertEqual(vols["data"]["persistentVolumeClaim"]["claimName"], pvc["metadata"]["name"])

    def test_loki_verify_config_offline(self):
        text = configmap("loki/loki.yaml", "loki-config")["loki.yaml"]
        proc = run_image(LOKI_IMAGE, None, {"loki.yaml": text}, ["-config.file=/work/loki.yaml", "-verify-config"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_loki_verify_rejects_broken_config(self):
        text = configmap("loki/loki.yaml", "loki-config")["loki.yaml"].replace("retention_period: 48h", "retention_period: not-a-duration")
        proc = run_image(LOKI_IMAGE, None, {"loki.yaml": text}, ["-config.file=/work/loki.yaml", "-verify-config"])
        self.assertNotEqual(proc.returncode, 0, "negative control: -verify-config must reject a bad duration")


class GrafanaProvisioningTest(unittest.TestCase):
    def test_datasources_provisioned_with_fixed_uids(self):
        sources = provisioned_datasources()
        self.assertEqual(set(sources), {"prometheus", "loki"})
        self.assertEqual(sources["prometheus"]["type"], "prometheus")
        self.assertEqual(sources["loki"]["type"], "loki")
        self.assertEqual(sources["loki"]["url"], "http://loki.liveprobe-observability.svc:3100")
        for source in sources.values():
            self.assertNotIn("secureJsonData", source)
            self.assertNotIn("basicAuthPassword", source)

    def test_dashboard_panels_and_queries(self):
        dash = dashboard()
        self.assertEqual(dash["uid"], "liveprobe-overview")
        panels = {p["title"]: p for p in dash["panels"]}
        required = {
            "Scrape targets": ("prometheus", "up{"),
            "Target container restarts": ("prometheus", "kube_pod_container_status_restarts_total{namespace=\"liveprobe-target\"}"),
            "Target Pod logs": ("loki", "{namespace=\"liveprobe-target\",app=~\"order|inventory\"}"),
        }
        for title, (uid, expr) in required.items():
            with self.subTest(panel=title):
                self.assertIn(title, panels)
                self.assertEqual(panels[title]["datasource"]["uid"], uid)
                self.assertTrue(any(expr in t["expr"] for t in panels[title]["targets"]))
        self.assertEqual(len({p["id"] for p in dash["panels"]}), len(dash["panels"]))

    def test_every_datasource_reference_is_provisioned(self):
        sources = provisioned_datasources()
        refs = []

        def walk(node):
            if isinstance(node, dict):
                if isinstance(node.get("datasource"), dict):
                    refs.append(node["datasource"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(dashboard())
        self.assertTrue(refs)
        for ref in refs:
            self.assertIn(ref["uid"], sources)
            self.assertEqual(ref["type"], sources[ref["uid"]]["type"])

    def test_dashboard_provider_and_mounts(self):
        data = configmap("grafana/grafana.yaml", "grafana-dashboards")
        provider = yaml.safe_load(data["dashboards.yaml"])["providers"][0]
        self.assertEqual((provider["type"], provider["allowUiUpdates"], provider["disableDeletion"]), ("file", False, True))
        spec = workload("grafana/grafana.yaml", "grafana")["spec"]["template"]["spec"]
        mounts = {m["name"]: m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}
        vols = {v["name"]: v for v in spec["volumes"]}
        self.assertEqual(mounts["datasources"], "/etc/grafana/provisioning/datasources")
        self.assertEqual(mounts["dashboard-provider"], "/etc/grafana/provisioning/dashboards")
        self.assertEqual(mounts["dashboard-files"], provider["options"]["path"])
        self.assertEqual(vols["datasources"]["configMap"]["name"], "grafana-datasources")
        self.assertEqual([i["key"] for i in vols["dashboard-provider"]["configMap"]["items"]], ["dashboards.yaml"])
        self.assertEqual([i["key"] for i in vols["dashboard-files"]["configMap"]["items"]], ["liveprobe.json"])

    def test_no_embedded_credentials(self):
        text = (ROOT / "grafana/grafana.yaml").read_text()
        self.assertNotIn("kind: Secret", text)
        env = workload("grafana/grafana.yaml", "grafana")["spec"]["template"]["spec"]["containers"][0]["env"]
        for item in env:
            if re.search(r"(?i)(PASSWORD|ADMIN_USER|TOKEN|SECRET|API_KEY)$", item["name"]):
                self.assertIn("secretKeyRef", item.get("valueFrom", {}), item["name"])
                self.assertNotIn("value", item)


class ComponentBuildTest(unittest.TestCase):
    def test_kustomize_build_contains_log_and_grafana_objects(self):
        tool = shutil.which("kustomize")
        cmd = [tool, "build"] if tool else ["kubectl", "kustomize"]
        rendered = subprocess.run([*cmd, str(ROOT)], capture_output=True, text=True, timeout=60, check=True).stdout
        names = {(d["kind"], d["metadata"]["name"]) for d in yaml.safe_load_all(rendered) if d}
        for expected in [("ConfigMap", "alloy-config"), ("Deployment", "alloy"), ("ConfigMap", "loki-config"),
                         ("PersistentVolumeClaim", "loki-data"), ("Deployment", "loki"), ("Service", "loki"),
                         ("ConfigMap", "grafana-datasources"), ("ConfigMap", "grafana-dashboards"),
                         ("Deployment", "grafana")]:
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
