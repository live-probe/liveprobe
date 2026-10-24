#!/usr/bin/env python3
"""KAN-44 secret scan (KAN-92 criterion). Prints counts/classifications only, never values.

Sources: pod logs (current + previous) in the three LiveProbe namespaces, a data-only
dump of every DB table, and optional saved API response files given as arguments.

Classification per pattern hit:
  identifier_fragment  - 'access_token'/'refresh_token' embedded in a longer identifier (accepted, KAN-92)
  key_only             - key name without a real value (e.g. prose, empty/short/redacted value)
  token_value          - an actual credential-looking value (FAIL)
Exit code 1 if any token_value is found.
"""
import re
import subprocess
import sys
from collections import Counter

CTX = ["kubectl", "--context", "rancher-desktop"]
NAMESPACES = ("liveprobe-system", "liveprobe-observability", "liveprobe-target")
KEY = re.compile(r"refresh_token|access_token|sk-", re.I)
# Actual-value detectors.
VALUE_PATTERNS = [
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"(?:refresh_token|access_token)[\"']?\s*[:=]\s*[\"']?(?!\*|<|REDACTED|null|None|\"\")[A-Za-z0-9_\-\.~+/]{16,}", re.I),
]


def classify(text):
    counts = Counter()
    value_spans = []
    for pat in VALUE_PATTERNS:
        for m in pat.finditer(text):
            value_spans.append((m.start(), m.end()))
    counts["token_value"] = len(value_spans)
    for m in KEY.finditer(text):
        if any(s <= m.start() < e for s, e in value_spans):
            continue
        before = text[m.start() - 1] if m.start() else ""
        after = text[m.end()] if m.end() < len(text) else ""
        word = m.group(0).lower()
        if word != "sk-" and (re.match(r"[A-Za-z0-9_]", before) or re.match(r"[A-Za-z0-9_]", after)):
            counts["identifier_fragment"] += 1
        elif word == "sk-" and re.match(r"[A-Za-z0-9]", before):
            counts["identifier_fragment"] += 1  # e.g. 'task-', 'disk-'
        else:
            counts["key_only"] += 1
    return counts


def report(label, text, total):
    c = classify(text)
    total.update(c)
    if sum(c.values()):
        print(f"{label}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items()) if v))


def run(args):
    return subprocess.run(CTX + args, capture_output=True, text=True)


def main():
    total = Counter()
    sources = 0
    for ns in NAMESPACES:
        pods = run(["-n", ns, "get", "pods", "-o", "jsonpath={.items[*].metadata.name}"]).stdout.split()
        for pod in pods:
            for prev in (False, True):
                args = ["-n", ns, "logs", pod, "--all-containers=true"] + (["--previous"] if prev else [])
                out = run(args)
                if out.returncode != 0:
                    continue
                sources += 1
                report(f"log {ns}/{pod}{' (previous)' if prev else ''}", out.stdout, total)
    tables = run(["-n", "liveprobe-system", "exec", "postgres-0", "--", "sh", "-c",
                  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc '
                  "\"select table_name from information_schema.tables where table_schema='public'\""]).stdout.split()
    for table in tables:
        out = run(["-n", "liveprobe-system", "exec", "postgres-0", "--", "sh", "-c",
                   f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select t::text from {table} t"'])
        if out.returncode != 0:
            print(f"db {table}: query failed (rc={out.returncode})")
            continue
        sources += 1
        rows = len(out.stdout.splitlines())
        print(f"db {table}: rows={rows}")
        report(f"db {table}", out.stdout, total)
    for path in sys.argv[1:]:
        sources += 1
        with open(path, encoding="utf-8", errors="replace") as fh:
            report(f"api {path}", fh.read(), total)
    print(f"SUMMARY sources={sources} " + " ".join(f"{k}={total.get(k, 0)}" for k in
                                                   ("token_value", "key_only", "identifier_fragment")))
    return 1 if total.get("token_value") else 0


if __name__ == "__main__":
    sys.exit(main())
