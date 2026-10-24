"""Leak gate for the judgement guide (P12 / P13).

The guide is built from run records only, but it is indexed into RAG and later used on new cases (P13). Before
indexing it must contain no answer text of any case catalog it will be evaluated against:

- every truth string (cause / expected_response / decoy_signal, >= MIN_SCAN_LEN chars) of each catalog, verbatim;
- no hidden answer KEY names written as data (e.g. "expected_response:"), which would mean a label block was pasted;
- for a "held-out" catalog (the new P13 cases), not even its case ids may appear, since the guide predates them.

Values are never echoed, only a short hash, so the check itself cannot leak.

  python -m evaluation.guide_leak_check docs/reference/liveprobe-judgement-guide.md \
      --catalog chaos/cases --held-out chaos/cases-new
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

from evaluation.harness.cases import HIDDEN_KEYS, Catalog


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def problems(text: str, catalogs: list[Path], held_out: list[Path]) -> list[str]:
    out: list[str] = []
    body = _norm(text)
    for d in [*catalogs, *held_out]:
        cat = Catalog(d, pattern="*.yaml")     # any case naming (new P13 cases are not F*)
        for secret in sorted(cat._secrets):            # noqa: SLF001 - the gate is the one consumer of the scan set
            if _norm(secret) in body:
                out.append(f"{d.name}: truth string present (sha256 {hashlib.sha256(secret.encode()).hexdigest()[:12]})")
    for key in HIDDEN_KEYS:
        if re.search(rf"(^|[\s\"'{{,]){re.escape(key)}\s*[:=]", text, re.M):
            out.append(f"hidden answer key written as data: {key}")
    for d in held_out:
        for cid in Catalog(d, pattern="*.yaml").case_ids:
            if re.search(rf"\b{re.escape(cid)}\b", text):
                out.append(f"held-out case id referenced: {cid}")
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("guide", type=Path)
    p.add_argument("--catalog", type=Path, action="append", default=[])
    p.add_argument("--held-out", type=Path, action="append", default=[])
    a = p.parse_args(argv)
    found = problems(a.guide.read_text("utf-8"), a.catalog, a.held_out)
    for f in found:
        print("LEAK:", f)
    sha = hashlib.sha256(a.guide.read_bytes()).hexdigest()
    print(("FAIL" if found else "PASS"), f"guide sha256={sha}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
