FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY config ./config
COPY migrations ./migrations
COPY spec ./spec
USER 65532:65532
EXPOSE 8000
CMD ["uvicorn", "vtrade.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
