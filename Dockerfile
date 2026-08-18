FROM ghcr.io/astral-sh/uv:0.11.2 AS uv

FROM python:3.12.11-slim-bookworm AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1
WORKDIR /app

# Keep the dependency installer versioned with the image contract.
COPY --from=uv /uv /uvx /bin/

COPY pyproject.toml uv.lock README.md ./
RUN uv lock --check

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --compile-bytecode

COPY config ./config
COPY migrations ./migrations
COPY spec ./spec
RUN python -c "import vtrade, vtrade.api, vtrade.worker"
RUN python -m vtrade.frozen_artifacts
USER 65532:65532
EXPOSE 8000
CMD ["uvicorn", "vtrade.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
