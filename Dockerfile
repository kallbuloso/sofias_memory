FROM ghcr.io/astral-sh/uv:0.8.17 AS uv

# Explicit Python patch version + explicit Debian variant, pinned additionally
# by digest: python:3.12-slim (and even python:3.12-slim-bookworm alone) are
# floating tags that move without notice, which the PRD's release-image
# reproducibility requirement forbids. Bumping the Python version means
# updating both the tag and the digest together -- no more work than bumping
# the tag alone, and it removes any ambiguity about which image was actually
# built.
FROM python:3.12.14-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579 AS runtime

ARG APP_VERSION=0.1.1
ARG VCS_REF=local

LABEL org.opencontainers.image.title="Sofias Memory" \
    org.opencontainers.image.version="${APP_VERSION}" \
    org.opencontainers.image.revision="${VCS_REF}" \
    org.opencontainers.image.source="https://github.com/kallbuloso/sofias_memory" \
    org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH="/app"

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./

RUN uv sync --frozen --no-dev --no-install-project

COPY sofias_memory ./sofias_memory
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY scripts ./scripts

RUN groupadd --system sofias-memory \
    && useradd --system --gid sofias-memory --home-dir /app sofias-memory \
    && mkdir -p /data/sources /data/tmp \
    && chown -R sofias-memory:sofias-memory /app /data

USER sofias-memory

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('HTTP_PORT', '8000') + '/health/live', timeout=5).read()"

CMD ["uvicorn", "sofias_memory.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
