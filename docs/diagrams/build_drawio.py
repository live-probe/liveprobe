"""Assemble page XML files into one multi-page .drawio file (uncompressed pages, readable in git diffs).
Usage: python build_drawio.py OUT.drawio name1=page1.xml name2=page2.xml ...
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from xml.sax.saxutils import quoteattr


def main() -> None:
    out = Path(sys.argv[1])
    pages = []
    for arg in sys.argv[2:]:
        name, path = arg.split("=", 1)
        model = Path(path).read_text(encoding="utf-8").strip()
        if not model.startswith("<mxGraphModel"):
            raise SystemExit(f"{path}: not an mxGraphModel page")
        pid = uuid.uuid5(uuid.NAMESPACE_URL, f"liveprobe/{name}").hex[:20]
        pages.append(f"  <diagram name={quoteattr(name)} id={quoteattr(pid)}>\n    {model}\n  </diagram>")
    out.write_text('<mxfile host="app.diagrams.net" agent="LiveProbe docs/diagrams" type="device">\n'
                   + "\n".join(pages) + "\n</mxfile>\n", encoding="utf-8")
    print(f"wrote {out} ({len(pages)} pages)")


if __name__ == "__main__":
    main()
