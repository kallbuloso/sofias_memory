FROM ghcr.io/astral-sh/uv:0.8.17 AS uv

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./

RUN uv sync --frozen --no-dev --no-install-project

COPY sofias_memory ./sofias_memory

RUN groupadd --system sofias-memory \
    && useradd --system --gid sofias-memory --home-dir /app sofias-memory \
    && mkdir -p /data/sources /data/tmp \
    && chown -R sofias-memory:sofias-memory /app /data

USER sofias-memory

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('HTTP_PORT', '8000') + '/health/live', timeout=5).read()"

CMD ["uvicorn", "sofias_memory.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
