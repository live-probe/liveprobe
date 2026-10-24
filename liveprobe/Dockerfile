FROM node:22-bookworm-slim AS codex
# 0.163.0: first pinned version whose model catalog accepts gpt-6.1-sol on a ChatGPT (subscription) account;
# 0.157.0 rejects it with "not supported when using Codex with a ChatGPT account" (checked 2026-10-18).
RUN npm install -g @openai/codex@0.163.0

# Claude Code CLI (native per-arch binary shipped via optional npm deps). Pinned; verified with --version.
FROM node:22-bookworm-slim AS claude
RUN npm install -g @anthropic-ai/claude-code@2.1.283 \
 && CLAUDE_REAL="$(readlink -f "$(command -v claude)")" && cp "$CLAUDE_REAL" /claude \
 && /claude --version

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 \
    LIVEPROBE_EMBEDDING_CACHE_DIR=/opt/models PATH="/app/.venv/bin:/usr/local/bin:${PATH}" \
    DISABLE_AUTOUPDATER=1 DISABLE_TELEMETRY=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules/@openai/codex /usr/local/lib/node_modules/@openai/codex
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex && chmod +x /usr/local/bin/codex
COPY --from=claude /claude /usr/local/bin/claude
RUN claude --version && codex --version
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.8.22 && uv sync --frozen --no-dev
ENV HF_HUB_OFFLINE=0
RUN /app/.venv/bin/python -c "from fastembed import TextEmbedding; m=TextEmbedding(model_name='sentence-transformers/all-MiniLM-L6-v2',cache_dir='/opt/models'); assert len(next(iter(m.embed(['ready'])))) == 384"
ENV HF_HUB_OFFLINE=1
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY app/ ./app/
COPY runbooks/ ./runbooks/
COPY reference/ ./reference/
COPY corpus/ ./corpus/
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
