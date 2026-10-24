#!/usr/bin/env python3
"""API smoke for the login-free build: reads work, foreign Host and cross-site writes are refused."""
import json
import urllib.error
import urllib.request

BASE = "http://liveprobe.localhost/api"


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


assert get("/healthz") == (200, {"status": "ok"})
assert get("/readyz")[0] == 200
for path in ("/incidents", "/status", "/topology", "/llm/accounts", "/policy/profile"):
    status, _ = get(path)
    assert status == 200, (path, status)
assert "login" not in json.dumps(get("/status")[1]).lower()


def raw(path, method="GET", headers=None):
    req = urllib.request.Request(BASE + path, method=method, headers=headers or {}, data=b"{}" if method == "POST" else None)
    try:
        return urllib.request.urlopen(req, timeout=20).status
    except urllib.error.HTTPError as error:
        return error.code


# Direct to the ingress with a foreign Host the proxy answers 404; the API guard itself is covered by tests/test_api.py.
assert raw("/incidents", headers={"Host": "evil.example"}) in (403, 404)
# A write without the dashboard header or with a foreign Origin must never be accepted.
assert raw("/settings", "PUT", {"Origin": "http://evil.example", "Content-Type": "application/json"}) == 403
assert raw("/settings", "PUT", {"Content-Type": "application/json"}) == 403
print("health/ready 200; read routes open; foreign Host and cross-site/unmarked writes refused: PASS")
