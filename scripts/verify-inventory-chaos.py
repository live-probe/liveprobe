#!/usr/bin/env python3
"""Operator-only live smoke for KAN-68; does not print or persist the token."""
import base64
import json
import sys
import subprocess
import time
import urllib.error
import urllib.request

K = ["kubectl", "--context", "rancher-desktop", "-n", "liveprobe-target"]

def call(path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request("http://127.0.0.1:18080" + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""

raw = subprocess.check_output(K + ["get", "secret", "inventory-chaos-operator", "-o", "jsonpath={.data.token}"], stderr=subprocess.DEVNULL)
token = base64.b64decode(raw).decode().strip()
pf = subprocess.Popen(K + ["port-forward", "service/inventory", "18080:8080"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try:
            if call("/api/stock/a")[0] == 200:
                break
        except (OSError, TimeoutError):
            time.sleep(.25)
    else:
        raise RuntimeError("port-forward baseline not ready")
    assert call("/internal/chaos/reset", {}, None)[0] == 401
    assert call("/internal/chaos/reset", {}, "wrong")[0] == 401
    bad = call("/internal/chaos/inject", {"duration_seconds": 31, "delay_ms": 100}, token)[0]
    assert bad == 400, f"invalid duration returned {bad}"
    assert call("/internal/chaos/inject", {"duration_seconds": 2, "delay_ms": 1200}, token)[0] == 200
    started = time.monotonic()
    assert call("/api/stock/a")[0] == 200
    delay = time.monotonic() - started
    assert delay > 1.0, delay
    assert call("/internal/chaos/reset", {}, token)[0] == 200
    assert call("/api/stock/a")[0] == 200
    print("PASS auth 401/401 bounds 400 latency %.2fs reset baseline 200" % delay)

    assert call("/internal/chaos/inject", {"duration_seconds": 4, "connections": 5}, token)[0] == 200
    assert call("/api/stock/a")[0] == 503
    assert call("/internal/chaos/reset", {}, token)[0] == 200
    assert call("/api/stock/a")[0] == 200
    print("PASS pool exhaustion 503 reset baseline 200")

    assert call("/internal/chaos/inject", {"duration_seconds": 2, "error_percent": 100}, token)[0] == 200
    assert call("/api/stock/a")[0] == 503
    time.sleep(2.4)
    assert call("/api/stock/a")[0] == 200
    print("PASS error_rate 503 automatic expiry baseline 200")

    assert call("/internal/chaos/inject", {"duration_seconds": 3, "megabytes": 16}, token)[0] == 200
    assert call("/api/stock/a")[0] == 200
    assert call("/internal/chaos/reset", {}, token)[0] == 200
    assert call("/api/stock/a")[0] == 200
    print("PASS bounded memory_hold 16Mi reset baseline 200")

    if "--oom" in sys.argv:
        before = json.loads(subprocess.check_output(K + ["get", "pods", "-l", "app=inventory", "-o", "json"]))
        old = before["items"][0]
        assert call("/internal/chaos/oom", {}, token)[0] == 202
        for _ in range(90):
            time.sleep(1)
            now = json.loads(subprocess.check_output(K + ["get", "pods", "-l", "app=inventory", "-o", "json"]))
            if any(p["metadata"]["uid"] == old["metadata"]["uid"]
                   and p.get("status", {}).get("containerStatuses", [{}])[0].get("restartCount", 0)
                   > old.get("status", {}).get("containerStatuses", [{}])[0].get("restartCount", 0)
                   for p in now["items"]):
                break
        else:
            raise RuntimeError("OOM did not restart the inventory container")
        for _ in range(60):
            status = subprocess.run(K + ["exec", "deployment/loadgen", "--", "curl", "-s", "-o", "/dev/null",
                                         "-w", "%{http_code}", "--max-time", "5",
                                         "http://inventory.liveprobe-target.svc:8080/api/stock/a"],
                                    capture_output=True, text=True, timeout=12)
            if status.returncode == 0 and status.stdout == "200":
                break
            time.sleep(1)
        else:
            raise RuntimeError("baseline did not recover after OOM")
        print("PASS bounded OOM restarted container and baseline 200")
finally:
    pf.terminate()
    try:
        pf.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pf.kill()
