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

## Layout

- `app/main.py`: startup wiring (migrate → seed settings/users → resolve group → poll).
- `app/config.py`: env-only config (secrets, allowlist `<id>:<slug>`, `GROUP_CHAT_ID`, `TZ`).
- `app/settings.py` + `app/seed/`: runtime settings in the `settings` table, read per request.
  Seeds are inserted only for missing keys. **Model IDs and prices live only in
  `app/seed/settings.json`**; a test fails if `claude-` appears in any `.py` under `app/`.
- `app/db/`: `Database` (one writer thread + read-only pool), numbered migrations in
  `app/db/migrations/NNNN_name.sql`, small query modules in `app/db/repos/`.
- `app/llm/client.py`: `LLMClient` protocol + `AnthropicLLMClient`, the only code that calls
  the Claude API (budget check, timeouts, SDK retries, usage/cost rows).
- `app/telegram/`: `AccessGate` outer middleware (allowlist + allowed group, runs before
  anything else), `TelegramAdapter` (persist + reply when addressed), `ChatGateway` port,
  HTML formatting/splitting.
- `app/orchestrator/`: prompt assembly (§7.2), history replay, turn handling.
- `tests/fakes/`: `FakeLLMClient`, `FakeGateway`. `tests/conftest.py` has a migrated temp DB.

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
- Randomness lives in code, never in the LLM (§8).
- Logging: structured JSON to stdout. No secrets and no message contents at INFO.
- Secrets only via `.env` (see `.env.example`); never commit `.env` or `data/`.
- Commit at sensible checkpoints with clear messages.
