FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY . .
RUN uv sync --no-dev
ENV SESSION_DB=/data/sessions.sqlite3
RUN mkdir /data && useradd --uid 10001 --create-home starter && chown starter /data
USER starter
# Demo login has no production authentication. Keep the published port on host loopback only.
CMD ["uv", "run", "--no-sync", "uvicorn", "alsense_chat.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
