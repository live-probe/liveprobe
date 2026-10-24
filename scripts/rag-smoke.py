"""Run in liveprobe-api pod after the image has indexed runbooks."""
from app import db
from app.config import get_settings
from app.rag import FastEmbedder, search

settings = get_settings()
db.init_db(settings.sqlalchemy_url())
embedder = FastEmbedder(settings.embedding_model, settings.embedding_cache_dir)
with db.session() as s:
    for label, query, wanted in (
        ("family1", "inventory ImagePullBackOff ErrImagePull bad image tag rollout undo", "image-pull-crashloop.md"),
        ("family2", "inventory OOMKilled container memory limit restart count", "oomkilled.md"),
        ("family3", "order 5xx inventory latency timeout connection pool exhausted", "cascading-5xx.md"),
        ("family4", "inventory service selector mismatch no ready endpoints order 5xx", "service-endpoint-loss.md"),
    ):
        hits = search(s, embedder, query, 3)
        docs = [hit.doc for hit in hits]
        assert wanted in docs, (label, docs)
        assert all(h.source_uri and h.locator and h.license for h in hits), (label, docs)
        print(label, "top3:", docs, "source:", [hit.source for hit in hits],
              "locator:", [hit.locator for hit in hits])
