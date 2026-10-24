#!/usr/bin/env python3
"""KAN-103 disposable kind proof; hard-coded context, always deletes only its cluster."""
from __future__ import annotations

import base64
import json
import os
import sys
import secrets
from pathlib import Path
import subprocess
import ssl
import tempfile
import time
import urllib.error
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "liveprobe"))
from app.rollback_grant import issue
NAME = "kan103-rollback"
CONTEXT = "kind-" + NAME
SA = "system:serviceaccount:liveprobe-system:liveprobe"
RB = "system:serviceaccount:liveprobe-system:rollback-controller"


def run(*args, input=None, ok=True):
    result = subprocess.run(args, input=input, text=True, capture_output=True)
    if ok and result.returncode:
        raise AssertionError(f"command {args[:5]} failed: {result.stderr[:900]}")
    return result


def k(*args, **kwargs):
    return run("kubectl", "--context", CONTEXT, *args, **kwargs)


def objects(path, kinds):
    docs = list(yaml.safe_load_all(path.read_text()))
    return yaml.safe_dump_all([d for d in docs if d and d["kind"] in kinds])


def assertion(label, condition):
    if not condition:
        raise AssertionError(label)
    print("PASS", label, flush=True)


def main():
    # Explicitly reject a stale/pre-existing name rather than deleting another job.
    existing = run("kind", "get", "clusters").stdout.splitlines()
    if NAME in existing:
        raise RuntimeError("isolated cluster name already exists; remove it manually before rerunning")
    run("docker", "build", "-f", str(ROOT / "liveprobe/Dockerfile.rollback"),
        "-t", "liveprobe/rollback-controller:dev", str(ROOT / "liveprobe"))
    run("docker", "pull", "busybox:1.37")
    run("kind", "create", "cluster", "--name", NAME, "--image", "kindest/node:v1.34.0")
    try:
        run("kind", "load", "docker-image", "liveprobe/rollback-controller:dev", "--name", NAME)
        with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"]) as tmp:
            tmp = Path(tmp)
            tls = tmp / "tls.crt"
            key = tmp / "tls.key"
            run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(tls), "-days", "1",
                "-subj", "/CN=rollback-controller.liveprobe-system.svc",
                "-addext", "subjectAltName=DNS:rollback-controller.liveprobe-system.svc")
            for ns in ("liveprobe-system", "liveprobe-target"):
                k("create", "namespace", ns)
            digest = run("docker", "image", "inspect", "busybox:1.37", "--format", "{{json .RepoDigests}}")
            safe = "docker.io/library/" + json.loads(digest.stdout)[0]
            assertion("trusted digest", "@sha256:" in safe)
            config = k("-n", "liveprobe-system", "create", "configmap", "rollback-image-policy",
                       f"--from-literal=inventory={safe}", f"--from-literal=order={safe}",
                       "--dry-run=client", "-o", "yaml").stdout
            k("apply", "-f", "-", input=config)
            secret = k("-n", "liveprobe-system", "create", "secret", "tls", "rollback-admission-tls",
                       "--cert", str(tls), "--key", str(key), "--dry-run=client", "-o", "yaml").stdout
            k("apply", "-f", "-", input=secret)
            grant_key = secrets.token_bytes(48)
            grant_file = tmp / "key"
            grant_file.write_bytes(grant_key)
            grant_file.chmod(0o600)
            grant_secret = k("-n", "liveprobe-system", "create", "secret", "generic", "rollback-action-grant",
                             "--from-file=key=" + str(grant_file), "--dry-run=client", "-o", "yaml").stdout
            k("apply", "-f", "-", input=grant_secret)
            manifest = ROOT / "deploy/base/liveprobe/rollback-controller.yaml"
            k("apply", "-f", "-", input=objects(manifest, {"ServiceAccount", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding", "Deployment", "Service"}))
            reader = ROOT / "deploy/base/liveprobe/liveprobe.yaml"
            k("apply", "-f", "-", input=objects(reader, {"ServiceAccount", "ClusterRole", "ClusterRoleBinding"}))
            k("apply", "-f", str(ROOT / "deploy/base/liveprobe/liveprobe-write-guard.yaml"))
            k("-n", "liveprobe-system", "rollout", "status", "deployment/rollback-controller", "--timeout=180s")
            # Install admission only after cert/service are live; no live cluster touched.
            k("apply", "-f", "-", input=objects(manifest, {"ValidatingWebhookConfiguration"}))
            ca = base64.b64encode(tls.read_bytes()).decode("ascii")
            patch = [{"op": "add", "path": "/webhooks/0/clientConfig/caBundle", "value": ca}]
            k("patch", "validatingwebhookconfiguration", "rollback-controller-image-guard", "--type=json",
              "-p", json.dumps(patch))
            assertion("API no write", k("auth", "can-i", "patch", "deployments.apps/inventory",
                                         "--as=" + SA, "-n", "liveprobe-target", ok=False).stdout.strip() == "no")
            assertion("controller named write", k("auth", "can-i", "patch", "deployments.apps/inventory",
                                                    "--as=" + RB, "-n", "liveprobe-target").stdout.strip() == "yes")
            assertion("controller other deployment denied", k("auth", "can-i", "patch", "deployments.apps/loadgen",
                                                                "--as=" + RB, "-n", "liveprobe-target", ok=False).stdout.strip() == "no")
            for sa in (SA, RB):
                assertion(sa.split(":")[-1] + " secret read denied",
                          k("auth", "can-i", "get", "secrets", "--as=" + sa,
                            "-n", "liveprobe-target", ok=False).stdout.strip() == "no")
            # Minimal synthetic workload, no production target or credential.
            dep = {"apiVersion": "apps/v1", "kind": "Deployment",
                   "metadata": {"name": "inventory", "namespace": "liveprobe-target"},
                   "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "inventory"}},
                            "template": {"metadata": {"labels": {"app": "inventory"}},
                                         "spec": {"containers": [{"name": "inventory", "image": safe,
                                                                  "command": ["sh", "-c", "sleep 3600"]}]}}}}
            k("apply", "-f", "-", input=yaml.safe_dump(dep))
            service = {"apiVersion": "v1", "kind": "Service",
                       "metadata": {"name": "inventory", "namespace": "liveprobe-target"},
                       "spec": {"selector": {"app": "inventory"}, "ports": [{"port": 80, "targetPort": 80}]}}
            k("apply", "-f", "-", input=yaml.safe_dump(service))
            k("-n", "liveprobe-target", "rollout", "status", "deployment/inventory", "--timeout=180s")
            # A broken image creates an actual prior RS and records rollout history.
            k("-n", "liveprobe-target", "set", "image", "deployment/inventory", "inventory=busybox:nonexistent")
            time.sleep(4)
            before = json.loads(k("-n", "liveprobe-target", "get", "deployment", "inventory", "-o", "json").stdout)
            bad_image = before["spec"]["template"]["spec"]["containers"][0]["image"]
            assertion("bad image set", bad_image == "busybox:nonexistent")
            # Adversarial escalation: explicit RoleBinding grants API patch, VAP must still deny.
            escalation = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
                          "metadata": {"name": "adversarial", "namespace": "liveprobe-target"},
                          "rules": [{"apiGroups": ["apps"], "resources": ["deployments"],
                                     "resourceNames": ["inventory"], "verbs": ["patch"]},
                                    {"apiGroups": [""], "resources": ["services"],
                                     "resourceNames": ["inventory"], "verbs": ["patch"]}]}
            k("apply", "-f", "-", input=yaml.safe_dump(escalation))
            escalation["kind"] = "RoleBinding"
            escalation.pop("rules")
            escalation["roleRef"] = {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "adversarial"}
            escalation["subjects"] = [{"kind": "ServiceAccount", "name": "liveprobe", "namespace": "liveprobe-system"}]
            k("apply", "-f", "-", input=yaml.safe_dump(escalation))
            denial = k("-n", "liveprobe-target", "--as=" + SA, "patch", "deployment", "inventory",
                       "--type=json", "-p", json.dumps([{"op": "replace",
                          "path": "/spec/template/spec/containers/0/image", "value": "busybox:evil"}]), ok=False)
            assertion("escalated API patch rejected by admission", denial.returncode != 0 and "denied" in denial.stderr.lower())
            selector = k("-n", "liveprobe-target", "--as=" + SA, "patch", "service", "inventory",
                         "--type=merge", "-p", json.dumps({"spec": {"selector": {"app": "evil"}}}), ok=False)
            assertion("escalated Service selector rejected", selector.returncode != 0 and "denied" in selector.stderr.lower())
            for label, path, value in (("arbitrary image", "/spec/template/spec/containers/0/image", "busybox:evil"),
                                       ("env secretRef", "/spec/template/spec/containers/0/env", [{"name": "LEAK", "valueFrom": {"secretKeyRef": {"name": "dummy-token", "key": "token"}}}]),
                                       ("service account", "/spec/template/spec/serviceAccountName", "default")):
                denied = k("-n", "liveprobe-target", "--as=" + RB, "patch", "deployment", "inventory", "--type=json",
                           "-p", json.dumps([{"op": "add", "path": path, "value": value}]), ok=False)
                assertion("controller rejects " + label,
                          denied.returncode != 0 and "denied" in denied.stderr.lower())
            # Isolated HTTPS probe; cert validated against the generated local CA.
            port = subprocess.Popen(["kubectl", "--context", CONTEXT, "-n", "liveprobe-system",
                                     "port-forward", "service/rollback-controller", "18443:443"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                token = k("-n", "liveprobe-system", "create", "token", "liveprobe",
                          "--audience=liveprobe-rollback", "--duration=10m").stdout.strip()
                other = k("-n", "liveprobe-system", "create", "token", "rollback-controller",
                          "--audience=liveprobe-rollback", "--duration=10m").stdout.strip()
                default_audience = k("-n", "liveprobe-system", "create", "token", "liveprobe",
                                     "--duration=10m").stdout.strip()
                context = ssl.create_default_context(cafile=str(tls))
                body = issue(grant_key, incident_id=1, action_id=1, name="inventory",
                             expected_revision=int(before["metadata"]["annotations"]["deployment.kubernetes.io/revision"]),
                             expected_source=next(rs["metadata"]["name"] for rs in
                                  json.loads(k("-n", "liveprobe-target", "get", "replicasets", "-o", "json").stdout)["items"]
                                  if rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision") == "1"))
                def request(auth, payload=body):
                    headers = {"Content-Type": "application/json"}
                    if auth:
                        headers["Authorization"] = "Bearer " + auth
                    req = urllib.request.Request("https://127.0.0.1:18443/undo",
                                                 data=json.dumps(payload).encode(), headers=headers)
                    try:
                        with urllib.request.urlopen(req, context=context, timeout=3) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as exc:
                        return exc.code, None
                # Port-forward loopback does not match the service DNS on the cert.
                # Verify the generated CA here; the application client verifies
                # both CA and hostname against the real Service DNS.
                context.check_hostname = False
                for _ in range(40):
                    if port.poll() is not None:
                        raise AssertionError("port-forward exited")
                    try:
                        status, _ = request(None)
                        break
                    except OSError:
                        time.sleep(.5)
                else:
                    raise AssertionError("controller request unavailable")
                assertion("unauthenticated HTTP cannot patch", status == 403)
                assertion("wrong SA cannot patch", request(other)[0] == 403)
                assertion("wrong audience cannot patch", request(default_audience)[0] == 403)
                assertion("missing action grant cannot patch", request(token, {"name": "inventory"})[0] == 403)
                assertion("forged action id cannot patch", request(token, {**body, "action_id": 999})[0] == 403)
                assertion("forged target cannot patch", request(token, {**body, "name": "order"})[0] == 403)
                assertion("expired grant cannot patch", request(token, issue(grant_key, incident_id=1,
                          action_id=1, name="inventory", expected_revision=body["expected_revision"],
                          expected_source=body["expected_source"], now=int(time.time()) - 60))[0] == 403)
                assertion("wrong revision cannot patch", request(token, {**body, "expected_revision": 999})[0] == 403)
                assertion("unauthorized attempts leave bad image", json.loads(k("-n", "liveprobe-target", "get", "deployment", "inventory", "-o", "json").stdout)["spec"]["template"]["spec"]["containers"][0]["image"] == bad_image)
                status, result = request(token)
                assertion("signed, bounded grant accepted", status == 200)
                assertion("replayed grant cannot patch", request(token)[0] == 403)
            finally:
                port.terminate()
                port.wait(timeout=5)
            assertion("named undo returned provenance", result["to_revision"] >= 1 and result["to_images"] == [safe])
            k("-n", "liveprobe-target", "rollout", "status", "deployment/inventory", "--timeout=180s")
            after = json.loads(k("-n", "liveprobe-target", "get", "deployment", "inventory", "-o", "json").stdout)
            assertion("positive rollout_undo restored Ready digest", after["spec"]["template"]["spec"]["containers"][0]["image"] == safe and after["status"].get("availableReplicas") == 1)
    finally:
        run("kind", "delete", "cluster", "--name", NAME)


if __name__ == "__main__":
    main()
