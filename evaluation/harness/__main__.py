"""CLI: python3 -m evaluation.harness (--dry-run | --real --approved-by NAME) --store DIR --cost-ceiling-usd N

--dry-run : fake runner + fake fault worker (no LLM, no cluster).
--real    : in-cluster Codex runner + Kubernetes-API fault worker. Requires the approved protocol
            (docs/plans/revamp/P10-evaluation-protocol.md) hash to match and every case to be one of the
            Kubernetes-API-owned cases (bad_image, selector_mismatch).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .cases import CASES_DIR, Catalog
from .runners import FakeFaultWorker, FakeRunner
from .scheduler import NotAuthorized, Stop, run_campaign


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m evaluation.harness", description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="fake runner + fake fault worker (no LLM, no cluster)")
    p.add_argument("--real", action="store_true", help="real in-cluster runner + Kubernetes fault worker")
    p.add_argument("--approved-by", help="who approved the protocol (recorded in the manifest)")
    p.add_argument("--arm", action="append", dest="arms", help="limit to arm id (repeatable)")
    p.add_argument("--store", type=Path, required=True, help="progress directory (created 0700; resumable)")
    p.add_argument("--cost-ceiling-usd", type=float, required=True, help="mandatory; campaign STOPs before exceeding")
    p.add_argument("--cases-dir", type=Path, default=CASES_DIR)
    p.add_argument("--case", action="append", dest="cases", help="limit to case id (repeatable)")
    p.add_argument("--max-pairs", type=int, help="stop after N executed pairs (simulate interruption)")
    p.add_argument("--p13", action="store_true", help="P13 comparison: held-out cases with adaptive no-guide vs guide arms")
    args = p.parse_args(argv)
    if args.dry_run == args.real:
        print("refused: choose exactly one of --dry-run or --real", file=sys.stderr)
        return 2
    if args.dry_run:
        try:
            out = run_campaign(args.store, Catalog(args.cases_dir), FakeRunner(), FakeFaultWorker(),
                               cost_ceiling_usd=args.cost_ceiling_usd, dry_run=True, case_ids=args.cases,
                               max_pairs=args.max_pairs, arm_ids=args.arms)
        except (Stop, NotAuthorized, ValueError) as exc:
            print(f"STOP: {exc}", file=sys.stderr)
            return 3
    else:
        import hashlib
        from .cases import REPO
        from .arms import P13_ARMS
        from .real import CodexInClusterRunner, K8sFaultWorker
        if args.p13 and Path(args.cases_dir).name != "cases-new":
            print("refused: --p13 runs only on the held-out catalog chaos/cases-new", file=sys.stderr)
            return 2
        from .scheduler import PROTOCOL_FILE
        if not args.approved_by:
            print("refused: --real needs --approved-by", file=sys.stderr)
            return 2
        auth = {"approved_by": args.approved_by,
                "protocol_sha256": hashlib.sha256((REPO / PROTOCOL_FILE).read_bytes()).hexdigest()}
        runner, worker = CodexInClusterRunner(), K8sFaultWorker(cases_dir=args.cases_dir)
        try:
            runner.lock(4 * 3600)       # held for the whole campaign; also expires on its own if this process dies
            out = run_campaign(args.store, Catalog(args.cases_dir), runner, worker,
                               cost_ceiling_usd=args.cost_ceiling_usd, dry_run=False, case_ids=args.cases,
                               max_pairs=args.max_pairs, authorization=auth, arm_ids=args.arms,
                               **({"arms": P13_ARMS} if args.p13 else {}))
        except (Stop, NotAuthorized, ValueError) as exc:
            print(f"STOP: {exc}", file=sys.stderr)
            return 3
        finally:
            try:
                runner.unlock()
            except Exception:  # noqa: BLE001
                pass
    print(json.dumps({"stopped": out.stopped, "reason": out.reason, **out.tally}, sort_keys=True))
    return 1 if out.stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
