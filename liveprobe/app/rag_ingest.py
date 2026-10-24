"""Curated document ingestion into pgvector with fail-closed provenance and atomic per-document refresh.

Callers own the transaction. A per-doc PostgreSQL transaction advisory lock serializes concurrent ingests.
Superseded chunks are retired, not deleted, so existing citation foreign keys remain valid. Search excludes retired
chunks. No web access is used by this module; public excerpt files are checked against corpus/manifest.yaml.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .models import RagChunk, RagDocument
from .rag import Chunk, Embedder, chunk_markdown, front_matter
from .redact import redact

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "corpus"
SUPPORTED_LICENSES = {"internal", "CC-BY-4.0", "CC-BY-SA-4.0"}
_HEX = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class Document:
    key: str
    title: str
    kind: str
    source: str
    source_uri: str
    license: str
    text: str
    license_url: str | None = None
    attribution: str | None = None
    retrieved_at: datetime | None = None
    incident_id: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    expected_hash: str | None = None

    @property
    def version_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


@dataclass
class IngestResult:
    documents: int = 0
    new_chunks: int = 0
    retired_chunks: int = 0
    skipped: int = 0


def _validate(d: Document) -> None:
    if d.kind not in {"runbook", "public_doc", "past_incident", "reference_guide"}:
        raise ValueError(f"unsupported document kind: {d.kind}")
    if d.license not in SUPPORTED_LICENSES:
        raise ValueError(f"unsupported or unknown reuse license: {d.license}")
    if not d.key or len(d.key) > 255 or not d.text.strip():
        raise ValueError("invalid document key/text")
    if not d.source_uri or d.source_uri.startswith("http:"):
        raise ValueError("missing/unsafe source URI")
    if d.kind == "public_doc":
        if d.license == "internal" or not d.source_uri.startswith("https://") or not d.attribution:
            raise ValueError("public document requires licensed https source + attribution")
        if not d.retrieved_at or not d.expected_hash or d.version_hash != d.expected_hash:
            raise ValueError("public document requires a verified content hash and retrieval time")
    if d.kind == "past_incident" and (d.incident_id is None or d.license != "internal"):
        raise ValueError("incident requires incident id and internal licence")


def load_runbook_documents(directory: str) -> list[Document]:
    out = []
    for p in sorted(Path(directory).glob("*.md")):
        raw = p.read_text()
        meta, _, _ = front_matter(raw)
        if meta.get("source", "internal-runbook") != "internal-runbook":
            raise ValueError(f"unexpected runbook source: {p.name}")
        out.append(Document(key=p.name, title=str(meta.get("title", p.stem)), kind="runbook",
                            source="internal-runbook", source_uri=f"repo://liveprobe/runbooks/{p.name}",
                            license="internal", text=raw, meta={"applies_to": meta.get("applies_to", [])}))
    return out


def load_reference_guides(directory: str, expected_sha256: dict[str, str] | None = None) -> list[Document]:
    """Judgement guides built from run records (docs/reference). A guide is indexed only at the exact version that
    passed the leak gate (evaluation/guide_leak_check.py): its sha256 must be given and must match."""
    out = []
    for p in sorted(Path(directory).glob("*.md")):
        raw = p.read_text()
        meta, _, _ = front_matter(raw)
        if meta.get("source_type") != "reference_guide":
            continue
        want = (expected_sha256 or {}).get(p.name)
        if not want or hashlib.sha256(raw.encode()).hexdigest() != want:
            raise ValueError(f"reference guide {p.name}: version not approved by the leak gate (sha256 mismatch/missing)")
        out.append(Document(key=p.name, title=str(meta.get("title", p.stem)), kind="reference_guide",
                            source="liveprobe-reference-guide", source_uri=f"repo://docs/reference/{p.name}",
                            license="internal", text=raw, expected_hash=want,
                            meta={"version": meta.get("version"), "built_from": meta.get("built_from")}))
    return out


def load_public_documents(manifest_path: Path = CORPUS / "manifest.yaml") -> list[Document]:
    manifest = yaml.safe_load(manifest_path.read_text())
    if manifest.get("version") != 1:
        raise ValueError("unknown corpus manifest version")
    out = []
    for e in manifest["public"]:
        p = manifest_path.parent / "public" / e["doc"]
        if not p.is_file() or p.name != e["doc"]:
            raise ValueError(f"missing/unsafe public document path: {e['doc']}")
        if e["license"] not in manifest["allowed_licenses"] or e["license"] not in SUPPORTED_LICENSES:
            raise ValueError(f"unapproved licence: {e['doc']}")
        raw = p.read_text()
        meta, body, _ = front_matter(raw)
        if meta.get("license") != e["license"] or meta.get("attribution") != e["attribution"]:
            raise ValueError(f"licence/attribution mismatch: {e['doc']}")
        if meta.get("source_uri") != e["page_url"] or meta.get("upstream") != (
            (f"{e['upstream_repo']}/wiki/Special:PermanentLink/{e['upstream_commit']}"
             if e.get("upstream_type") == "mediawiki" else
             f"{e['upstream_repo']}/blob/{e['upstream_commit']}/{e['upstream_path']}")
            + f"#L{e['upstream_lines'][0]}-L{e['upstream_lines'][1]}"):
            raise ValueError(f"source location mismatch: {e['doc']}")
        # The excerpt (including front matter) must match its pinned upstream version exactly;
        # only curated/verified files with their hash in the manifest may be indexed.
        if not e.get("excerpt_sha256") or hashlib.sha256(raw.encode()).hexdigest() != e["excerpt_sha256"]:
            raise ValueError(f"excerpt hash mismatch: {e['doc']}")
        d = Document(key=e["doc"], title=e["title"], kind="public_doc", source=e["source"],
                     source_uri=meta["upstream"], license=e["license"],
                     license_url=manifest["allowed_licenses"][e["license"]], attribution=e["attribution"],
                     retrieved_at=datetime.fromisoformat(e["retrieved_at"].replace("Z", "+00:00")),
                     text=raw, expected_hash=e["excerpt_sha256"],
                     meta={"page_url": e["page_url"], "family": e["family"], "changes": e["changes"],
                           "upstream_sha256": e["upstream_sha256"], "upstream_lines": e["upstream_lines"],
                           "upstream_line_map": meta["upstream_line_map"]})
        _validate(d)
        out.append(d)
    if {p.name for p in (manifest_path.parent / "public").glob("*.md")} != {e.key for e in out}:
        raise ValueError("unmanifested public file")
    return out


def _provenance(d: Document) -> dict[str, Any]:
    return {"source_uri": d.source_uri, "license": d.license, "license_url": d.license_url,
            "attribution": d.attribution, "version_hash": d.version_hash,
            "retrieved_at": d.retrieved_at.isoformat() if d.retrieved_at else None,
            "title": d.title, "kind": d.kind, "source": d.source}


def _content_hash(d: Document, chunk: Chunk) -> str:
    # Version and licence are part of the identity: historical citation provenance must not change
    # even when a document is replaced with identical wording at a different source/version.
    return hashlib.sha256(
        f"{d.key}\0{d.version_hash}\0{d.source_uri}\0{d.license}\0{d.license_url}\0"
        f"{d.attribution}\0{d.retrieved_at}\0{chunk.chunk_ix}\0{chunk.content}".encode()
    ).hexdigest()


def _locator(d: Document, c: Chunk) -> str:
    if d.kind == "public_doc":
        _, _, off = front_matter(d.text)
        first_body_line = d.text.count("\n", 0, off) + 1
        line_map = d.meta["upstream_line_map"]
        start_ix, end_ix = c.line_start - first_body_line, c.line_end - first_body_line
        if start_ix < 0 or end_ix >= len(line_map):
            raise ValueError("invalid public chunk line map")
        return (f"{d.source_uri.split('#', 1)[0]}#L{line_map[start_ix]}-L{line_map[end_ix]}"
                f" (excerpt {d.key} lines {c.line_start}-{c.line_end})")
    return f"{d.source_uri}#L{c.line_start}-L{c.line_end}"


def index_documents(s: Session, embedder: Embedder, documents: Iterable[Document]) -> IngestResult:
    """Index each document as a single replacement transaction (caller must commit/rollback the session)."""
    result = IngestResult()
    for d in documents:
        _validate(d)
        s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": "rag:" + d.key})
        stored = s.scalar(select(RagDocument).where(RagDocument.doc_key == d.key))
        chunks = chunk_markdown(d.key, d.text)
        if not chunks:
            raise ValueError(f"document has no chunks: {d.key}")
        # Legacy chunks were indexed before provenance existed. Match per document and retire if modified.
        current = list(s.scalars(select(RagChunk).where(RagChunk.doc == d.key, RagChunk.retired_at.is_(None))
                                 .order_by(RagChunk.chunk_ix)))
        old_by_ix = {c.chunk_ix: c for c in current}
        same = (stored and stored.version_hash == d.version_hash and stored.source_uri == d.source_uri
                and stored.license == d.license and stored.license_url == d.license_url
                and stored.attribution == d.attribution and stored.retrieved_at == d.retrieved_at
                and len(chunks) == len(current))
        if same and all(old_by_ix.get(c.chunk_ix) and old_by_ix[c.chunk_ix].content == c.content
                        for c in chunks):
            result.skipped += 1
            continue
        if not stored:
            stored = RagDocument(doc_key=d.key, kind=d.kind, source=d.source, title=d.title,
                                 source_uri=d.source_uri, license=d.license, license_url=d.license_url,
                                 attribution=d.attribution, version_hash=d.version_hash,
                                 retrieved_at=d.retrieved_at, incident_id=d.incident_id,
                                 meta=d.meta, ingested_at=datetime.now(timezone.utc))
            s.add(stored)
            s.flush()
        else:
            # Preserve the previous version's metadata on all rows before updating the mutable
            # document pointer. Legacy rows without any metadata cannot acquire an invented source.
            for old in current:
                if old.provenance is None and old.document_id == stored.id:
                    old.provenance = {"source_uri": stored.source_uri, "license": stored.license,
                                      "license_url": stored.license_url, "attribution": stored.attribution,
                                      "version_hash": stored.version_hash, "title": stored.title,
                                      "kind": stored.kind, "source": stored.source,
                                      "retrieved_at": stored.retrieved_at.isoformat() if stored.retrieved_at else None}
            for key in ("kind", "source", "title", "source_uri", "license", "license_url", "attribution",
                        "version_hash", "retrieved_at", "incident_id", "meta"):
                setattr(stored, key, getattr(d, key))
            stored.ingested_at = datetime.now(timezone.utc)
        # Reuse exactly-identical chunks (preserve existing citations), retire only changed/removed ones.
        todo: list[tuple[Chunk, str]] = []
        for c in chunks:
            old = old_by_ix.pop(c.chunk_ix, None)
            if old and old.content == c.content and old.provenance == _provenance(d):
                old.document_id = stored.id
                old.section, old.char_start, old.char_end = c.section, c.char_start, c.char_end
                old.line_start, old.line_end, old.locator = c.line_start, c.line_end, _locator(d, c)
                continue
            if old:
                old.retired_at = datetime.now(timezone.utc)
                result.retired_chunks += 1
            todo.append((c, _content_hash(d, c)))
        for old in old_by_ix.values():
            old.retired_at = datetime.now(timezone.utc)
            result.retired_chunks += 1
        # content_hash is globally unique, even for retired chunks; a refresh may produce an identical
        # earlier version. Restore it instead of inserting a duplicate, retaining historical citations.
        remaining: list[tuple[Chunk, str]] = []
        for c, h in todo:
            row = s.scalar(select(RagChunk).where(RagChunk.content_hash == h))
            if row and row.doc == d.key and row.retired_at is not None:
                row.retired_at = None
                row.document_id = stored.id
                row.section, row.char_start, row.char_end = c.section, c.char_start, c.char_end
                row.line_start, row.line_end, row.locator = c.line_start, c.line_end, _locator(d, c)
                # The snapshot belongs to the original version; do not mutate it on reactivation.
            else:
                remaining.append((c, h))
        todo = remaining
        if todo:
            vecs = embedder.embed([c.content for c, _ in todo])
            if len(vecs) != len(todo):
                raise ValueError("embedder returned wrong number of vectors")
            for (c, h), v in zip(todo, vecs):
                s.add(RagChunk(doc=d.key, source=d.source, chunk_ix=c.chunk_ix, content=c.content,
                               content_hash=h, embedding=v, document_id=stored.id,
                               section=c.section, char_start=c.char_start, char_end=c.char_end,
                               line_start=c.line_start, line_end=c.line_end, locator=_locator(d, c),
                               provenance=_provenance(d)))
            result.new_chunks += len(todo)
        s.flush()
        result.documents += 1
    return result


_SUMMARY_WORDS = frozenset({
    "pod", "container", "workload", "oomkilled", "imagepullbackoff", "errimagepull", "crashloopbackoff",
    "after", "rollout", "memory", "limit", "exceeded", "service", "has", "no", "endpoints", "selector",
    "mismatch", "upstream", "downstream", "timeout", "latency", "connection", "refused", "5xx", "errors",
    "high", "pool", "exhaustion", "restart", "failed", "failure", "error", "and", "the", "a", "is",
    "due", "to", "of", "in", "with", "caused", "by", "pressure", "bad", "image", "tag", "request",
    "requests", "slow", "unavailable", "dependency", "readiness", "not", "ready", "pods",
})


def _safe_summary(text_value: str | None) -> str | None:
    """Strict allowlist of non-sensitive diagnostic summaries only, never raw logs/evidence/LLM output.

    Every word must be known diagnostic vocabulary, otherwise reject the entire summary. This loses some
    safe summaries deliberately; redaction alone cannot guarantee PII-free incident text.
    """
    if not text_value:
        return None
    value = redact(text_value)
    if value is None or value != text_value or len(value) > 500 or not re.fullmatch(r"[A-Za-z0-9 ,.;()\-]+", value):
        return None
    if any(w.lower() not in _SUMMARY_WORDS for w in re.findall(r"[A-Za-z0-9]+", value)):
        return None
    if not re.search(
        r"imagepullbackoff|errimagepull|crashloopbackoff|oomkilled|no endpoints|selector mismatch|5xx|latency|timeout|connection refused",
        value, flags=re.I,
    ):
        return None
    return value


def closed_incident_documents(s: Session) -> list[Document]:
    """Only explicitly safe, final incidents. Summaries failing strict screening are skipped; never read evidence.

    Caller runs this after closure (e.g. periodic background job). Existing RAG citations are not copied in,
    preventing recursive loops. A changed summary gets the same stable doc key and an atomic refresh.
    """
    from .models import Incident
    out = []
    for inc in s.scalars(select(Incident).where(Incident.status == "resolved", Incident.closed_at.is_not(None),
                                                ~Incident.fingerprint.like("eval/%"))):
        summary = _safe_summary(inc.summary)
        if summary is None:
            continue
        text_value = f"# Resolved incident {inc.id}\n\n## Sanitized summary\n{summary}\n"
        out.append(Document(key=f"incident-{inc.id}.md", title=f"Resolved incident {inc.id}",
                            kind="past_incident", source="closed-incident", license="internal",
                            source_uri=f"incident://{inc.id}", incident_id=inc.id, text=text_value,
                            meta={"closed_at": inc.closed_at.isoformat()}))
    return out
