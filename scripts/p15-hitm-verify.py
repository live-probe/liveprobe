#!/usr/bin/env python3
"""P15 W17: operator-confirmed command live verification in a disposable sandbox namespace.

Steps (each recorded as JSON):
  1. ConfigMap create -> plan/dry-run/side effects -> confirm -> execute -> verify -> rollback (deleted)
  2. Explicit ConfigMap delete -> assert unchanged UID/RV/content before confirm -> execute -> recreate on rollback
  3. Deployment scale 1 -> 0: side effects must say service interruption; execute; rollback to 1
  4. Secret write-only: plan with key names only, masked value via /secret-values, then search DB rows,
     API logs and run events for the sentinel value (must be absent)
  5. Bad confirmation hash is rejected; cluster_change isolation is covered separately by API regression tests.
The sandbox namespace is created and deleted by this script (kubectl as the local admin), never by the API.

Usage: python scripts/p15-hitm-verify.py --incident <id> [--out <dir>]
No credential value is printed. The sentinel is random per run and only its absence is reported.
"""
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://liveprobe.localhost"
NS = "p15-sandbox"
HDR = {"X-LiveProbe-Client": "dashboard", "Content-Type": "application/json", "Origin": BASE}
TERMINAL = {"succeeded", "failed", "cancelled", "rejected", "rolled_back"}


def api(method: str, path: str, body: dict | bytes | None = None, timeout: int = 30) -> tuple[int, dict]:
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=HDR)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def kubectl(*args: str, check: bool = True, stdin: str | None = None) -> str:
    r = subprocess.run(["kubectl", *args], input=stdin, text=True, capture_output=True, timeout=120)
    if check and r.returncode:
        raise RuntimeError(f"kubectl {' '.join(args[:3])} failed: {r.stderr.strip()[-300:]}")
    return r.stdout


def wait_status(cid: int, wanted: set[str], deadline_s: int = 420) -> dict:
    end = time.time() + deadline_s
    row: dict = {}
    while time.time() < end:
        code, listing = api("GET", f"/api/incidents/{INCIDENT}/commands")
        row = next((c for c in listing.get("items", []) if c["id"] == cid), {})
        if row.get("status") in wanted:
            return row
        time.sleep(3)
    raise TimeoutError(f"command {cid} stuck in {row.get('status')}")


def command(instruction: str) -> dict:
    code, body = api("POST", f"/api/incidents/{INCIDENT}/commands", {"instruction": instruction})
    if code != 201:
        raise RuntimeError(f"create failed {code} {body}")
    return wait_status(body["command"]["id"], {"awaiting_confirmation", "awaiting_secret", "rejected", "failed"})


def summary(row: dict) -> dict:
    eff = row.get("side_effects") or {}
    return {"id": row.get("id"), "status": row.get("status"), "error": row.get("error"),
            "ops": [(o.get("op"), o.get("kind"), o.get("namespace"), o.get("name")) for o in (row.get("plan") or {}).get("operations", [])],
            "dry_run_ok": [d.get("ok") for d in row.get("dry_run") or []],
            "downtime": eff.get("downtime"), "restarts": eff.get("restarts"), "reversible": eff.get("reversible"),
            "warnings_ko": eff.get("warnings_ko"), "items_ko": eff.get("items_ko"),
            "verification": row.get("verification"), "rollback": {k: v for k, v in (row.get("rollback") or {}).items() if k != "operations"}}


def main() -> int:
    global INCIDENT
    ap = argparse.ArgumentParser()
    ap.add_argument("--incident", type=int, required=True)
    ap.add_argument("--out", default=".e2e-tmp/revamp-p15/hitm")
    a = ap.parse_args()
    INCIDENT = a.incident
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rec: dict = {"incident": INCIDENT, "namespace": NS, "steps": {}}
    sentinel = "p15-sentinel-" + secrets.token_hex(12)
    failures: list[str] = []

    def check(name: str, ok: bool, detail=None):
        rec["steps"].setdefault("checks", []).append({"name": name, "ok": bool(ok), "detail": detail})
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail is not None and not ok else ""))
        if not ok:
            failures.append(name)

    kubectl("create", "namespace", NS)
    kubectl("label", "namespace", NS, "liveprobe.io/purpose=p15-hitm")
    try:
        kubectl("-n", NS, "create", "deployment", "echo", "--image=busybox:1.37", "--replicas=1", "--", "sleep", "3600")
        kubectl("-n", NS, "create", "secret", "generic", "app-credentials", "--from-literal=API_TOKEN=placeholder")
        kubectl("-n", NS, "rollout", "status", "deploy/echo", "--timeout=120s", check=False)

        # 1) ConfigMap create + rollback
        row = command(f"{NS} 네임스페이스에 p15-note라는 ConfigMap을 만들고 data의 message 키에 hello 값을 넣어줘")
        rec["steps"]["configmap_plan"] = summary(row)
        check("ConfigMap 계획 확인 대기", row["status"] == "awaiting_confirmation", row.get("error"))
        if row["status"] == "awaiting_confirmation":
            code, _ = api("POST", f"/api/commands/{row['id']}/confirm", {"plan_sha256": "0" * 64})
            check("잘못된 해시 확인 거부(409/422)", code in (409, 422), code)
            check("확인 전 미생성", not kubectl("-n", NS, "get", "configmap", "p15-note", "--ignore-not-found", "-o", "name").strip())
            code, _ = api("POST", f"/api/commands/{row['id']}/confirm", {"plan_sha256": row["plan_sha256"]})
            done = wait_status(row["id"], TERMINAL)
            rec["steps"]["configmap_exec"] = summary(done)
            got = kubectl("-n", NS, "get", "configmap", "p15-note", "-o", "jsonpath={.data.message}", check=False)
            check("ConfigMap 실행 성공·실제 값", done["status"] == "succeeded" and got == "hello", (done["status"], got))
            code, rb = api("POST", f"/api/commands/{row['id']}/rollback", {"plan_sha256": row["plan_sha256"]}, timeout=120)
            rec["steps"]["configmap_rollback"] = {"http": code, **summary(rb if isinstance(rb, dict) and "status" in rb else {})}
            gone = not kubectl("-n", NS, "get", "configmap", "p15-note", "--ignore-not-found", "-o", "name").strip()
            check("ConfigMap 원복(삭제)·검증", code == 200 and rb.get("status") == "rolled_back" and gone, (code, rb.get("status"), gone))

        # 2) Explicit deletion: planning/server dry-run must not delete or change the object before confirmation.
        kubectl("-n", NS, "create", "configmap", "p15-delete-probe", "--from-literal=message=keep-before-confirmation")
        original = json.loads(kubectl("-n", NS, "get", "configmap", "p15-delete-probe", "-o", "json"))
        row = command(f"{NS} 네임스페이스의 p15-delete-probe ConfigMap을 삭제해줘")
        rec["steps"]["delete_plan"] = summary(row)
        check("삭제 계획 확인 대기", row["status"] == "awaiting_confirmation", row.get("error"))
        observed_text = kubectl("-n", NS, "get", "configmap", "p15-delete-probe", "--ignore-not-found", "-o", "json")
        observed = json.loads(observed_text) if observed_text.strip() else None
        check("삭제 확인 전 UID·RV·내용 불변", observed == original)
        if row["status"] == "awaiting_confirmation":
            api("POST", f"/api/commands/{row['id']}/confirm", {"plan_sha256": row["plan_sha256"]})
            done = wait_status(row["id"], TERMINAL)
            rec["steps"]["delete_exec"] = summary(done)
            gone = not kubectl("-n", NS, "get", "configmap", "p15-delete-probe", "--ignore-not-found", "-o", "name").strip()
            check("삭제 확인 후 실행·실제 소멸", done["status"] == "succeeded" and gone, (done["status"], gone))
            code, rb = api("POST", f"/api/commands/{row['id']}/rollback", {"plan_sha256": row["plan_sha256"]}, timeout=120)
            restored_text = kubectl("-n", NS, "get", "configmap", "p15-delete-probe", "--ignore-not-found", "-o", "json")
            restored = json.loads(restored_text) if restored_text.strip() else {}
            rec["steps"]["delete_rollback"] = {"http": code, **summary(rb)}
            check("삭제 원복·내용 복원·새 UID", code == 200 and rb.get("status") == "rolled_back"
                  and restored.get("data") == original["data"] and (restored.get("metadata") or {}).get("uid") not in (None, original["metadata"]["uid"]))

        # 3) scale 1 -> 0 must disclose interruption
        row = command(f"{NS} 네임스페이스의 echo Deployment replicas를 0으로 줄여줘")
        rec["steps"]["scale_plan"] = summary(row)
        check("scale 0 계획: 서비스 중단 예상 표시", row["status"] == "awaiting_confirmation" and (row.get("side_effects") or {}).get("downtime") == "expected",
              (row["status"], (row.get("side_effects") or {}).get("downtime"), row.get("error")))
        if row["status"] == "awaiting_confirmation":
            api("POST", f"/api/commands/{row['id']}/confirm", {"plan_sha256": row["plan_sha256"]})
            done = wait_status(row["id"], TERMINAL)
            rec["steps"]["scale_exec"] = summary(done)
            rep = kubectl("-n", NS, "get", "deploy", "echo", "-o", "jsonpath={.spec.replicas}")
            check("scale 실행·실제 replicas 0", done["status"] == "succeeded" and rep == "0", (done["status"], rep))
            code, rb = api("POST", f"/api/commands/{row['id']}/rollback", {"plan_sha256": row["plan_sha256"]}, timeout=120)
            rep = kubectl("-n", NS, "get", "deploy", "echo", "-o", "jsonpath={.spec.replicas}")
            rec["steps"]["scale_rollback"] = {"http": code, "status": rb.get("status"), "replicas": rep}
            check("scale 원복 replicas 1", code == 200 and rb.get("status") == "rolled_back" and rep == "1", (code, rb.get("status"), rep))

        # 3) Secret write-only
        row = command(f"{NS} 네임스페이스의 app-credentials Secret에서 API_TOKEN 키 값을 바꾸고 싶어. 값은 내가 따로 입력할게")
        rec["steps"]["secret_plan"] = summary(row)
        check("Secret 계획: 키 이름만, 값 미요청", row["status"] == "awaiting_confirmation" and row["plan"]["operations"][0].get("keys") == ["API_TOKEN"],
              (row["status"], row.get("error"), (row.get("plan") or {}).get("operations")))
        if row["status"] == "awaiting_confirmation":
            code, conf = api("POST", f"/api/commands/{row['id']}/confirm", {"plan_sha256": row["plan_sha256"]})
            check("Secret 확인 후 값 입력 대기", conf.get("status") == "awaiting_secret", conf.get("status"))
            code, bad = api("POST", f"/api/commands/{row['id']}/secret-values", json.dumps({"plan_sha256": row["plan_sha256"], "values": {"OTHER": sentinel}}).encode())
            check("계획 밖 키 거부", code in (409, 422), code)
            check("거부 응답에 값 미포함", sentinel not in json.dumps(bad), None)
            code, ok = api("POST", f"/api/commands/{row['id']}/secret-values", json.dumps({"plan_sha256": row["plan_sha256"], "values": {"API_TOKEN": sentinel}}).encode())
            rec["steps"]["secret_write"] = {"http": code, "status": ok.get("status"), "keys": ok.get("keys")}
            # Verify through a local admin read, comparing in-process only (never printed).
            import base64
            stored = base64.b64decode(kubectl("-n", NS, "get", "secret", "app-credentials", "-o", "jsonpath={.data.API_TOKEN}") or b"").decode()
            check("Secret 실제 반영(로컬 관리자 비교, 값 미출력)", code == 200 and ok.get("status") == "succeeded" and stored == sentinel, (code, ok.get("status"), stored == sentinel))
            check("응답에 값 미포함", sentinel not in json.dumps(ok), None)

        # 4) Sentinel absence across DB / logs / events / API views
        sql = ("select count(*) from (select instruction::text t from operator_commands union all select plan::text from operator_commands "
               "union all select result::text from operator_commands union all select coalesce(error,'') from operator_commands "
               "union all select verification::text from operator_commands union all select dry_run::text from operator_commands "
               "union all select side_effects::text from operator_commands union all select rollback::text from operator_commands "
               "union all select body from operator_messages union all select intent_detail::text from operator_messages "
               "union all select detail::text from run_events union all select summary from run_events "
               "union all select output::text from agent_runs union all select coalesce(error,'') from agent_runs "
               "union all select detail::text from audit_log union all select event::text from incident_events "
               "union all select ko from interpretations) x where t like '%" + sentinel + "%'")
        hits = kubectl("-n", "liveprobe-system", "exec", "postgres-0", "--", "psql", "-U", "liveprobe", "-d", "liveprobe", "-tAc", sql, check=False).strip()
        check("DB 전체(명령·메시지·이벤트·Agent·감사)에 sentinel 없음", hits == "0", hits)
        logs = kubectl("-n", "liveprobe-system", "logs", "deploy/liveprobe-api", "-c", "api", "--since=30m", check=False)
        check("API 로그에 sentinel 없음", sentinel not in logs, None)
        _, ev = api("GET", f"/api/incidents/{INCIDENT}/events")
        _, nar = api("GET", f"/api/incidents/{INCIDENT}/narrative")
        _, cl = api("GET", f"/api/incidents/{INCIDENT}/commands")
        check("사건 이벤트·서술·명령 API에 sentinel 없음", all(sentinel not in json.dumps(x) for x in (ev, nar, cl)), None)
        planner = kubectl("-n", "liveprobe-system", "exec", "postgres-0", "--", "psql", "-U", "liveprobe", "-d", "liveprobe", "-tAc",
                          f"select count(*) from agent_runs where incident_id={INCIDENT} and role='operator_planner' and created_at > now() - interval '30 minutes'", check=False).strip()
        rec["steps"]["planner_runs"] = planner
    finally:
        kubectl("delete", "namespace", NS, "--wait=true", "--timeout=180s", check=False)
        left = kubectl("get", "namespace", NS, "--ignore-not-found", "-o", "name", check=False).strip()
        check("sandbox namespace 정리", not left, left)
    rec["failures"] = failures
    (out / "hitm-result.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2))
    print(f"checks={len(rec['steps']['checks'])} failures={len(failures)} -> {out / 'hitm-result.json'}")
    return 1 if failures else 0


INCIDENT = 0
if __name__ == "__main__":
    sys.exit(main())
