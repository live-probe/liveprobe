"""CLI: deterministic recompute of KAN-59 metrics from an immutable harness store.

  python3 -m evaluation.metrics compute --store DIR [--cases-dir DIR] [--out FILE]
  python3 -m evaluation.metrics verify  --store DIR --report FILE [--cases-dir DIR]
  python3 -m evaluation.metrics definitions

`compute` prints (or writes) the canonical report JSON; the same inputs always
give byte-identical output. `verify` recomputes independently and compares
digests per aggregate. Exit codes: 0 ok, 1 verify mismatch, 2 invalid input,
3 report computed but not publishable (dry-run store, missing pairs, issues)
when --require-publishable is given. Never writes into --store.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from evaluation.harness.cases import CASES_DIR

from .compute import compute, verify_report
from .definitions import definitions_payload
from .store_import import StoreInvalid, load_store
from .truth import ApprovedTruthSnapshot, TruthNotApproved


def _dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=1) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m evaluation.metrics", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("compute", "verify"):
        s = sub.add_parser(name)
        s.add_argument("--store", type=Path, required=True)
        s.add_argument("--cases-dir", type=Path, default=CASES_DIR)
        if name == "compute":
            s.add_argument("--out", type=Path, help="write report here (must be outside --store)")
            s.add_argument("--summary", action="store_true", help="print a compact per-arm table to stderr")
            s.add_argument("--require-publishable", action="store_true")
        else:
            s.add_argument("--report", type=Path, required=True)
    sub.add_parser("definitions")
    args = p.parse_args(argv)

    if args.cmd == "definitions":
        sys.stdout.write(_dump(definitions_payload()))
        return 0
    try:
        store = load_store(args.store)
        truth = ApprovedTruthSnapshot(args.cases_dir)
    except (StoreInvalid, TruthNotApproved, OSError, ValueError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    report = compute(store, truth)

    if args.cmd == "verify":
        stored = json.loads(args.report.read_text("utf-8"))
        problems = verify_report(stored, report)
        print(json.dumps({"ok": not problems, "report_digest": report["report_digest"], "problems": problems},
                         sort_keys=True, ensure_ascii=False))
        return 1 if problems else 0

    text = _dump(report)
    if args.out:
        out = args.out.resolve()
        if Path(args.store).resolve() in out.parents:
            print("INVALID: --out must not be inside the immutable store", file=sys.stderr)
            return 2
        out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    if args.summary:
        print(f"report_digest {report['report_digest']}  publishable={report['publishable']}", file=sys.stderr)
        for b in report["publication_blockers"]:
            print(f"  blocker: {b}", file=sys.stderr)
        for m in report["metrics"]:
            print(f"  {m['arm_id']:<18} {m['metric_id']:<28} value={m['value']} "
                  f"n={m['numerator']}/{m['denominator']} scored={m['scored']}/{m['expected_pairs']} "
                  f"verdict={m['verdict']}", file=sys.stderr)
    return 3 if args.require_publishable and not report["publishable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
