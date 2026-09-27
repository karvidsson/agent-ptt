FROM ghcr.io/astral-sh/uv:0.11.8 AS uv
FROM python:3.11-slim-bookworm

COPY --from=uv /uv /usr/local/bin/uv
RUN useradd --uid 10001 --create-home app && mkdir -p /home/app/.cache/huggingface && chown -R app:app /home/app/.cache
WORKDIR /app
ENV UV_CACHE_DIR=/tmp/uv-cache UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH" HOME=/home/app \
    AGENT_PTT_HOSTED=1 AGENT_PTT_MUTE=1 AGENT_PTT_SERVER_SPEECH=1 PORT=8080
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/tmp/uv-cache uv sync --frozen --only-group server
COPY agent_ptt ./agent_ptt
COPY migrations ./migrations
COPY alembic.ini ./
ENV PYTHONPATH=/app
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-m", "agent_ptt.runtime", "probe"]
CMD ["python", "-m", "agent_ptt.runtime", "serve"]
