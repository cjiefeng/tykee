# Tykee: kickoff instructions for Claude Code

You're building **Tykee**, a private Telegram bot powered by the Claude API that helps two people (me, Jack, and my partner) make everyday decisions. The full design is in `docs/design.md`. **Read it completely before doing anything.** It is the source of truth; section numbers below (§) refer to it.

## About me
I'm a senior backend engineer (Python, Go, MySQL at scale, Docker/Kubernetes). Skip beginner explanations; be direct about trade-offs. I'll review every milestone.

## Repo state
This folder currently contains only `docs/design.md` and this file (`docs/KICKOFF.md`). If it isn't a git repo yet, run `git init` first.

## How we'll work
1. **Plan first.** After reading the doc, give me: (a) a proposed repo layout, (b) the M1 task breakdown, (c) any contradictions, gaps or risky assumptions you found in the design, with a recommendation for each. Wait for my go-ahead before writing code.
2. **Work milestone by milestone** (§16): M1 → M2 → M2b → M3 → M4 → M5 → M6 → M7 → M8. Stop at the end of each milestone, summarise what was built, how to run/verify it, and what deviated from the design. Don't start the next milestone until I say so.
3. **Don't silently deviate from the design.** If something in the doc is wrong or impractical, stop and propose the change. If I approve, update `docs/design.md` in the same commit so the doc stays true.
4. **Create a `CLAUDE.md`** at the start with: project summary, commands (run, test, lint, migrate, docker build), conventions below, and a pointer to `docs/design.md`. Keep it updated.
5. **Commit** at sensible checkpoints with clear messages; never commit secrets.

## Non-negotiables (from the design)
- **Python 3.12**, single async process hosting bot + dashboard + scheduler (§3). Exactly one replica.
- **SQLite only** (WAL; single writer connection on a dedicated thread; read-only connections for reads; pragmas per §5.1). Schema per §5.2 via numbered SQL migrations.
- **Second brain** = markdown files under `/data/vault` as source of truth; FTS5 + `sqlite-vec` index is derived and rebuildable (§6). All vault writes go through `NoteStore` (atomic write + synchronous reindex). No file watcher.
- **Embeddings local**: `fastembed` with `intfloat/multilingual-e5-small`, with `"query: "` / `"passage: "` prefixes (§6.8). Run CPU-bound work in a thread pool, never on the event loop.
- **LLM = Claude API only** via the official `anthropic` SDK (§7.0). All calls go through one `LLMClient` wrapper (budget check, usage/cost recording, timeouts, retries). **No hardcoded model IDs.** Read them from `settings`. Check Anthropic's current docs for model IDs, tool-use, structured outputs, prompt caching and Batches API syntax rather than relying on memory.
- **Randomness in code**, never by the LLM (§8). Category resolution is deterministic (§8.1).
- **Telegram**: `aiogram` 3.x, long polling, `parse_mode=HTML`, allowlist enforced before any processing (§10). Group privacy mode off; speak-or-stay-silent logic per §10.2.
- **Dashboard**: FastAPI + Jinja2 + HTMX (vendored, no CDN), password login, CSRF, LAN-only (§11).
- **Deploy target**: a single Docker image for **linux/amd64** (UGREEN DXP4800 Plus NAS, 8 GB RAM, ≤ 1 GB for this container), docker compose, `/data` volume (§14). Bake the embedding model into the image.
- Secrets only via `.env`; ship a `.env.example`.

## Engineering conventions
- `uv` for dependency management, `ruff` (lint + format), `mypy --strict` on `app/`, `pytest` + `pytest-asyncio`.
- Type hints everywhere; small modules with clear boundaries matching the design's components (adapter, orchestrator, llm, decision engine, second brain, dashboard, scheduler, importer).
- **Testability without external services:** the Telegram adapter and `LLMClient` sit behind interfaces with fakes for tests. Unit tests must run offline with no API key. Add an opt-in integration test marker for real API calls.
- Must-have tests: migrations apply cleanly; weighting/recency math (§8.3); category resolver thresholds (§8.1); NoteStore atomic write + reindex + path-traversal rejection; hybrid retrieval with owner filtering (§6.5); allowlist enforcement; speak-or-silent stage-1 rules and debounce (§10.2); Telegram export parser for both single-chat and full-account JSON, including list-form `text` fields (§15.1).
- Structured JSON logging to stdout. No secrets or full message contents at INFO level.

## Start now
Read `docs/design.md`, then reply with the plan described in step 1 of "How we'll work". Don't write code yet.
