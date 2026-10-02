# syntax=docker/dockerfile:1
# Target: linux/amd64 (UGREEN DXP4800 Plus). Build with `make build`.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable --reinstall-package tykee
# Bake the embedding model (int8 by default, §6.9) so startup works offline.
ARG EMBED_PRECISION=int8
RUN /app/.venv/bin/python -c "from pathlib import Path; from app.brain.embedder import download; download('${EMBED_PRECISION}', Path('/app/models'))" \
 && rm -rf /app/models/.locks && chmod -R a+rX /app/models

FROM python:3.12-slim
RUN useradd --uid 1000 --create-home tykee && mkdir /data && chown tykee:tykee /data
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/models /app/models
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
USER tykee
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "-m", "app"]
