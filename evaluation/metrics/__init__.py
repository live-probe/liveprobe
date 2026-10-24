"""KAN-137: recomputable KAN-59 evaluation metrics engine (no cost, host only).

Reads an immutable KAN-133/KAN-58 harness store (manifest.json + progress.jsonl)
and an approved truth snapshot (chaos/cases + LABELS.json), and produces a
deterministic report: per-arm metrics with explicit denominators, disclosed
missing/failed runs, raw row ids and a sha256 export digest per aggregate.

Stdlib only. Nothing here calls an LLM, kubectl or reads a Secret, and nothing
writes to the store it reads.
"""
METRICS_ENGINE_VERSION = "0.1.0-kan137"
