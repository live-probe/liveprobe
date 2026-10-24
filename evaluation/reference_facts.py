"""Collect the observed facts the judgement guide is built from (P12).

Only run records are read: routing level, agent conclusions, actions and their observed outcomes (post-verification
passed or not), cost and time. Case answer keys and the metrics report (whose success verdicts are derived from the
answer keys) are deliberately NOT inputs, so the guide cannot carry evaluation answers to the new cases (P13).

  python -m evaluation.reference_facts --out .e2e-tmp/revamp-P12/reference-facts.json STORE_DIR:POLICY ...
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

FAMILY_NAME = {"F1": "image_crash", "F2": "oom", "F3": "latency_pool", "F4": "endpoint_loss"}


def collect(stores: list[tuple[Path, str]]) -> dict:
    groups: dict = collections.defaultdict(lambda: collections.defaultdict(list))
    for store, policy in stores:
        for line in (store / "progress.jsonl").read_text("utf-8").splitlines():
            r = json.loads(line)
            if r.get("kind") != "done" or r.get("status") != "completed":
                continue
            o = r["output"]
            t = o.get("telemetry") or {}
            fam = FAMILY_NAME.get(r["case_id"][:2], "other")
            acts = [(a["type"], a["status"]) for a in t.get("actions", [])]
            groups[fam][policy].append({
                "run": f"{store.name}#{r['case_id']}/{r['arm_id']}#att{r.get('attempt', 1)}",
                "incident_id": o.get("incident_id"),
                "arm": r["arm_id"],
                "level": o.get("selected_workflow"),
                "outcome": (o.get("outcome") or {}).get("kind"),
                "outcome_verified": (o.get("outcome") or {}).get("verified"),
                "actions": acts,
                "status_reason": (t.get("status_reason") or "")[:240],
                "top_cause_classes": [c.get("cause_class") for c in (o.get("root_cause_candidates") or [])[:3]],
                "wall_s": t.get("wall_s"),
                "cost_usd": o.get("cost_usd"),
                "violations": len(o.get("policy_violations") or []),
            })
    return {"schema": "liveprobe-reference-facts/v1", "families": groups}


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("stores", nargs="+", help="STORE_DIR:POLICY")
    a = p.parse_args(argv)
    specs = []
    for s in a.stores:
        store, policy = s.rsplit(":", 1)
        specs.append((Path(store), policy))
    facts = collect(specs)
    a.out.write_text(json.dumps(facts, ensure_ascii=False, indent=1, default=str) + "\n", "utf-8")
    n = sum(len(v) for fam in facts["families"].values() for v in fam.values())
    print(f"wrote {a.out} ({n} runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
