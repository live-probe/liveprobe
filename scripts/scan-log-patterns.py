#!/usr/bin/env python3
"""Print token-pattern counts and sanitized contexts only; never emit log values."""
import re
import subprocess

pattern = re.compile(r"refresh_token|access_token|sk-", re.I)
for namespace in ("liveprobe-system", "liveprobe-observability", "liveprobe-target"):
    pods = subprocess.check_output([
        "kubectl", "--context", "rancher-desktop", "-n", namespace,
        "get", "pods", "-o", "jsonpath={.items[*].metadata.name}",
    ], text=True).split()
    for pod in pods:
        output = subprocess.run([
            "kubectl", "--context", "rancher-desktop", "-n", namespace,
            "logs", pod, "--all-containers=true", "--tail=10000",
        ], capture_output=True, text=True).stdout
        matches = pattern.findall(output)
        if matches:
            print(namespace, pod, "count", len(matches), "kinds", sorted(set(m.lower() for m in matches)))
            for line in output.splitlines():
                for match in pattern.finditer(line):
                    # Report only structural properties, never any log field values.
                    prefix, suffix = line[:match.start()], line[match.end():]
                    keys = re.findall(r'\b(?:logger|msg|level|path|method|status|component)=', line)
                    print("  match structure:", {
                        "fixed_field_keys": sorted(set(keys)),
                        "quoted_key": bool(re.search(r'[\"\']$', prefix) and re.match(r'[\"\']\s*:', suffix)),
                        "assignment": bool(re.match(r'\s*[:=]', suffix)),
                        "identifier_fragment": bool(re.search(r'[A-Za-z_]$', prefix) or re.match(r'[A-Za-z_]', suffix)),
                        "url_query_key": bool(re.search(r'[?&]$', prefix) and re.match(r'=', suffix)),
                        "line_length": len(line),
                    })
