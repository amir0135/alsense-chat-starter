# Pinned base: uv 0.9.11 on Python 3.12 (Debian bookworm). Update the tag and the digest together.
FROM ghcr.io/astral-sh/uv:0.9.11-python3.12-bookworm-slim@sha256:ef305979f94d4729707a0fc98444ebf1bc0e63a2d2919a62d28fcc3e42957508

# Dependencies install from the committed uv.lock with hash checking. Behind a corporate mirror, pass
# --build-arg UV_DEFAULT_INDEX=https://your-mirror/simple: the same versions and hashes are enforced.
ARG UV_DEFAULT_INDEX=https://pypi.org/simple
ARG APP_COMMIT=""
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ALSENSE_CONTAINER=1 \
    APP_PORT=8001 \
    SESSION_DB=/data/sessions.sqlite3 \
    APP_COMMIT=${APP_COMMIT}

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file /tmp/requirements.txt \
    && uv venv "$VIRTUAL_ENV" \
    && uv pip install --require-hashes --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY src ./src
COPY scripts ./scripts
RUN useradd --uid 10001 --create-home starter && mkdir -p /data && chown starter /data
USER starter

EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/health', timeout=2)"]
# Demo sign-in has no production authentication: publish the port on host loopback only.
CMD ["uvicorn", "alsense_chat.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8001"]
