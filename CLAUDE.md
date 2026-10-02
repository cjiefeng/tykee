# Tykee

Private Telegram bot for two people (Jack + partner) that helps make everyday decisions. Claude
API for conversation, a deterministic decision engine for randomness, a markdown "second brain"
with local hybrid search, and a LAN-only admin dashboard. One Python 3.12 asyncio process,
SQLite, one Docker container on a UGREEN NAS (linux/amd64, ≤ 1 GB RAM).

**Source of truth: [docs/design.md](docs/design.md).** Read the relevant section before changing
behaviour. If the design is wrong or impractical, propose the change; once approved, update
`docs/design.md` in the same commit. Milestones are in §16; work one at a time.

## Commands

All tooling runs inside Docker (no local Python/uv needed).

| What | Command |
|---|---|
| Install/refresh dev venv | `make sync` |
| Lock deps | `make lock` |
| Tests (offline) | `make test` (extra args: `make test ARGS="-k middleware"`) |
| Live API test (opt-in) | `ANTHROPIC_API_KEY=... make test ARGS="-m integration"` |
| Lint / format | `make lint` / `make fmt` |
| Types (`mypy --strict` on `app/`) | `make typecheck` |
| Everything | `make check` |
| Migrate `./data/bot.db` manually | `make migrate` (also runs automatically on startup) |
| Build amd64 image | `make build` |
| Run locally | `cp .env.example .env`, fill in, `make up`; `make logs`; `make down` |
| CI | GitHub Actions `.github/workflows/ci.yml` runs the same checks on every PR (§14.5) |
| Deploy (NAS, no make needed) | `./deploy.sh` (latest of current branch) or `./deploy.sh <branch>` to test a PR branch |

## Layout

- `app/main.py`: startup wiring (migrate → seed settings/users → resolve group → poll).
- `app/config.py`: env-only config (secrets, allowlist `<id>:<slug>`, `GROUP_CHAT_ID`, `TZ`).
- `app/settings.py` + `app/seed/`: runtime settings in the `settings` table, read per request.
  Seeds are inserted only for missing keys. **Model IDs and prices live only in
  `app/seed/settings.json`**; a test fails if `claude-` appears in any `.py` under `app/`.
- `app/db/`: `Database` (one writer thread + read-only pool), numbered migrations in
  `app/db/migrations/NNNN_name.sql`, small query modules in `app/db/repos/`.
- `app/llm/client.py`: `LLMClient` / `BatchClient` protocols + `AnthropicLLMClient`, the only
  code that calls the Claude API (budget check, timeouts, SDK retries, usage/cost rows; batches
  recorded at 50%; import spend counts against the monthly cap only).
- `app/telegram/`: `AccessGate` outer middleware (allowlist + allowed group, runs before
  anything else), `TelegramAdapter` (persist + reply when addressed, commands, ✅🎲❌
  callbacks), `ChatGateway` port, keyboards, HTML formatting/splitting.
- `app/decisions/`: category resolver (§8.1: alias → slug → Claude chooses from the catalog),
  engine (§8.2–8.3 weighting/sampling; pure math split from DB code), feedback (§8.4), and
  `DecisionService`, the async façade used by tools, commands, callbacks and fallback.
- `app/orchestrator/`: prompt assembly (§7.2), history replay, Claude tool loop (max 6
  iterations, then `tool_choice: none`), tool schemas + router in `tools.py`, fallback (§8.5),
  rolling chat summaries (`summary.py`, background, one run per chat at a time).
- `app/brain/`: second brain (§6). `notes.py` (pure: paths, frontmatter, edit modes, chunking,
  wikilinks), `store.py` (`NoteStore`: the only vault writer; atomic write → synchronous
  reindex; `reconcile()` at startup), `index.py` (notes/chunks/FTS5/vec0/links rows),
  `retrieval.py` (hybrid BM25 + KNN + RRF, owner-filtered), `embedder.py` (fastembed e5-small,
  int8 by default, own thread), `memory.py` (`MemoryService`: scopes, write policy, inbox,
  pinned block, avoid_tags, decision log). Vault lives at `/data/vault`.
- `app/telegram/topics.py`: forum topics (§10.4): read every topic, answer only in the answer
  topic; `thread_of()` (General = 1) / `send_thread()` (General → no thread id).
- `app/harvest.py` + `app/scheduler.py`: memory harvester for non-answer topics (code-only tick
  every minute, Haiku extraction only when a topic has new chat); `app/extraction/schema.py` is
  shared with the M5 import; `app/inbox_appliers.py` applies approved category/option suggestions.
- `app/importer/`: bootstrap import (§15, notes in §15.6). `telegram.py` (streaming ijson parser,
  zip guard), `windowing.py` (windows + cost estimate), `prompts.py`, `consolidate.py` (pure:
  episode dedupe, category-design validation, options/weights, notes), `jobs.py` (queries +
  review edits), `service.py` (`ImportService`: job state machine ticked by the scheduler, one
  Message Batch per round, Opus consolidation, apply). Exports live in `/data/imports`.
- `app/dashboard/`: FastAPI + Jinja2 + vendored HTMX (§11), served by uvicorn inside the bot's
  event loop. `core.py` (LAN-only, argon2 login + lockout, sessions, CSRF, `render()`),
  `queries.py` (all dashboard SQL + validated `save_settings`), `views_*.py` per page,
  `templates/`, `static/` (see `static/VENDORED.md`). `python -m app.dashboard.hashpw` makes the
  password hash + session secret.
- `app/health.py`: in-process health signals for the dashboard tiles.
- `app/ambient/`: speak-or-stay-silent (§10.2). `rules.py` (pure stage-1), `debounce.py`
  (per-chat timers, in memory), `judge.py` (structured-output call), `state.py`
  (`chat_state`/`ambient_log`), `phrases.py` (mute/negative cues, `/quiet` durations),
  `service.py` (`AmbientService` ties it together; the adapter is its `responder`).
- `scripts/embedding_eval.py`: the §6.9 int8 vs fp32 eval (needs the real models; run in the dev
  container with `uv run python -m scripts.embedding_eval all <cache dir>`).
- `tests/fakes/`: `FakeLLMClient` (scripted text / `tool_call(...)` / exceptions), `FakeGateway`,
  `FakeEmbedder` (hashed bag of words; unit tests never load the real model), `FakeBatches`
  (scripted Message Batches), `telegram_export.py` (export builders).
  `tests/conftest.py`: migrated temp DB (`env`), `make_stack()` wiring the whole bot with fakes,
  `seed_category()`. Ambient tests call `stack.ambient.fire(chat_id)` directly instead of
  waiting for the debounce; background tasks are closed via `env.closers`.

## Conventions

- Type hints everywhere; `mypy --strict` must pass. `ruff` lint + format, line length 100.
- Small modules matching design components; external services sit behind protocols with fakes.
  Unit tests run offline with no API key; real API calls use `@pytest.mark.integration`.
- DB access: pass a function `fn(conn)` to `db.write` (runs in a transaction on the writer
  thread) or `db.read` (read-only connection). Never touch SQLite from the event loop.
  CPU-bound work goes to a thread pool.
- Timestamps are stored as UTC text (`app/timeutil.py`); day boundaries use the household `TZ`.
- Telegram output: Claude writes plain text + `**bold**`/`_italic_`/`[text](url)`;
  `app/telegram/formatting.py` escapes and converts. Never send unescaped model text as HTML.
- History replay sends only user text and final assistant text; tool calls are stored for
  debugging, not replayed. Fallback replies are not stored in history.
- Dashboard: every POST needs the session's CSRF token (forms: `{{ csrf_input }}`; HTMX: the
  `X-CSRF-Token` header set on `<body>`). Settings writes go through `queries.save_settings`, which
  validates the whole set first. Store new session values by assignment (Starlette only re-sends
  the cookie when the session dict itself changes). Tests drive the app with
  `httpx.ASGITransport(app, client=(ip, port))`.
- Group sends go to the answer topic: pass `thread=` to `TelegramAdapter._send`; never send to
  the group without it.
- Vault writes only through `NoteStore` (or `MemoryService` for Claude-facing policy). Never write
  files under `/data/vault` directly; FTS5 deletes need the old text and vec0 rows aren't
  cascaded, both handled in `brain/index.py`.
- Randomness lives in code, never in the LLM (§8). Engine functions take an injectable
  `random.Random`; tests seed it and use the `Clock` from `tests/conftest.py`.
- Tool inputs are validated with pydantic; bad input goes back to Claude as an `is_error`
  tool result, never an exception.
- Logging: structured JSON to stdout. No secrets and no message contents at INFO.
- Secrets only via `.env` (see `.env.example`); never commit `.env`, `data/` or anything the bot
  writes (DB, vault, model cache, backups, imports). `.gitignore` covers these by file shape and
  CI's "no bot data committed" job enforces it (§12).
- Commit at sensible checkpoints with clear messages.
