# Tykee

A private Telegram bot that helps two people make everyday low-stakes decisions: "what's for
dinner?", "what should we watch?". The name is a phonetic spelling of Tyche, the Greek goddess of
chance.

- **Claude** handles the conversation and reasoning.
- **A deterministic decision engine** does the actual picking: weighted, genuinely random, and it
  avoids suggesting the same thing three nights running. Randomness lives in code, never in the LLM.
- **A "second brain"** of markdown notes with local hybrid search (keywords + embeddings) remembers
  both people's preferences, dislikes and allergies.
- **A LAN-only admin dashboard** controls persona, models, options, memory and spend.

It runs as one Python 3.12 asyncio process with SQLite, in a single Docker container on a home server
(linux/amd64, ≤ 1 GB RAM). The design doc is the source of truth:
**[docs/design.md](docs/design.md)**.

## How it behaves

- **The shared group is the main interface.** Tykee reads every group message but stays silent by
  default. It always answers @mentions, replies and `/commands`, and steps in on its own only when
  a cheap judge call thinks it would help (e.g. "idk, you decide"). `/quiet` mutes it.
- **Private DMs** work too, and always get a reply.
- **Only allowlisted Telegram users** and the one configured group are served. Everything else is
  dropped before any processing or API call.
- **Decisions** come with inline buttons: ✅ Go with it, 🎲 Reroll, ❌ Not this. Outcomes feed back
  into future weights.
- **Categories** (dinner, movie, weekend activity, …) are created from conversation as needed, not
  from a fixed list.
- **Memory:** "remember I hate coriander" is written to the vault immediately. Facts Tykee infers
  on its own go to an inbox for approval. Pinned notes (like allergies) are always in context, so
  they never depend on search recall.
- **If Claude is unavailable** or the budget cap is reached, it falls back to a plain random pick.

Commands: `/pick`, `/options`, `/remember`, `/forget`, `/think`, `/quiet`, `/unquiet`, `/inbox`,
`/settopic`, `/help` (see design §10).

## Architecture

```
Telegram ◄─ long polling ─► Telegram adapter (aiogram) ─► Orchestrator ─► Claude API (tool loop)
                                                                │
                                   ┌────────────────────────────┼──────────────────┐
                                   ▼                            ▼                  ▼
                           Decision engine            Second brain           Settings
                           (weighted random)          (notes + FTS5 +        (SQLite, read
                                                       sqlite-vec + local     per request)
                                                       embeddings)
Browser (LAN) ◄─► Dashboard (FastAPI, :8081)

/data/bot.db   SQLite: app state, FTS5, sqlite-vec
/data/vault/   markdown notes (source of truth; the search index can be rebuilt from it)
```

| Piece | Choice |
|---|---|
| Telegram | aiogram, long polling (no inbound ports or webhooks) |
| LLM | Claude API via the Anthropic SDK; model IDs and prices live only in `app/seed/settings.json` |
| Storage | SQLite (WAL): one writer thread plus a read-only pool |
| Embeddings | `fastembed` (ONNX, CPU) with `intfloat/multilingual-e5-small`, int8 weights, baked into the image (§6.9) |
| Retrieval | FTS5 BM25 + vector KNN, fused with Reciprocal Rank Fusion, plus 1-hop wikilink expansion |
| Dashboard | FastAPI + Jinja2 + HTMX, password login, LAN only |

## Getting started

### 1. Create the bot

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Find both users' numeric Telegram IDs (e.g. via [@userinfobot](https://t.me/userinfobot)).

### 2. Configure

```bash
cp .env.example .env
```

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from BotFather. Required. |
| `ANTHROPIC_API_KEY` | Claude API key. If empty, the bot runs in fallback mode (no Claude calls). |
| `ALLOWED_TELEGRAM_IDS` | `<id>:<slug>` pairs, comma-separated. The first entry is the admin. |
| `GROUP_CHAT_ID` | The one group Tykee serves. Can be left empty at first (see step 4). |
| `TZ` | Household timezone. Daily budgets and caps reset on its day boundaries. |
| `TYKEE_DATA_DIR` | Host path mounted at `/data` (default `./data`). On the server, use a fast persistent volume. |
| `DASHBOARD_PASSWORD_HASH`, `SESSION_SECRET` | Dashboard login (M4). Single-quote the argon2 hash, because compose interpolates `$`. |

Never commit `.env` or the data directory.

### 3. Run

On the server:

```bash
./deploy.sh
```

This pulls the latest commit, checks `.env` and data-dir ownership (uid 1000), rebuilds, restarts,
and waits for the bot to report `polling`. On failure it prints the logs and a rollback command.
`./deploy.sh <branch>` deploys a PR branch to test it, and `./deploy.sh main` goes back.

For local development use `make up`, `make logs` and `make down`.

### 4. Add it to the group

1. Add the bot to your group and **make it a group admin**. That lets it see every message, not
   just mentions, and receive 👍/👎 reactions.
2. If `GROUP_CHAT_ID` is empty, the bot posts the group's chat id and leaves. Put that id in
   `.env` and redeploy.

Only one instance may run at a time. A second poller gets `409 Conflict` from Telegram.

## Development

All tooling runs inside Docker, so no local Python or uv is needed.

| What | Command |
|---|---|
| Install/refresh dev venv | `make sync` |
| Lock dependencies | `make lock` |
| Tests (offline, no API key) | `make test` (extra args: `make test ARGS="-k middleware"`) |
| Live API tests (opt-in) | `ANTHROPIC_API_KEY=... make test ARGS="-m integration"` |
| Lint / format | `make lint` / `make fmt` |
| Types (`mypy --strict`) | `make typecheck` |
| Everything | `make check` |
| Migrate `./data/bot.db` | `make migrate` (also runs automatically on startup) |
| Build the amd64 image | `make build` |

CI (`.github/workflows/ci.yml`) runs the same checks on every PR, plus shellcheck, a guard against
committed bot data, and an amd64 image build.

Conventions, code layout and contribution rules are in [CLAUDE.md](CLAUDE.md). In short: type
hints everywhere, external services behind protocols with fakes, never touch SQLite from the event
loop, and update `docs/design.md` in the same commit as any behaviour change.

## Roadmap

| Milestone | Scope |
|---|---|
| M1 ✅ | Skeleton: allowlist, group + DM handling, Claude chat with history, usage and budget tracking |
| M2 ✅ | Decision engine: dynamic categories, weighted non-repeating picks, buttons, feedback learning |
| M2b ✅ | Ambient participation: decides when to speak in the group, `/quiet` |
| M3 ✅ | Second brain: notes, local embeddings, hybrid search, memory tools, inbox |
| M4 | Dashboard, plus forum topics (read every topic, answer in one) |
| M5 | Bootstrap import from 6 months of Telegram chat history |
| M6 | Scheduled nudges, backups, healthcheck, runbook |
| M7 | Web search and fetch, with cited answers |

Details and acceptance criteria are in design §16.

## Privacy

Everything stays on the server except the prompts sent to the Anthropic API. Embeddings are computed
locally. The dashboard is LAN-only, with no port forwarding.
