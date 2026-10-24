"""Write or verify the public-document excerpts listed in corpus/manifest.yaml.

Only the pinned upstream files listed in the manifest are fetched (no crawling). Default mode is --verify:
re-download each pinned upstream file, check its sha256 against the manifest, and check the committed excerpt
body is exactly the declared line range (minus Hugo shortcode lines). --write regenerates the excerpt files.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.request
import urllib.parse
import json
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SHORTCODE = re.compile(r"^\s*\{\{[<%].*[%>]\}\}\s*$")


def raw_url(e: dict) -> str:
    if e.get("upstream_type") == "mediawiki":
        return "https://wikitech.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query", "prop": "revisions", "revids": e["upstream_commit"],
            "rvprop": "ids|timestamp|content", "rvslots": "main", "format": "json",
        })
    repo = e["upstream_repo"].removeprefix("https://github.com/")
    return f"https://raw.githubusercontent.com/{repo}/{e['upstream_commit']}/{e['upstream_path']}"


def upstream_text(e: dict, data: bytes) -> str:
    if e.get("upstream_type") == "mediawiki":
        revision = next(iter(json.loads(data)["query"]["pages"].values()))["revisions"][0]
        if revision["revid"] != int(e["upstream_commit"]) or revision["timestamp"] != e["upstream_timestamp"]:
            raise ValueError("MediaWiki revision mismatch")
        return revision["slots"]["main"]["*"]
    return data.decode()


def upstream_locator(e: dict) -> str:
    if e.get("upstream_type") == "mediawiki":
        return (f"{e['upstream_repo']}/wiki/Special:PermanentLink/{e['upstream_commit']}"
                f"#L{e['upstream_lines'][0]}-L{e['upstream_lines'][1]}")
    return (f"{e['upstream_repo']}/blob/{e['upstream_commit']}/{e['upstream_path']}"
            f"#L{e['upstream_lines'][0]}-L{e['upstream_lines'][1]}")


def excerpt_body(upstream: str, lines: list[int]) -> tuple[str, list[int]]:
    a, b = lines
    picked = [(num, value) for num, value in enumerate(upstream.splitlines()[a - 1:b], a)
              if not SHORTCODE.match(value)]
    # strip exactly the same outer whitespace we record in the map; do not alter the text of kept lines
    while picked and not picked[0][1].strip():
        picked.pop(0)
    while picked and not picked[-1][1].strip():
        picked.pop()
    return "\n".join(value for _, value in picked) + "\n", [num for num, _ in picked]


def render(e: dict, body: str, line_map: list[int]) -> str:
    fm = {
        "title": e["title"], "source": e["source"], "kind": "public_doc", "family": e["family"],
        "source_uri": e["page_url"],
        "upstream": upstream_locator(e),
        "upstream_line_map": line_map,
        "license": e["license"], "attribution": e["attribution"], "retrieved_at": e["retrieved_at"],
        "changes": e["changes"],
    }
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    manifest = yaml.safe_load((HERE / "manifest.yaml").read_text())
    allowed = manifest["allowed_licenses"]
    out_dir = HERE / "public"
    out_dir.mkdir(exist_ok=True)
    failed = 0
    cache: dict[str, bytes] = {}
    for e in manifest["public"]:
        if e["license"] not in allowed:
            print(f"FAIL {e['doc']}: licence {e['license']} not allowed"); failed += 1; continue
        url = raw_url(e)
        if url not in cache:
            request = urllib.request.Request(url, headers={"User-Agent": "LiveProbe-curator/1.0 (source verification)"})
            with urllib.request.urlopen(request, timeout=30) as r:  # noqa: S310 (pinned https URL from manifest)
                cache[url] = r.read()
        data = cache[url]
        try:
            upstream = upstream_text(e, data)
        except (KeyError, ValueError, IndexError) as exc:
            print(f"FAIL {e['doc']}: invalid pinned upstream: {exc}"); failed += 1; continue
        sha = hashlib.sha256(upstream.encode()).hexdigest()
        if sha != e["upstream_sha256"]:
            print(f"FAIL {e['doc']}: upstream sha256 {sha} != manifest"); failed += 1; continue
        body, line_map = excerpt_body(upstream, e["upstream_lines"])
        text = render(e, body, line_map)
        path = out_dir / e["doc"]
        if args.write:
            path.write_text(text)
            print(f"wrote {path.relative_to(HERE)} ({len(text)} bytes)")
        elif not path.exists() or path.read_text() != text:
            print(f"FAIL {e['doc']}: excerpt differs from pinned upstream"); failed += 1
        else:
            print(f"OK   {e['doc']} upstream_sha256={sha[:16]} lines={e['upstream_lines']} licence={e['license']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
