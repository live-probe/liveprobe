#!/usr/bin/env python3
"""P15 W18: reproduce the agreed 10 failure types on the LIVE operating path (no evaluation runner).

Per case, strictly one at a time:
  gate + baseline -> inject (chaos/run-case.py KAN-71 operations or the authenticated in-pod chaos API, reused
  from evaluation/harness/real.py K8sFaultWorker) -> the running API's watcher opens an incident by itself ->
  unattended analysis/action -> terminal status -> capture overview pin + war room (raw/Korean, flow, chat) ->
  restore baseline and prove it.
A case that ends in handoff/failure is recorded as a failure; cases are never replaced.

Usage: python scripts/p15-live-verify.py [--case F1-01 ...] [--out .e2e-tmp/revamp-p15/live10]
Ground-truth labels never leave the operator host; only public fields (id/target/mode) are logged.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from evaluation.harness.real import K8sFaultWorker  # noqa: E402

BASE = "http://liveprobe.localhost"
HDR = {"X-LiveProbe-Client": "dashboard"}
CASES = ["F1-01", "F1-03", "F1-09", "F2-01", "F2-04", "F2-07", "N2-02", "F3-08", "F3-10", "N3-02"]
TERMINAL = {"resolved", "handed_off", "failed"}


def get(path: str, timeout: int = 20):
    req = urllib.request.Request(BASE + path, headers=HDR)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def case_file(cid: str) -> Path:
    for d in ("cases", "cases-new"):
        p = REPO / "chaos" / d / f"{cid}.yaml"
        if p.exists():
            return p
    raise FileNotFoundError(cid)


def psql(sql: str) -> str:
    r = subprocess.run(["kubectl", "-n", "liveprobe-system", "exec", "postgres-0", "--", "psql", "-U", "liveprobe", "-d",
                        "liveprobe", "-tAc", sql], text=True, capture_output=True, timeout=60)
    return r.stdout.strip()


def capture(page_url: str, path: Path, wait_selector: str | None = None) -> bool:
    """Headless Chromium capture through the dashboard's own Playwright install."""
    script = f"""
const {{ chromium }} = require('playwright');
(async () => {{
  const b = await chromium.launch(); const p = await b.newPage({{ viewport: {{ width: 1440, height: 1000 }} }});
  await p.goto({json.dumps(page_url)}, {{ waitUntil: 'networkidle', timeout: 45000 }});
  {f"await p.waitForSelector({json.dumps(wait_selector)}, {{ timeout: 30000 }});" if wait_selector else ""}
  await p.waitForTimeout(2500);
  await p.screenshot({{ path: {json.dumps(str(path))}, fullPage: false }});
  await b.close();
}})().catch(e => {{ console.error(String(e)); process.exit(1); }});
"""
    r = subprocess.run(["node", "-e", script], cwd=REPO / "dashboard", text=True, capture_output=True, timeout=120)
    return r.returncode == 0


def run_case(w: K8sFaultWorker, cid: str, out: Path, deadline_s: int) -> dict:
    case = json.loads(case_file(cid).read_text())
    public = {"id": cid, "target": case["target"], "expected_signal": case["expected_signal"]}
    rec = {"case": cid, "target": case["target"], "mode": case["injection"]["mode"], "started_at": datetime.now(timezone.utc).isoformat()}
    last_id = int(psql("select coalesce(max(id),0) from incidents") or 0)
    t0 = time.monotonic()
    try:
        # The fault worker reads injection parameters from its own catalog dir; N-cases live in chaos/cases-new.
        w.cases_dir = case_file(cid).parent
        rec["inject"] = w.inject(public)
        iid, inc = None, None
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            row = psql(f"select id from incidents where id > {last_id} and workload = '{case['target']}' order by id limit 1")
            if row:
                iid = int(row)
                inc = get(f"/api/incidents/{iid}") or {}
                if not rec.get("map_pin_live"):
                    # The fault location must be pinned on the operations map WHILE it is open.
                    ov = get("/api/overview")
                    if any((w2.get("incident") or {}).get("id") == iid for w2 in ov.get("workloads", [])):
                        rec["map_pin_live"] = True
                        rec["map_pin_after_s"] = round(time.monotonic() - t0, 1)
                        shots = out / "captures"
                        shots.mkdir(exist_ok=True)
                        if capture(f"{BASE}/", shots / f"{cid}-overview-pin.png", ".react-flow"):
                            rec["capture_overview_pin"] = str(shots / f"{cid}-overview-pin.png")
                if (inc.get("incident") or inc).get("status") in TERMINAL:
                    break
            time.sleep(5)
        rec["incident_id"] = iid
        if iid is None:
            rec["outcome"] = "no_incident"
            return rec
        detail = (inc or {}).get("incident") or inc or {}
        rec["status"] = detail.get("status")
        rec["status_reason"] = detail.get("status_reason")
        rec["level"] = detail.get("severity_level")
        rec["elapsed_s"] = round(time.monotonic() - t0, 1)
        rec["actions"] = [dict(zip(("type", "status", "executed"), r.split("|"))) for r in
                          psql(f"select type,status,executed from actions where incident_id={iid} order by id").splitlines() if r]
        nar = get(f"/api/incidents/{iid}/narrative")
        settle = time.monotonic() + 180
        while nar.get("interpreting") and time.monotonic() < settle:   # display-only Korean readings finish after close
            time.sleep(10)
            nar = get(f"/api/incidents/{iid}/narrative")
        rec["interpreting_at_capture"] = bool(nar.get("interpreting"))
        raw = nar.get("raw") or []
        rec["raw_items"] = len(raw)
        rec["korean"] = sum(1 for x in raw if x.get("ko"))
        rec["flow_nodes"] = [n.get("role") or n.get("type") for n in (nar.get("flow") or {}).get("nodes", [])]
        rec["headline"] = nar.get("headline")
        rec["map_pin"] = bool(rec.get("map_pin_live"))
        shots = out / "captures"
        shots.mkdir(exist_ok=True)
        rec["capture_room"] = capture(f"{BASE}/incidents/{iid}", shots / f"{cid}-room.png", ".flow-canvas") and str(shots / f"{cid}-room.png")
        rec["outcome"] = "auto_completed" if rec["status"] == "resolved" else rec["status"]
        return rec
    except Exception as e:  # noqa: BLE001 - record and keep the failure, never replace the case
        rec["outcome"] = "error"
        rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        return rec
    finally:
        ok, info = w.restore_baseline(public)
        rec["restore"] = {**info, "ok": ok}
        rec["finished_at"] = datetime.now(timezone.utc).isoformat()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", action="append", dest="cases")
    ap.add_argument("--out", default=str(REPO / ".e2e-tmp/revamp-p15/live10"))
    ap.add_argument("--deadline-s", type=int, default=900)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    results = out / "results.jsonl"
    cases = a.cases or CASES
    w = K8sFaultWorker()
    w.acquire()
    for cid in cases:
        print(f"== {cid}", flush=True)
        rec = run_case(w, cid, out, a.deadline_s)
        with results.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(json.dumps({k: rec.get(k) for k in ("case", "incident_id", "status", "level", "outcome", "elapsed_s", "map_pin", "korean", "raw_items")},
                         ensure_ascii=False), flush=True)
        if not (rec.get("restore") or {}).get("ok"):
            print("baseline restore failed: stopping", flush=True)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
