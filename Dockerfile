# Groundwork image: MCP server (stdio), Slack product runtime, CLI.
# NOTE: built and run in CI only; not verified on the authoring machine (Docker was not installed).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY context ./context
COPY evals ./evals
COPY config/product.example.yaml ./config/product.example.yaml
COPY data/manifests ./data/manifests
RUN uv sync --frozen --no-dev

# Least privilege: non-root user; only the data dir is writable.
RUN useradd --create-home --uid 10001 cf && mkdir -p /app/data && chown -R cf:cf /app/data
USER cf

ENV PATH="/app/.venv/bin:$PATH" \
    GROUNDWORK_ROOT=/app \
    GROUNDWORK_DATA_DIR=/app/data

# The warehouse is NOT baked into the image (dataset license: CC BY-NC-SA 4.0).
# Run `groundwork acquire && groundwork build` into the mounted /app/data volume first.
HEALTHCHECK --interval=60s --timeout=30s --start-period=10s --retries=3 \
    CMD ["groundwork", "health"]

ENTRYPOINT ["groundwork"]
CMD ["serve", "--transport", "stdio"]
