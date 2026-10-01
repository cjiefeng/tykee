# Dev tooling runs inside Docker (no local Python/uv required).
DEV_IMAGE ?= ghcr.io/astral-sh/uv:python3.12-bookworm-slim
RUN = docker run --rm -v $(CURDIR):/work -v tykee-venv:/work/.venv -v tykee-uv-cache:/root/.cache/uv \
      -w /work -e UV_LINK_MODE=copy -e ANTHROPIC_API_KEY $(DEV_IMAGE)

.PHONY: lock sync test lint fmt typecheck check shell build migrate up down logs
lock:      ; $(RUN) uv lock
sync:      ; $(RUN) uv sync
test:      ; $(RUN) uv run pytest $(ARGS)
lint:      ; $(RUN) sh -c "uv run ruff check . && uv run ruff format --check ."
fmt:       ; $(RUN) sh -c "uv run ruff check --fix . && uv run ruff format ."
typecheck: ; $(RUN) uv run mypy
check: lint typecheck test
shell:     ; docker run --rm -it -v $(CURDIR):/work -v tykee-venv:/work/.venv -v tykee-uv-cache:/root/.cache/uv -w /work $(DEV_IMAGE) bash
build:     ; docker buildx build --platform linux/amd64 -t tykee:dev .
migrate:   ; $(RUN) sh -c "DATA_DIR=/work/data uv run python -m app.db.migrate"
up:        ; docker compose up --build -d
down:      ; docker compose down
logs:      ; docker compose logs -f tykee
