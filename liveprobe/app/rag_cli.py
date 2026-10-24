"""Offline RAG ingest/query CLI; run from the baked image (no external network).

Examples:
  python -m app.rag_cli ingest              # runbooks + licensed excerpts + sanitized closed incidents
  python -m app.rag_cli ingest --runbooks-only
  python -m app.rag_cli query "OOMKilled memory limit" --kind runbook

The application startup also ingests runbooks and the curated corpus, then polls closed incidents.
This CLI is useful for explicit retries or for an isolated database smoke. DB migrations run on startup,
not automatically here: run `alembic upgrade head` / `app.db.run_migrations(url)` first.
"""
from __future__ import annotations

import argparse
import json

from . import db
from .config import get_settings
from .rag import FastEmbedder, search
from pathlib import Path

from .rag_ingest import (closed_incident_documents, index_documents, load_public_documents, load_reference_guides,
                         load_runbook_documents)

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference"


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--runbooks-only", action="store_true")
    guide = sub.add_parser("ingest-guide", help="index docs/reference judgement guides approved by the leak gate")
    guide.add_argument("--dir", default=str(REFERENCE_DIR))
    query = sub.add_parser("query")
    query.add_argument("query")
    query.add_argument("--kind", choices=("runbook", "public_doc", "past_incident", "reference_guide"), action="append")
    query.add_argument("--k", type=int, default=3)
    args = p.parse_args()
    settings = get_settings()
    db.init_db(settings.sqlalchemy_url())
    emb = FastEmbedder(settings.embedding_model, settings.embedding_cache_dir)
    if args.command == "ingest":
        docs = load_runbook_documents(settings.runbooks_dir)
        if not args.runbooks_only:
            docs += load_public_documents()
        with db.session() as s:
            if not args.runbooks_only:
                docs += closed_incident_documents(s)
            result = index_documents(s, emb, docs)
        print(json.dumps(result.__dict__, sort_keys=True))
    elif args.command == "ingest-guide":
        approved = json.loads((Path(args.dir) / "APPROVED.json").read_text())["guides"]
        docs = load_reference_guides(args.dir, approved)
        with db.session() as s:
            result = index_documents(s, emb, docs)
        print(json.dumps({**result.__dict__, "guides": {d.key: d.version_hash for d in docs}}, sort_keys=True))
    else:
        with db.session() as s:
            hits = search(s, emb, args.query, k=args.k, kinds=args.kind)
        print(json.dumps([{"doc": h.doc, "source": h.source, "source_uri": h.source_uri,
                           "license": h.license, "locator": h.locator, "score": h.score,
                           "hybrid_score": h.hybrid_score} for h in hits], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
