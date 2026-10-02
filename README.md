# Tykee

A private Telegram bot that helps two people make everyday low-stakes decisions: "what's for
dinner?", "what should we watch?". The name is a phonetic spelling of Tyche, the Greek goddess of
chance.

- **Claude** handles the conversation and reasoning.
- **A deterministic decision engine** does the actual picking: weighted, genuinely random, and it
  avoids suggesting the same thing three nights running. Randomness lives in code, never in the LLM.
- **A "second brain"** of markdown notes with local hybrid search (keywords + embeddings) remembers
  both people's preferences, dislikes and allergies.
- **Web search and fetch** (Claude's server-side tools) answer live questions like opening hours
  or reviews, with short cited replies.
- **A LAN-only admin dashboard** controls persona, models, options, memory and spend.

It runs as one Python 3.12 asyncio process with SQLite, in a single Docker container on a home server
(linux/amd64, ≤ 1 GB RAM). The design doc is the source of truth:
**[docs/design.md](docs/design.md)**.

## How it behaves

- **The shared group is the main interface.** Tykee reads every group message but stays silent by
  default. It always answers @mentions, replies and `/commands`, and steps in on its own only when
  a cheap judge call thinks it would help (e.g. "idk, you decide"). `/quiet` mutes it.
- **Forum topics:** Tykee reads and learns from every topic (except an optional ignore list) but
  only speaks in one answer topic, set with `/settopic` or from the dashboard. A background
  harvester turns preferences mentioned in other topics into memory suggestions.
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
- **Web lookups** happen only when the answer depends on live facts, capped per reply and per day,
  and paused when spend nears the budget. Facts found on the web never go into memory without
  approval.
- **Bootstrap import:** upload a Telegram Desktop export in the dashboard and Tykee learns your
  past decisions, categories and preferences from it. Health, money, work and relationship talk
  is skipped.
- **Google Maps links:** paste a Maps link (or share a Telegram venue) and Tykee works out which
  shop it is, in code with no API or LLM, and remembers it as a place. "Eating here" with a link in
  the answer topic records the decision and gets a quiet 👌 reaction instead of a reply, so "where
  are we eating?" answers with the shop name and link.
- **Place recommendations:** "brunch around Tiong Bahru, pet friendly" picks from places you know,
  ranked and chosen in code, plus new ones found on the web when there aren't enough. Must-haves
  (pet-friendly, kid-friendly, halal, aircon, quiet) show where the info comes from ("you confirmed"
  vs "per website, call ahead"). Each pick has a Map link, and [✅ 1] [✅ 2] [✅ 3] [🎲 more] buttons
  settle it in one tap. Pets you add in the dashboard are taken into account. "Near home" uses a
  neighbourhood you set, never an address.
- **Scheduled nudges** (off by default) can post a pick at a set time, e.g. "Dinner? I'm thinking
  Thai" on weekdays at 17:30. No Claude call needed.
- **If Claude is unavailable** or the budget cap is reached, it falls back to a plain random pick.

Commands: `/pick`, `/options`, `/remember`, `/forget`, `/think`, `/quiet`, `/unquiet`, `/inbox`,
`/settopic`, `/help` (see design §10).

## Architecture

```
Telegram ◄─ long polling ─► Telegram adapter (aiogram) ─► Orchestrator ─► Claude API (tool loop,
                                                                │           + web search/fetch)
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
| Web | Claude API server tools (web search + fetch); no extra API key |
| Dashboard | FastAPI + Jinja2 + HTMX, password login, LAN only (`http://<server>:8081`) |

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
| `GROUP_TOPIC_ID` | Optional. Seeds the forum topic Tykee answers in on first run. Easier: send `/settopic` in that topic as the admin. |
| `LOG_LEVEL` | Default `INFO`. |
| `TYKEE_DATA_DIR` | Host path mounted at `/data` (default `./data`). On the server, use a fast persistent volume. |
| `DASHBOARD_PASSWORD_HASH`, `SESSION_SECRET` | Dashboard login. Generate both with `docker compose run --rm tykee python -m app.dashboard.hashpw`. Single-quote the argon2 hash, because compose interpolates `$`. Without both, the dashboard stays off. |

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

Backups (nightly SQLite copies plus a local git commit of the vault), restore, healthcheck and other
operations are covered in the [runbook](docs/runbook.md).

### 5. Optional: account reader (M10)

The account reader logs in **as you** (read-only) to learn decisions from chats the bot isn't in,
such as your DM with your partner. It needs three values in `.env`, plus a one-time login.

#### 5a. `TG_API_ID` and `TG_API_HASH` (from Telegram)

1. Go to <https://my.telegram.org> and log in with **your** phone number. The code arrives in your
   Telegram app, not by SMS.
2. Click **API development tools**.
3. Fill in "Create new application":
   - App title: `Tykee Reader`
   - Short name: `tykeereader` (5–32 letters/numbers)
   - Platform: `Desktop`
   - URL and description: leave blank, or "personal read-only reader"
4. Click **Create application**. Copy:
   - **App api_id** (a number) → `TG_API_ID`
   - **App api_hash** (32 hex characters) → `TG_API_HASH`

Notes:
- Each account gets one app. Revisiting the page later shows the same values.
- If creation fails with a plain "ERROR", it's usually a VPN, ad-blocker or browser issue. Try
  another browser with them off.
- Treat the hash like a password. It can't log in on its own, but don't share it.

#### 5b. `READER_SESSION_KEY` (you generate it)

This key encrypts the saved Telegram login. It must be a Fernet key (32 random bytes, URL-safe
base64, 44 characters). Generate it with either:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# or, without Python packages:
openssl rand -base64 32 | tr '+/' '-_'
```

#### 5c. Add them to `.env`

```env
TG_API_ID=12345678
TG_API_HASH=0123456789abcdef0123456789abcdef
READER_SESSION_KEY=q3V9x...Zk8=
```

- Keep `.env` next to `compose.yaml`, **not** inside the data directory, so the key never ends up
  in backups. Backups then never contain a usable login.
- `chmod 600 .env`.
- Save a copy of `READER_SESSION_KEY` in your password manager.

#### 5d. Log in once, on the server

```bash
docker exec -it tykee python -m app.reader.login
```

It asks for your phone number, the login code and your 2FA password (never stored), then saves the
encrypted session. In Telegram → Settings → Devices it shows up as **"Tykee reader (read-only)"**.

Then, in the dashboard (System → Account reader):

1. Turn the reader **on**.
2. Add the chats Tykee may read (a `@username`, numeric id, `t.me` link, or **Pick from my
   chats**). Adding needs your dashboard password again, and the bot DMs you about it.
3. Tick each chat's consent box (password again).

Nothing is read until all three are done. The first poll only notes where to start; after that,
new messages are fetched every 30 minutes per chat (changeable) and harvested right away. To
learn from older history, press **Backfill**: it pulls up to 6 months into the Import wizard,
with the usual cost preview and review.

#### Lost key or want to revoke access

- **Lost key:** the saved session can't be decrypted, but nothing else breaks. Generate a new key,
  run the login again, and remove the old "Tykee reader" entry under Telegram → Settings → Devices.
- **Revoke:** press **Disconnect** in the dashboard, or terminate "Tykee reader (read-only)" from
  Telegram → Settings → Devices on any phone. The reader disables itself and alerts you.

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
| M4 ✅ | Dashboard, plus forum topics (read every topic, answer in one) |
| M5 ✅ | Bootstrap import from 6 months of Telegram chat history |
| M6 ✅ | Scheduled nudges, backups, healthcheck, runbook |
| M7 ✅ | Web search and fetch, with cited answers |
| M8 ✅ | Google Maps links → places: resolve pasted links to a named shop (no API, no LLM), record "eating here" as a decision, never store home addresses |
| M9 ✅ | Place recommendations: "brunch around Tiong Bahru, pet friendly" picks from known places plus web discovery, with must-have filters and a source for each attribute (you confirmed vs. per website) |
| M10 ✅ | Read-only account reader: learns decisions and preferences from chats the bot isn't in (starting with the Jack ↔ partner DM) via Jack's account. Strictly read-only; which chats it may read is picked in the dashboard, each with its own consent; one-click Disconnect |

Details and acceptance criteria are in design §16.

## Privacy

Everything stays on the server except the prompts sent to the Anthropic API, web searches made
through it, and the redirect lookups for pasted Maps short links (Google hosts only). Embeddings
are computed locally. The dashboard is LAN-only, with no port forwarding.
Tykee doesn't use the Google Maps or Places API: place info comes from pasted links, chat and web
search, and home addresses are never stored.

The M10 account reader logs in as Jack and reads only the chats on an allowlist managed
in the dashboard. Each chat needs a recorded consent, and adding one requires re-entering the admin
password and triggers a Telegram alert to the admin. The reader can't send, react or mark messages
as read. The session is encrypted with a key kept out of backups, and raw chat text is deleted after
harvesting (7 days by default).
