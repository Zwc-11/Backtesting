# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.8.22-python3.12-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY config ./config
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --locked --no-dev --no-editable
RUN chmod -R a+rX /app/src /app/config /app/.venv
RUN useradd --uid 10001 --create-home app && mkdir -p /app/data && chown app:app /app/data
ENV PATH="/app/.venv/bin:$PATH" XASSET_DATA_DIR=/app/data PYTHONUNBUFFERED=1
USER app
EXPOSE 8000
ENTRYPOINT ["xasset-app", "--data-dir", "/app/data"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
