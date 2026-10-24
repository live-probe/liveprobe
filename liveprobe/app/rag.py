"""Lightweight RAG: fastembed (all-MiniLM-L6-v2, 384-d, local/offline) + pgvector + Postgres FTS hybrid search.

Every chunk carries its provenance (document, source URI, licence) and its position in the original file
(section heading, character span and line span), so a citation can always be traced back to the source text.
Ingestion of documents (runbooks, licensed public excerpts, closed incidents) lives in `rag_ingest.py`.

Ranking policy (hybrid, see tests/test_rag.py):
  1. candidate set = top `CANDIDATES` active chunks by cosine distance  UNION  top `CANDIDATES` by ts_rank_cd
     of an OR-query of the query's words (parameterised, words restricted to [a-z0-9]);
  2. fused score = sum over lists of 1 / (RRF_K + rank)  (reciprocal rank fusion, rank starts at 1);
  3. ties broken by smaller cosine distance, then smaller chunk id (deterministic);
  4. diversity: at most one chunk per document; at most `MAX_PER_KIND[kind]` hits of one kind
     (past incidents are capped at 1 so repeated similar incidents cannot push runbooks out of top-k).
`Hit.score` stays the cosine similarity (MVP-0 citation contract); `hybrid_score` is the fused score.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml
from sqlalchemy import bindparam, func, literal_column, select, text
from sqlalchemy.orm import Session

from .models import RagChunk, RagDocument

log = logging.getLogger(__name__)

CANDIDATES = 30
RRF_K = 60
MAX_PER_KIND = {"past_incident": 1}
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "with", "by", "at",
         "be", "it", "as", "from", "this", "that", "not", "no"}


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    def __init__(self, model_name: str, cache_dir: str | None = None):
        from fastembed import TextEmbedding
        self._model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            return [v.tolist() for v in self._model.embed(texts)]


@dataclass
class Chunk:
    doc: str
    source: str
    chunk_ix: int
    content: str
    section: str | None = None
    char_start: int | None = None   # offsets into the original file text (front matter included)
    char_end: int | None = None
    line_start: int | None = None   # 1-based, inclusive
    line_end: int | None = None


@dataclass
class Hit:
    chunk_id: int
    doc: str
    source: str
    chunk_ix: int
    content: str
    score: float                         # cosine similarity (MVP-0 contract)
    hybrid_score: float = 0.0
    vector_rank: int | None = None
    lexical_rank: int | None = None
    section: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    locator: str | None = None
    kind: str | None = None
    title: str | None = None
    source_uri: str | None = None
    license: str | None = None
    attribution: str | None = None

    def provenance(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("kind", "title", "source_uri", "license", "attribution", "section",
                                               "locator", "line_start", "line_end", "char_start", "char_end")}


def front_matter(text: str) -> tuple[dict[str, Any], str, int]:
    """Return (meta, body, body_offset). YAML front matter; falls back to `key: value` lines."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            raw = text[3:end]
            try:
                meta = yaml.safe_load(raw) or {}
                if not isinstance(meta, dict):
                    meta = {}
            except yaml.YAMLError:
                meta = {}
                for line in raw.strip().splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip()
            off = end + 4
            while off < len(text) and text[off] in "\r\n \t":
                off += 1
            return meta, text[off:].rstrip(), off
    stripped = text.lstrip()
    return {}, stripped.rstrip(), len(text) - len(stripped)


def _front_matter(text: str) -> tuple[dict[str, str], str]:  # backwards compatible helper
    meta, body, _ = front_matter(text)
    return {k: str(v) for k, v in meta.items()}, body


_PARA = re.compile(r"\S.*?(?=\n[ \t]*\n|\Z)", re.S)
_HEADING = re.compile(r"#{1,6}\s+(.*)")


def chunk_markdown(doc: str, text: str, size: int = 500) -> list[Chunk]:
    """Split by headings, pack paragraphs up to `size` chars; record section + char/line span in `text`."""
    meta, body, off = front_matter(text)
    source = str(meta.get("source", "internal-runbook"))
    title = str(meta.get("title", doc))
    out: list[Chunk] = []
    section: str | None = None
    buf: list[tuple[int, int]] = []   # spans (relative to body) packed into the current chunk
    buf_section: str | None = None

    def flush() -> None:
        if not buf:
            return
        a, b = buf[0][0], buf[-1][1]
        s, e = off + a, off + b
        out.append(Chunk(doc=doc, source=source, chunk_ix=len(out), content=f"[{title}]\n{body[a:b]}",
                         section=buf_section, char_start=s, char_end=e,
                         line_start=text.count("\n", 0, s) + 1, line_end=text.count("\n", 0, e) + 1))
        buf.clear()

    for m in _PARA.finditer(body):
        a, b = m.start(), m.end()
        para = body[a:b].rstrip()
        b = a + len(para)
        h = _HEADING.match(para)
        if h and para.startswith("##"):          # a new section starts a new chunk (as in MVP-0)
            flush()
            section = h.group(1).strip()
        elif h and section is None:
            section = h.group(1).strip()
        if buf and (b - buf[0][0]) > size:
            flush()
        if not buf:
            buf_section = section
        buf.append((a, b))
    flush()
    return out


def load_runbooks(directory: str) -> list[Chunk]:
    out: list[Chunk] = []
    for p in sorted(Path(directory).glob("*.md")):
        out.extend(chunk_markdown(p.name, p.read_text()))
    return out


def index_runbooks(s: Session, embedder: Embedder, directory: str) -> int:
    """MVP-0 entry point, now provenance-aware (idempotent per document). Returns number of new chunks."""
    from .rag_ingest import index_documents, load_runbook_documents
    return index_documents(s, embedder, load_runbook_documents(directory)).new_chunks


def lexical_query(query: str) -> str:
    """OR-query of distinct lowercase words; safe for to_tsquery (only [a-z0-9] tokens)."""
    words, seen = [], set()
    for w in _WORD.findall(query.lower()):
        if w in _STOP or len(w) < 2 or w in seen:
            continue
        seen.add(w)
        words.append(w)
    return " | ".join(words[:32])


def search(s: Session, embedder: Embedder, query: str, k: int = 3, *, kinds: list[str] | None = None,
           candidates: int = CANDIDATES, max_per_kind: dict[str, int] | None = None) -> list[Hit]:
    qv = embedder.embed([query])[0]
    max_per_kind = MAX_PER_KIND if max_per_kind is None else max_per_kind
    active = RagChunk.retired_at.is_(None)
    kind_col = func.coalesce(RagDocument.kind, literal_column("'runbook'"))
    base_filter = [active]
    if kinds:
        base_filter.append(kind_col.in_(kinds))

    dist = RagChunk.embedding.cosine_distance(qv)
    vec_rows = s.execute(
        select(RagChunk.id, dist.label("d"))
        .outerjoin(RagDocument, RagDocument.id == RagChunk.document_id)
        .where(*base_filter).order_by(dist, RagChunk.id).limit(candidates)).all()
    vec_rank = {r.id: i + 1 for i, r in enumerate(vec_rows)}

    lex_rank: dict[int, int] = {}
    lq = lexical_query(query)
    if lq:
        tsq = func.to_tsquery("english", bindparam("lq", lq))
        rank = func.ts_rank_cd(RagChunk.tsv, tsq)
        lex_rows = s.execute(
            select(RagChunk.id, rank.label("r"))
            .outerjoin(RagDocument, RagDocument.id == RagChunk.document_id)
            .where(*base_filter, RagChunk.tsv.op("@@")(tsq))
            .order_by(rank.desc(), RagChunk.id).limit(candidates)).all()
        lex_rank = {r.id: i + 1 for i, r in enumerate(lex_rows)}

    ids = set(vec_rank) | set(lex_rank)
    if not ids:
        return []
    rows = s.execute(
        select(RagChunk, RagDocument, dist.label("d"))
        .outerjoin(RagDocument, RagDocument.id == RagChunk.document_id)
        .where(RagChunk.id.in_(ids))).all()
    scored = []
    for ch, doc, d in rows:
        fused = sum(1.0 / (RRF_K + r[ch.id]) for r in (vec_rank, lex_rank) if ch.id in r)
        scored.append((fused, float(d), ch, doc))
    scored.sort(key=lambda t: (-t[0], t[1], t[2].id))

    hits: list[Hit] = []
    per_kind: dict[str, int] = {}
    seen_docs: set[str] = set()
    for fused, d, ch, doc in scored:
        kind = doc.kind if doc else "runbook"
        if ch.doc in seen_docs or (kind in max_per_kind and per_kind.get(kind, 0) >= max_per_kind[kind]):
            continue
        seen_docs.add(ch.doc)
        per_kind[kind] = per_kind.get(kind, 0) + 1
        prov = ch.provenance or {}
        hits.append(Hit(
            chunk_id=ch.id, doc=ch.doc, source=ch.source, chunk_ix=ch.chunk_ix, content=ch.content,
            score=round(1.0 - d, 4), hybrid_score=round(fused, 6),
            vector_rank=vec_rank.get(ch.id), lexical_rank=lex_rank.get(ch.id),
            section=ch.section, char_start=ch.char_start, char_end=ch.char_end,
            line_start=ch.line_start, line_end=ch.line_end, locator=ch.locator, kind=kind,
            title=prov.get("title") or (doc.title if doc else None),
            source_uri=prov.get("source_uri") or (doc.source_uri if doc and ch.document_id else None),
            license=prov.get("license") or (doc.license if doc and ch.document_id else None),
            attribution=prov.get("attribution") or (doc.attribution if doc and ch.document_id else None)))
        if len(hits) >= k:
            break
    return hits


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


class RAG:
    """Holds the embedder; search runs in a caller-provided session."""

    def __init__(self, embedder: Embedder):
        self.embedder = embedder

    def search(self, s: Session, query: str, k: int = 3, **kw) -> list[Hit]:
        return search(s, self.embedder, query, k, **kw)


__all__ = ["Chunk", "Embedder", "FastEmbedder", "Hit", "RAG", "chunk_markdown", "front_matter", "index_runbooks",
           "lexical_query", "load_runbooks", "search", "content_hash", "field", "text"]
