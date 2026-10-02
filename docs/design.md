# Design Doc: Tykee — a Claude-powered decision bot for two

| | |
|---|---|
| **Status** | Draft v1.18 (bot data never in git; CI and deploy.sh, §14.5) |
| **Name** | Tykee: phonetic spelling of Tyche, the Greek goddess of chance. Telegram handle e.g. `@TykeeBot` (must end in "bot") |
| **Author** | Jack |
| **Date** | 2026-10-01 |
| **Users** | Jack + partner (2 users, fixed) |
| **Host** | UGREEN DXP4800 Plus (x86, Pentium Gold 8505, 8 GB), Docker on UGOS Pro, LAN-only |

---

## 1. Overview

A private Telegram bot that helps two people make everyday low-stakes decisions ("what's for dinner?", "what should we watch?"). Claude provides reasoning and conversation; a deterministic decision engine provides genuine randomness and anti-repetition; a bot-owned "second brain" (markdown notes + local hybrid search) gives it long-term knowledge of both users. An admin dashboard on the LAN controls behaviour, memory, and cost.

### 1.1 Goals

- Fast, useful answers to everyday decisions inside Telegram.
- Real randomness with memory: don't suggest the same thing three nights running.
- Persistent, inspectable, editable long-term memory per user plus shared memory.
- Full control from a dashboard: persona, models, options, memory, budgets.
- Simple to run: **one container, one process, one SQLite file + one notes folder.**

### 1.2 Non-goals

- Multi-tenant / more than two users.
- Human editing of the vault in Obsidian (the second brain is bot-owned; humans edit via the dashboard only).
- Public internet exposure (no webhooks, no public dashboard).
- Horizontal scaling / HA. Single replica by design.
- Voice notes, restaurant/maps APIs, weather (deferred; see §16). General web search/fetch **is** in scope (M7, §7.5).

---

## 2. Requirements

### 2.1 Functional

| ID | Requirement |
|---|---|
| F1 | Only allowlisted Telegram user IDs can interact; all others are silently ignored. |
| F2 | Users ask free-form decision questions; bot replies with one pick (default) or N options, with a short reason. |
| F3 | Inline buttons: **✅ Go with it**, **🎲 Reroll**, **❌ Not this**. Outcomes are recorded. |
| F4 | Decisions for "me", "partner", or "both" respect each person's hard constraints (allergies, dislikes). |
| F5 | Bot remembers facts on request ("remember I hate coriander") and can propose memories implicitly. |
| F6 | Recently chosen options are down-weighted per category with configurable decay. |
| F7 | Dashboard: edit persona/system prompt, model routing, per-category settings, options lists, memory notes, memory inbox, view conversations, usage & cost, reindex, backup. |
| F8 | Optional scheduled nudges: the bot starts a conversation at a set time (e.g. 17:30 "dinner idea?") in the group. **Off by default.** |
| F9 | Decision categories are **inferred and created dynamically** from conversation; no fixed list. Similar categories resolve to one canonical category (§8.1). |
| F10 | The **shared group** (Jack + partner + bot) is the primary interface. The bot reads all group messages and **decides for itself whether to speak or stay silent** (§10.2). Private DMs remain supported as a secondary channel. |
| F11 | **Bootstrap** the second brain, categories, options and decision history from the past 6 months of chat history (§15). |

### 2.2 Non-functional

| ID | Requirement |
|---|---|
| N1 | p50 reply latency < 4 s (dominated by Claude API). |
| N2 | Runs within ~1 GB RAM on the NAS. |
| N3 | All data local except prompts sent to the Anthropic API. |
| N4 | Survives restarts with no data loss; nightly backups. |
| N5 | Graceful degradation if Claude API is unavailable (pure random pick from options). |
| N6 | Daily/monthly API spend cap enforced in code. |

---

## 3. Architecture

### 3.1 Component diagram

```
                    ┌────────────────────────── tykee container (single Python process, asyncio) ───────────────────────────┐
 Telegram API ◄────►│ Telegram Adapter (aiogram, long polling)                                                                    │
 (outbound only)    │        │                                                                                                    │
                    │        ▼                                                                                                    │
                    │ Orchestrator ── builds prompt ──► Claude API (tool use loop) ──► Tool Router                                 │
                    │        │                                                         │        │          │                     │
                    │        │                                          Decision Engine ◄┘   Second Brain   Settings              │
                    │        │                                          (weighted random)    (NoteStore +   (hot-reloaded         │
                    │        │                                                               Retriever +    from SQLite)          │
                    │        │                                                               Embedder)                            │
                    │        ▼                                                                    │                               │
                    │ Scheduler (APScheduler: nudges, backups, reconcile)                         │                               │
                    │                                                                             │                               │
 Browser (LAN) ◄───►│ Dashboard (FastAPI + Jinja2 + HTMX, :8080, password login)                  │                               │
                    └─────────────────────────────────────────────────────────────────────────────┼───────────────────────────────┘
                                                                                                  ▼
                                               /data/bot.db  (SQLite: app state + FTS5 + sqlite-vec)    /data/vault/**/*.md (notes)
                                               └──────────────────────── NVMe volume ─────────────────────────────────────────┘
```

### 3.2 Key design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Process model | Single async process hosting bot, dashboard, scheduler | One SQLite writer, no IPC, trivial deploy. 2 users ≠ scale problem. |
| Telegram transport | Long polling | NAS is LAN-only; no inbound ports, no TLS certs. |
| Database | SQLite (WAL) for everything | Small data, single writer, zero ops, file-level backup. |
| Notes storage | Markdown files on disk = source of truth; SQLite holds a derived, rebuildable index | Human-readable, diffable (git), index can be dropped & rebuilt anytime. |
| Indexing trigger | Synchronous on write through `NoteStore` (no file watcher) | Only the bot/dashboard write the vault, so every write passes through one API. Startup reconcile catches drift. |
| Embeddings | Local, `fastembed` (ONNX, CPU) | Private, free, fast enough on the 8505. No PyTorch. |
| Retrieval | Hybrid: FTS5 BM25 + vector KNN, fused with RRF, + 1-hop wikilink expansion | Exact names need keywords; fuzzy intents need vectors. |
| Randomness | Done in code, not by the LLM | LLMs are poor at uniform randomness and repeat favourites. |
| Telegram formatting | `parse_mode=HTML` | MarkdownV2 escaping is a reliable source of crashes. |

---

## 4. Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Telegram | `aiogram` 3.x |
| LLM | `anthropic` Python SDK (Messages API, tool use, prompt caching) |
| Web | `fastapi`, `uvicorn`, `jinja2`, HTMX (vendored, no CDN) |
| DB | stdlib `sqlite3` (via a single writer thread) + `sqlite-vec` extension + FTS5 |
| Embeddings | `fastembed` with `intfloat/multilingual-e5-small` (384-d, multilingual; handles English + Chinese, see §6.8) |
| Notes | `python-frontmatter` |
| Scheduling | `APScheduler` (AsyncIOScheduler) |
| Packaging | Docker (`python:3.12-slim`), docker compose on UGOS |

---

## 5. Data model

### 5.1 SQLite configuration

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;   -- safe with WAL; fsync on checkpoint
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

**Time:** all timestamps are stored as UTC ISO-8601 text. Every "day" boundary (daily budget, `unprompted_today`, "today's decisions", nudges) uses the single household timezone from the `TZ` env var. `users.timezone` is only used to render "now" in the prompt.

Concurrency model: one dedicated **writer connection** owned by a single thread (all writes queued through it); a small pool of **read-only connections** for the dashboard and retrieval. CPU-bound work (embedding) runs in a `ThreadPoolExecutor` so the event loop is never blocked.

### 5.2 Schema

```sql
-- Users ---------------------------------------------------------------
CREATE TABLE users (
  id            INTEGER PRIMARY KEY,           -- internal
  telegram_id   INTEGER NOT NULL UNIQUE,
  slug          TEXT NOT NULL UNIQUE,           -- 'jack' | 'partner'
  display_name  TEXT NOT NULL,
  timezone      TEXT NOT NULL DEFAULT 'UTC',
  enabled       INTEGER NOT NULL DEFAULT 1
);

-- Settings (hot-reloaded; dashboard writes, bot reads per request) -------
CREATE TABLE settings (
  key         TEXT PRIMARY KEY,                -- e.g. 'persona.system_prompt', 'models.default'
  value_json  TEXT NOT NULL,
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Decision categories & options ------------------------------------------
CREATE TABLE categories (
  id               INTEGER PRIMARY KEY,
  slug             TEXT NOT NULL UNIQUE,       -- 'dinner', 'movie', 'weekend-activity' (created dynamically)
  display_name     TEXT NOT NULL,
  description      TEXT NOT NULL,              -- one line, used for semantic matching
  embedding        BLOB,                       -- reserved, unused (resolver doesn't embed; see §8.1)
  recency_tau_days REAL NOT NULL DEFAULT 3.0,  -- decay constant (see §8.3); Claude proposes on creation
  default_n        INTEGER NOT NULL DEFAULT 1, -- picks per answer
  allow_generated  INTEGER NOT NULL DEFAULT 1, -- may Claude propose options not in list?
  created_by       TEXT NOT NULL DEFAULT 'bot',-- 'bot' | 'admin' | 'import'
  merged_into      INTEGER REFERENCES categories(id), -- set when merged in dashboard; resolver follows it
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE category_aliases (                 -- phrasings already resolved to a category
  alias        TEXT PRIMARY KEY,                -- normalised: 'lunch', 'makan', '晚餐'
  category_id  INTEGER NOT NULL REFERENCES categories(id)
);

CREATE TABLE options (
  id           INTEGER PRIMARY KEY,
  category_id  INTEGER NOT NULL REFERENCES categories(id),
  name         TEXT NOT NULL,
  tags_json    TEXT NOT NULL DEFAULT '[]',     -- ['spicy','delivery','cheap']
  base_weight  REAL NOT NULL DEFAULT 1.0,
  owner        TEXT NOT NULL DEFAULT 'shared', -- 'jack' | 'partner' | 'shared'
  active       INTEGER NOT NULL DEFAULT 1,
  created_by   TEXT NOT NULL DEFAULT 'admin',  -- 'admin' | 'bot'
  UNIQUE (category_id, name)
);

-- Per-user preference multiplier on an option (learned from accept/reject)
CREATE TABLE option_prefs (
  option_id   INTEGER NOT NULL REFERENCES options(id),
  user_id     INTEGER NOT NULL REFERENCES users(id),
  multiplier  REAL NOT NULL DEFAULT 1.0,       -- clamp [0.1, 3.0]
  PRIMARY KEY (option_id, user_id)
);

-- Decision log ------------------------------------------------------------
CREATE TABLE decisions (
  id           INTEGER PRIMARY KEY,
  category_id  INTEGER NOT NULL REFERENCES categories(id),
  option_id    INTEGER REFERENCES options(id), -- NULL if free-text/generated
  choice_text  TEXT NOT NULL,
  for_users    TEXT NOT NULL,                  -- 'jack' | 'partner' | 'both'
  asked_by     INTEGER NOT NULL REFERENCES users(id),
  status       TEXT NOT NULL,                  -- 'suggested'|'accepted'|'rerolled'|'rejected'
  source       TEXT NOT NULL DEFAULT 'live',   -- 'live' | 'import'
  chat_id      INTEGER,                        -- DM or group it happened in
  tg_message_id INTEGER,                       -- bot message carrying the ✅🎲❌ keyboard
  context_json  TEXT,                          -- random_pick args (for_users, tags, extra_candidates), replayed on 🎲 reroll
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))   -- for imports: original message time
);
CREATE INDEX ix_decisions_cat_time ON decisions(category_id, created_at);

-- Conversation history ------------------------------------------------------
CREATE TABLE messages (
  id          INTEGER PRIMARY KEY,
  chat_id     INTEGER NOT NULL,
  tg_message_id INTEGER,                        -- Telegram message id (NULL for tool rows)
  user_id     INTEGER REFERENCES users(id),     -- NULL for assistant
  role        TEXT NOT NULL,                    -- 'user' | 'assistant' | 'tool' (internal; see §7.2)
  kind        TEXT NOT NULL DEFAULT 'text',     -- 'text'|'sticker'|'photo'|'emoji'|'voice'|'other'
  content     TEXT NOT NULL,                    -- JSON blocks
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_messages_chat_time ON messages(chat_id, created_at);
CREATE UNIQUE INDEX ux_messages_tg ON messages(chat_id, tg_message_id) WHERE tg_message_id IS NOT NULL;

CREATE TABLE chat_summaries (                   -- rolling summary of older turns
  chat_id     INTEGER PRIMARY KEY,
  summary     TEXT NOT NULL,
  upto_msg_id INTEGER NOT NULL
);

-- Second brain index (derived from /data/vault; safe to drop & rebuild) ------
CREATE TABLE notes (
  id          INTEGER PRIMARY KEY,
  path        TEXT NOT NULL UNIQUE,             -- relative to vault root
  owner       TEXT NOT NULL,                    -- 'jack' | 'partner' | 'shared'
  type        TEXT NOT NULL,                    -- 'profile'|'preference'|'place'|'fact'|'log'
  title       TEXT NOT NULL,
  file_hash   TEXT NOT NULL,                    -- sha256 of file bytes
  pinned      INTEGER NOT NULL DEFAULT 0,       -- always injected into prompt (core constraints)
  updated_at  TEXT NOT NULL
);

CREATE TABLE chunks (
  id          INTEGER PRIMARY KEY,
  note_id     INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  ord         INTEGER NOT NULL,
  heading     TEXT,                             -- "Food > Dislikes"
  text        TEXT NOT NULL,                    -- title + heading path + body
  chunk_hash  TEXT NOT NULL,
  embed_model TEXT NOT NULL                     -- model + precision, e.g. 'intfloat/multilingual-e5-small@int8' (§6.9)
);

CREATE TABLE links (                             -- [[wikilinks]] graph
  src_note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  dst_path    TEXT NOT NULL,
  PRIMARY KEY (src_note_id, dst_path)
);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
  text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE chunks_vec USING vec0(
  embedding float[384]                          -- rowid = chunks.id; stays fp32 even with an int8 model (§6.9)
);

-- Memory inbox (implicit memories awaiting approval) ---------------------------
CREATE TABLE memory_inbox (
  id          INTEGER PRIMARY KEY,
  owner       TEXT NOT NULL,
  target_path TEXT NOT NULL,
  content     TEXT NOT NULL,
  reason      TEXT,                              -- what in the chat triggered it
  source      TEXT,                              -- provenance: telegram:<chat>, web:<url> (M7), topic:<name> (M4)  (0004)
  decided_at  TEXT,                              -- (0004)
  decided_by  INTEGER REFERENCES users(id),      -- NULL when auto-approved (0004)
  status      TEXT NOT NULL DEFAULT 'pending',   -- 'pending'|'approved'|'rejected'
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Usage & cost --------------------------------------------------------------------
CREATE TABLE usage (
  id                 INTEGER PRIMARY KEY,
  user_id            INTEGER REFERENCES users(id),
  purpose            TEXT NOT NULL,               -- 'chat'|'judge'|'summary'|'harvest'|'import_extract'|'import_consolidate'
  chat_id            INTEGER,
  import_job_id      INTEGER REFERENCES import_jobs(id),
  model              TEXT NOT NULL,
  input_tokens       INTEGER NOT NULL,
  output_tokens      INTEGER NOT NULL,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  web_search_requests INTEGER NOT NULL DEFAULT 0,  -- M7 (§7.5)
  web_fetch_requests  INTEGER NOT NULL DEFAULT 0,  -- M7
  cost_usd           REAL NOT NULL,
  created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Ambient participation (§10.2) ------------------------------------------------------
CREATE TABLE chat_state (
  chat_id              INTEGER PRIMARY KEY,
  muted_until          TEXT,
  last_unprompted_at   TEXT,
  unprompted_today     INTEGER NOT NULL DEFAULT 0,
  cooldown_multiplier  REAL NOT NULL DEFAULT 1.0,    -- doubled on negative feedback, reset daily
  state_day            TEXT                          -- household-TZ date the daily counters belong to (0003)
);

CREATE TABLE ambient_log (
  id             INTEGER PRIMARY KEY,
  chat_id        INTEGER NOT NULL,
  from_msg_id    INTEGER NOT NULL,                   -- burst range in messages
  to_msg_id      INTEGER NOT NULL,
  action         TEXT NOT NULL,                      -- 'respond' | 'silent' | 'skipped_rule'
  rule           TEXT,                               -- which stage-1 rule fired, if any
  reason         TEXT,
  confidence     REAL,
  feedback       TEXT,                               -- NULL | 'negative' | 'positive'
  reply_tg_message_id INTEGER,                       -- bot message sent for 'respond' (0003), target of 👎/👍
  created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Bootstrap import (§15) ---------------------------------------------------------
CREATE TABLE import_jobs (
  id            INTEGER PRIMARY KEY,
  source        TEXT NOT NULL,                   -- 'telegram_json'
  file_sha256   TEXT NOT NULL UNIQUE,            -- same export can't be imported twice
  since         TEXT NOT NULL,                   -- e.g. now - 6 months
  status        TEXT NOT NULL,                   -- 'parsed'|'extracting'|'consolidating'|'review'|'done'|'failed'
  msg_count     INTEGER, window_count INTEGER,
  cost_usd      REAL NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE import_windows (                    -- unit of work; makes extraction resumable
  id            INTEGER PRIMARY KEY,
  job_id        INTEGER NOT NULL REFERENCES import_jobs(id),
  chat_ref      TEXT NOT NULL,                   -- which exported chat
  start_ts      TEXT NOT NULL, end_ts TEXT NOT NULL,
  text          TEXT,                            -- normalised transcript (nulled out at APPLY)
  status        TEXT NOT NULL DEFAULT 'pending', -- 'pending'|'submitted'|'done'|'failed'
  batch_id      TEXT,                            -- Anthropic Message Batches id
  result_json   TEXT                             -- extracted items
);

CREATE TABLE import_items (                      -- consolidated proposals awaiting review
  id            INTEGER PRIMARY KEY,
  job_id        INTEGER NOT NULL REFERENCES import_jobs(id),
  kind          TEXT NOT NULL,                   -- 'note'|'category'|'option'|'decision'
  payload_json  TEXT NOT NULL,
  evidence_json TEXT NOT NULL,                   -- [{window_id, ts, quote}]
  confidence    REAL NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending'  -- 'pending'|'approved'|'rejected'
);

CREATE TABLE schema_version (version INTEGER NOT NULL);
```

Migrations: numbered `.sql` files applied at startup inside a transaction, tracked by `schema_version`.

---

## 6. Second brain

### 6.1 Vault layout

The vault is bot-owned. Humans view/edit it only through the dashboard, so every write goes through `NoteStore`.

```
/data/vault/
├── people/
│   ├── jack.md            # pinned: hard constraints (allergies, strong dislikes, diet)
│   └── partner.md         # pinned
├── shared/
│   ├── household.md       # shared constraints, budget norms, kitchen equipment
│   ├── places/            # restaurants, hawker stalls, etc.
│   │   └── <slug>.md
│   └── topics/            # cuisines, shows, activities
├── memories/
│   ├── jack/<slug>.md     # facts learned about Jack
│   └── partner/<slug>.md
└── logs/
    └── 2026/10/2026-10-01.md   # human-readable mirror of decisions (DB is authoritative; NOT indexed)
```

### 6.2 Note format

```markdown
---
id: 01JABCXYZ...            # ULID
owner: jack                 # jack | partner | shared
type: preference            # profile | preference | place | fact | log
tags: [food, spicy]
pinned: false
source: telegram:msg/12345  # provenance
created: 2026-10-01T19:02:00+08:00
updated: 2026-10-01T19:02:00+08:00
---
# Spice tolerance

Jack likes spicy food but not numbing (mala) spice. See [[shared/topics/sichuan]].
```

### 6.3 Write path (`NoteStore`)

1. Validate path (must be under vault root, allowed folder for the operation, `.md`).
2. Render frontmatter + body; write to `path.tmp`, `fsync`, `rename()` (atomic).
3. Parse → chunk → diff `chunk_hash` against DB → embed only new/changed chunks (thread pool).
4. In one SQLite transaction: upsert `notes`, replace `chunks`/`chunks_fts`/`chunks_vec`/`links` for that note.
5. Mark the vault dirty for the nightly git commit (M6; until then the nightly job simply commits whatever changed).

`write_note` modes: `create` (fails if the note exists), `append` (optionally under a heading, created as `##` if missing), `replace_section` (rewrite one section: contradictions), `replace` (rewrite the body, keeping the title: forgetting a line). A new note gets a ULID, owner and type from its path (`people/` → profile, `shared/places/` → place, otherwise fact), and a readable title from its filename unless one is given. Only notes under `people/<user>.md`, `memories/<user>/`, `shared/household.md`, `shared/places/` and `shared/topics/` are writable by Claude; `logs/` is internal.

`logs/` is written through `NoteStore` but excluded from the index (steps 3–4 skipped), so decision logs never crowd out real memories in retrieval.

**Startup reconcile:** walk the vault, compare `file_hash`; reindex changed files, delete index rows for missing files. **Model change:** if `embed_model` ≠ configured model (including a precision change, §6.9), full reindex (a few thousand chunks ≈ about a minute on this CPU).

### 6.4 Chunking

- Split on markdown headings (`#`–`###`); merge tiny sections; hard cap ~512 tokens per chunk.
- Prefix each chunk with `"{title} > {heading path}\n"` so it's self-describing.
- Most notes will be a single chunk; that's fine.

### 6.5 Retrieval (`search_memory`)

```
input: query, asker, scope ('me'|'partner'|'both'|'shared'), k=6
       scope 'partner' = every user except the asker; default scope: group → 'both', DM → 'me' 

1. owner_filter = {asker_slug, 'shared'}          (+ partner's slug if scope == 'both')
2. fts  = top 30 from chunks_fts MATCH query       → join chunks/notes, filter owner, rank by bm25
3. vec  = top 30 from chunks_vec KNN(embed(query)) → join, filter owner
4. fused score per chunk = Σ 1 / (60 + rank_i)     (Reciprocal Rank Fusion)
5. take top k; add 1-hop [[link]] neighbours of their notes (max 3, owner-filtered, lower score)
6. return [{path, title, heading, snippet, score}]
```

KNN over-fetches (30) and filters by owner afterwards; at this data size that's simpler than vec0 metadata partitions and costs nothing.

### 6.6 Pinned context

Notes with `pinned: true` (people profiles, household constraints) are **always** injected into the system prompt. Safety-relevant facts (allergies) must never depend on retrieval recall.

**Scope (M3):** every user's pinned notes plus shared ones go into every prompt, DMs included, and into the speak-or-stay-silent judge's context. A DM decision is often "for both", and those are exactly the constraint-type notes §12 allows across users. Capped at `memory.pinned_max_chars` (default 6000), with a warning log when truncated. `avoid_tags` from `people/<user>.md` are shown alongside and enforced in code (§8.2).

**Reading other people's notes:** `read_note` in the group can read every note; in a DM it can read the asker's and shared notes, plus everyone's pinned `people/*.md` profiles.

### 6.7 Memory write policy

| Trigger | Behaviour |
|---|---|
| Explicit ("remember…", "note that…") | Claude calls `write_note` → written immediately, confirmed in chat. |
| Implicit (Claude infers a durable fact) | Claude calls `propose_memory(owner, content, reason, topic)` → row in `memory_inbox` targeting `memories/<owner>/<topic>.md` (or `shared/topics/<topic>.md`); approve/reject in dashboard (M4) or, until then, with the admin's `/inbox` command in Telegram (✅ Save / ❌ Drop buttons, admin only). Approving appends `- <content>` to the target note via `NoteStore`. Setting `memory.auto_approve` (default **off**) skips the inbox, except for items that must be reviewed (web facts, §7.5). |
| Contradiction | Claude must `read_note` first and update in place (`replace_section`), never append a contradicting line. |
| Forget ("forget that I…") | Claude edits/removes the line; dashboard can delete whole notes. |

Pinned notes can only be modified via the dashboard or an explicit user request, never implicitly.

### 6.8 Language handling (mainly English, some Singlish, a little Chinese)

| Concern | Handling |
|---|---|
| Embedding model | `multilingual-e5-small` puts English and Chinese in the same vector space, so "想吃辣的" can match a note saying "likes spicy food". Same 384-d as before; no schema change. |
| e5 prefixes | e5 models expect `"query: "` on search inputs and `"passage: "` on indexed chunks. Applied inside the `Embedder`; forgetting this noticeably degrades recall. |
| FTS5 and Chinese | `unicode61` doesn't segment CJK (no spaces), so a run of Chinese characters becomes one token and keyword search barely helps. Accepted trade-off: Chinese recall comes from the vector side of the hybrid search. If that proves weak, add a second `fts5(..., tokenize='trigram')` table as a third RRF input. |
| Singlish | Claude understands Singlish well, so no special handling in conversation. Retrieval is helped in two ways: (1) the `search_memory` tool description tells Claude to write queries in plain English **plus** any original local terms (e.g. `"takeaway food tapao"`); (2) notes are written in plain English with local terms kept inline (e.g. "Prefers to tapao (takeaway) on weekdays"), so both FTS and vectors match. |
| Reply language | Persona rule: reply in the language/register the user used; default English. |

### 6.9 Embedding precision (int8 vs fp32) — decided in M3

The ONNX model can run with fp32 weights (4 bytes/value, the trained reference) or int8 weights (1 byte/value plus a per-tensor/per-channel scale; lossy, made after training). Rough numbers for `multilingual-e5-small` (~118M params, most of them in the multilingual vocab), to be confirmed against the actual files:

| | fp32 | int8 |
|---|---|---|
| Model file | ~470 MB | ~120 MB |
| Runtime RAM (rough) | ~500–700 MB | ~150–300 MB |
| CPU speed | baseline | often 1.5–3× faster (the 8505 has AVX-VNNI) |
| Quality | reference | typically ~1–2% lower on retrieval benchmarks, sometimes more |

What matters here:

- **Memory decides it.** The container gets ~1 GB (N2, §2.2) shared with Python, aiogram, the SDK, SQLite and the dashboard. fp32 plus the ONNX Runtime arena can eat over half of that; int8 leaves headroom.
- **Speed doesn't.** Two users produce a handful of embeddings a minute; both variants are fast enough.
- **Quality is the risk.** Small multilingual models lose more to quantization than English benchmarks suggest (EN↔ZH matching like "想吃辣的" ↔ "likes spicy food"), and e5 cosine scores sit in a narrow band (§8.1 measured 0.877–0.892), so quantization noise can swap ranks. RRF fusion with BM25 (§6.5) softens this because it's rank-based.
- **Never mix variants.** Queries and stored chunks must come from the same model *and* precision. `embed_model` records both (`<model>@int8` / `<model>@fp32`); a mismatch triggers the full reindex in §6.3.
- **Vector storage stays fp32.** sqlite-vec's `int8[384]` saves 1,152 bytes per chunk, which is irrelevant at this scale (10k chunks ≈ 15 MB as fp32). Keep `float[384]`.

**Default: int8 model weights, fp32 vector storage.** Precision is a seeded setting (`embedding.precision`, `int8` | `fp32`) so it can be flipped without code changes; whichever variant is configured must be the one baked into the image.

**M3 acceptance check before locking the default:** a small offline eval (≈ 20 realistic notes mixing English, Singlish and Chinese; ≈ 15 queries with known answers) run through both variants, reporting recall@5 and peak RSS. If int8 misses a hit that fp32 finds, switch the default to fp32 and budget the RAM for it. Record the result here.

**Result (M3, `scripts/embedding_eval.py`, 20 notes / 15 queries incl. Chinese and Singlish):**

| | int8 | fp32 |
|---|---|---|
| Vector-only: recall@1 / recall@5 / MRR | 14/15 · 15/15 · 0.950 | 14/15 · 15/15 · 0.950 |
| Hybrid: recall@1 / recall@5 / MRR | 14/15 · 15/15 · 0.967 | 14/15 · 15/15 · 0.967 |
| Peak RSS, eval process (arm64 dev) | 519 MB | 916 MB |
| Model files | 130 MB | ~470 MB |

Identical ranks on every query (same two rank-2/rank-4 misses for both). **int8 is locked as the default.** In the amd64 image, offline, as uid 1000: model load ~2 s, peak RSS ~580 MB including indexing and a search, within the 1 GB limit. `embedding.precision` is read at startup (the model is loaded once), so changing it needs a restart; fp32 isn't baked into the image, so it's downloaded into `/data/models` on first start (or build with `--build-arg EMBED_PRECISION=fp32`). If the model can't load at all, the bot still runs: notes are written, chunks are marked `pending`, and search falls back to keywords until the next reconcile.

---

## 7. Claude integration

### 7.0 LLM provider: Claude API only

**Decided:** every LLM call at runtime goes through the **Anthropic Claude API** (Messages API + Message Batches API) using one `ANTHROPIC_API_KEY` from the Claude Console, billed pay-as-you-go. No Claude subscription, Claude Code or Agent SDK login is used at runtime. Claude Code is used only as a development tool to build Tykee.

| Component | API | Default tier | Section |
|---|---|---|---|
| Orchestrator (replies, tool use, memory proposals) | Messages API, tool use, prompt caching | Haiku-tier, escalates to Sonnet-tier | §7.1–7.4 |
| Speak-or-stay-silent judge | Messages API, structured JSON output, prompt caching | Haiku-tier | §10.2 |
| Chat history summaries | Messages API | Haiku-tier | §7.2 |
| Bootstrap import: extraction | **Message Batches API** (async, discounted) | **Opus-tier** (one-off; quality sets the bot's day-one memory) | §15.3 |
| Bootstrap import: consolidation | Messages API | **Opus-tier** | §15.3 |
| Embeddings, random pick | **None**: local `fastembed` + code | n/a | §6, §8 |
| Category resolver | Aliases/slugs in code; first-time phrasings judged by the orchestrator's own Claude call (no extra request) | Haiku-tier | §8.1 |
| Web search / web fetch (M7) | Anthropic **server-side tools** on the orchestrator's Messages API call; **never** on judge, summary or import calls | Same as orchestrator | §7.5 |

Client rules:
- A single `LLMClient` wrapper around the official `anthropic` SDK (`AsyncAnthropic`) is the only code allowed to call the API. It applies model selection from `settings`, budget checks before the call, `usage` recording after it, timeouts (30 s interactive, 120 s consolidation), and retries with exponential backoff on 429/5xx/overloaded (max 2 for interactive paths).
- Model IDs are never hardcoded in code paths; they come from `settings` (`models.default`, `models.escalated`, `models.judge`, `models.import_extract`, `models.import_consolidate`). The only place model IDs appear is the settings seed data, written from current Anthropic docs at development time and inserted on first run for missing keys (import keys default to the current Opus model).
- The price table used for cost tracking lives in `settings` (`pricing.<model>`), editable in the dashboard.
- If `ANTHROPIC_API_KEY` is missing or invalid at startup, the bot still starts in fallback mode (§8.5) and the dashboard shows a red health tile.

### 7.1 Model routing (all configurable in dashboard)

| Task | Default tier | Notes |
|---|---|---|
| Normal chat & decisions | Haiku-tier | Cheap, fast; most traffic. |
| Complex / multi-constraint planning (e.g. "plan our Saturday") | Sonnet-tier | Escalated by keyword/length heuristic or a `/think` command. |
| History summarisation | Haiku-tier | Background. |

Model IDs live in `settings` (`models.default`, `models.escalated`), never hardcoded.

### 7.2 Prompt assembly (in cache-friendly order)

```
system:
  [1] persona + behaviour rules              (settings: persona.system_prompt)   ┐ cache_control
  [2] tool usage rules & output format rules                                     │ (static, rarely changes)
  [3] pinned notes for the user(s) in scope                                      ┘ cache_control
  [4] dynamic context: now (user TZ), who is asking, chat type, today's decisions
messages:
  [5] chat summary (if any) + last N turns (default 12)
  [6] current user message
```

Prompt caching on [1]–[3] keeps per-message cost low because they're identical across turns.

**History replay:** tool calls and results are persisted (`role='tool'`, for the dashboard's conversation view) but **not replayed**. Only user text and the assistant's final text go into [5]: cheaper, and no risk of orphaned `tool_use`/`tool_result` pairs. The API has no `tool` role; it's an internal label. In groups, consecutive messages from users are collapsed into one user turn with speaker prefixes (`[jack] …\n[partner] …`).

**Rolling summary:** once at least `summary.batch` (default 20) messages have fallen out of the last-N replay window, they are folded into `chat_summaries` together with the previous summary (one `models.default` call, `purpose='summary'`, ≤ 150 words). It runs in the background after replies and judge calls, at most once at a time per chat, so it never adds reply latency. The summary is prepended to the first replayed user turn ([5]) and given to the judge.

### 7.3 Tools exposed to Claude

| Tool | Purpose | Key params |
|---|---|---|
| `resolve_category` | Map a free-text decision type to a canonical category, creating one if new (§8.1) | `phrase`, `proposed_slug`, `description`, `proposed_tau_days`, `use_existing?`, `create_new?` |
| `random_pick` | Weighted random pick(s) via Decision Engine | `category` (slug from `resolve_category`), `n`, `for_users`, `include_tags`, `exclude_tags`, `extra_candidates[]` |
| `list_options` | View the option list for a category | `category` |
| `add_option` | Add a new option (e.g. a new restaurant) | `category`, `name`, `tags` |
| `recent_decisions` | What was chosen recently | `category`, `days` |
| `search_memory` | Hybrid search over second brain | `query`, `scope`, `k` |
| `read_note` | Full note content | `path` |
| `write_note` | Create / append / replace section (explicit memories) | `path`, `mode`, `content`, `heading?` |
| `propose_memory` | Queue an implicit memory for approval | `owner`, `content`, `reason` |
| `web_search` *(server tool, M7)* | Live web search run by Anthropic; results come back with citations | configured via settings (§7.5), not by Claude |
| `web_fetch` *(server tool, M7)* | Fetch a specific URL (e.g. a restaurant page from search results or a link a user pasted) | configured via settings (§7.5) |

Tool loop: max 6 tool iterations per user message; on limit, answer with what's available. Server-side web tool calls happen inside a single API response and don't count toward this limit; they're capped separately (§7.5).

### 7.5 Web access (M7)

Tykee can look things up online using the Claude API's built-in **web search** and **web fetch** server tools. Anthropic runs the search and returns results with citations, so there is no search provider to integrate or API key to manage beyond `ANTHROPIC_API_KEY`. Check current Anthropic docs for the exact tool type/version strings and parameters; store them in settings rather than hardcoding.

**Typical uses:** "is that new ramen place in Tanjong Pagar any good?", "what's showing at the cinema tonight?", "is X open on Mondays?", opening a link one of you pasted in the group.

**Where it's enabled**

| Call | Web tools? | Why |
|---|---|---|
| Orchestrator (replies) | **Yes**, when `web.enabled` | The only place a person is asking for an answer. |
| Speak-or-stay-silent judge | **No** | Runs on every burst; must be fast and cheap. |
| Chat summaries | **No** | No need. |
| Bootstrap import | **No** | Extracts from your own chats only. |

**Settings (dashboard → Behaviour → Web)**

| Key | Default | Purpose |
|---|---|---|
| `web.enabled` | `true` once M7 ships | Master switch. |
| `web.search_max_uses` | 3 | Max searches per orchestrator call (passed as the tool's max-uses parameter). |
| `web.fetch_max_uses` | 2 | Max page fetches per orchestrator call. |
| `web.daily_search_cap` | 50 | Hard cap across both users; when hit, web tools are dropped from the tool list until midnight (user TZ). |
| `web.user_location` | Singapore (city/country/timezone) | Localises search results. |
| `web.allowed_domains` / `web.blocked_domains` | empty | Optional allow/block lists. |
| `web.tool_versions` | from current docs | Tool type strings, never hardcoded in code paths. |

**Behaviour rules (added to persona)**
- Check the second brain first; search the web only when the answer depends on live or external facts (opening hours, reviews, showtimes, prices, events).
- Keep it short in Telegram: one-line answer + at most 1–2 source links (HTML `<a>`), not a research report.
- Don't search for things the user didn't ask about just to "enrich" a pick.

**Memory safety rule:** facts found on the web are **never written to the vault automatically**. If Claude thinks a web fact is worth keeping (e.g. "Ramen X closed permanently"), it calls `propose_memory` with `source: web:<url>`. The item goes to the inbox **regardless of `memory.auto_approve`**, and one of you must confirm it. This protects the second brain from prompt injection and from stale or wrong web content.

**Prompt injection handling:** web results are untrusted data. The persona states that instructions found in web content are never followed. Web tools can't write anything (only `write_note`/`propose_memory` can, and those rules above apply), and pinned notes can't be changed implicitly (§6.7).

**Usage & cost:** the `usage` row records `web_search_requests` and `web_fetch_requests` from the response's usage block. Searches are billed per search on top of tokens (price per search in `settings.pricing.web_search`, from current pricing docs). Search results add input tokens, so a web-assisted reply costs noticeably more than a normal one. Both count toward the daily/monthly budget caps; when the budget hits 80%, web tools are disabled first, before any other degradation.

**Failure handling:** if a web tool returns an error (rate limit, fetch blocked, unavailable), Claude answers from memory/general knowledge and says it couldn't check live info. Never retry in a loop.

### 7.4 Behaviour rules (default persona excerpt)

- Make a decision; don't hedge. Default to **one** pick + one-line reason unless asked for options.
- Always call `random_pick` for choices within a known category, never "pick randomly" yourself.
- Respect pinned constraints for everyone in `for_users`.
- Ask at most one clarifying question, and only if the answer changes the pick materially.
- Keep replies short; Telegram, not email.

---

## 8. Decision engine

### 8.1 Dynamic categories

There is no fixed category list. Claude decides a message is a decision request, then calls `resolve_category` with the user's phrase and its proposed slug/description. Known phrasings resolve in deterministic code; a phrasing seen for the first time is judged once by Claude against the list of existing categories, and the answer is stored as an alias so it is deterministic from then on:

```
resolve_category(phrase, proposed_slug, description, proposed_tau_days, use_existing?, create_new?):
  0. use_existing: slug must exist (follow merged_into)                     → return it, add aliases
  1. alias hit:    normalise(phrase) or normalise(proposed_slug) in category_aliases → return it
  2. exact slug:   slugify(proposed_slug) in categories (follow merged_into)  → return it, add alias
  3. create_new, or no categories exist yet                                  → step 5
  4. choose:       return {status:'choose', categories:[{slug, display_name, description, uses}]}
                   Claude calls again with use_existing=<slug> (same kind of decision)
                   or create_new=true (a different kind)
  5. create:       new category(slug, description, tau = clamp(proposed_tau_days, 0.5, 365),
                   created_by='bot'), add aliases; if slug already exists, return that instead
```

**Why not embeddings (v1.9):** measured on `multilingual-e5-small`, cosine scores for unrelated categories overlap with correct matches (`board-game`→`dinner` 0.877 and `drinks`→`gift` 0.892 vs. `film`→`movie` 0.885 and `makan`→`dinner` 0.878), so no fixed threshold works. Choosing among a few dozen described categories is a reading task the orchestrator model does well, costs ~1k tokens only on a phrasing's first appearance, and needs no extra API call because it happens inside the existing tool loop.

- Aliases are normalised (NFKC, casefold, punctuation stripped, whitespace collapsed) and capped at 40 chars.
- **Sprawl control:** the dashboard shows new categories with usage counts and supports **merge** (sets `merged_into`, repoints options/decisions/aliases) and rename.
- **Empty categories:** a new category has no options. Claude supplies `extra_candidates` from memory + general knowledge. A candidate that gets ✅ accepted is persisted as an option (`created_by='bot'`), so option lists grow organically from real use.
- Typical τ Claude should propose: meals 2–4 days, snacks/drinks 1 day, movies/shows 14–30 days, weekend activities 7–14 days, trips 90+ days.

### 8.2 Candidate set

1. Active `options` for the category, owner ∈ scope (`for_users='both'` → everyone + shared; a single user → that user + shared).
2. Filter by `include_tags` (option must have **all** of them) / `exclude_tags` (must have **none**) and hard constraints: option tags vs. the union of `avoid_tags` for every user in `for_users`. `avoid_tags` is a structured frontmatter list on `people/<slug>.md` (e.g. `avoid_tags: [contains:peanut, contains:coriander]`), parsed by code, so allergy filtering never depends on the LLM.
3. Optionally union `extra_candidates` proposed by Claude (if `allow_generated`), each with weight 1.0. A candidate whose name matches any option in the category (active or not, casefolded) is dropped, so a filtered-out or deactivated option can't sneak back in. The tool schema **requires** `tags` on each candidate so the same constraint filter applies to them.

`avoid_tags` are applied in code on every pick, including 🎲 rerolls (re-read at reroll time), on top of whatever `exclude_tags` Claude passes; `random_pick` reports them back as `excluded_by_constraints`. Claude sets them with `write_note(..., add_avoid_tags=["contains:coriander"])` on `people/<user>.md` (only there). They only help for options tagged accordingly, so the persona asks Claude to tag allergens and key ingredients as `contains:<x>`, and pinned notes remain the second line of defence.

### 8.3 Weighting

For candidate *i*:

```
w_i = base_weight_i × pref_i × recency_i

pref_i     = Π over users in for_users of option_prefs.multiplier   (default 1.0)
recency_i  = 1 − exp(−Δt_i / τ)        Δt_i = days since last 'accepted' pick of i (∞ if never → 1.0)
τ          = categories.recency_tau_days (dinner default 3.0)
```

Examples with τ = 3: picked yesterday → 0.28; 3 days ago → 0.63; a week ago → 0.90.

Sampling: `random.SystemRandom().choices(candidates, weights, k=1)`, repeated without replacement for `n > 1` (n capped at 5). If every remaining weight is 0 (everything picked moments ago), sample uniformly. Options rerolled or rejected in the *current* conversation get weight 0, where "current conversation" = same `chat_id` and category within the last `decisions.session_hours` (default 6).

Recency for generated candidates (`option_id` NULL) matches past decisions on casefolded `choice_text`.

### 8.4 Learning from feedback

| Action | Effect |
|---|---|
| ✅ accepted | `decisions.status='accepted'`; `pref × 1.05` (clamped ≤ 3.0); bot posts "✅ X it is"; log mirrored to `logs/` (from M3, once `NoteStore` exists) |
| 🎲 reroll | `status='rerolled'`; no pref change; re-run pick from `context_json` excluding it, posted as a templated message with fresh buttons (no Claude call) |
| ❌ not this | `status='rejected'`; `pref × 0.85` (clamped ≥ 0.1) |

Preference updates apply only to the user who tapped, even when `for_users='both'`. Callbacks are allowlist-checked like messages. Only a decision still in `suggested` can be acted on, so a double tap (or both people tapping) is a no-op. Accepting a generated candidate persists it as an option first.

### 8.5 Fallback

If Claude is unavailable or the budget is exhausted: the bot matches the message against `category_aliases` and category slugs (whole-word match on the normalised text, longest wins), calls `random_pick` directly with the chat's default `for_users`, and replies with a plain templated message and buttons. No alias match → "my brain's offline, try `/pick <category>`". If Claude fails *after* `random_pick` already ran in that turn, the pick is still sent (templated) with its buttons.

`/pick <category>` and `/options <category>` resolve by alias/slug only (never create) and never call Claude, so they work in fallback mode.

---

## 9. Request flow

```
User (Telegram) ─ "what should we eat tonight?"
  │
  ├─ Adapter: allowlist check → persist message → typing indicator
  ├─ Orchestrator: load settings, pinned notes (both users), last N turns
  ├─ Claude: tool_use search_memory("dinner tonight preferences", scope=both)
  ├─ Claude: tool_use recent_decisions("dinner", 7)
  ├─ Claude: tool_use resolve_category("eat tonight", proposed_slug="dinner", ...) → "dinner" (alias hit)
  ├─ Claude: tool_use random_pick("dinner", n=1, for_users=both, exclude_tags=["heavy"])
  │     └─ Engine: filter → weight → sample → insert decisions(status='suggested')
  ├─ Claude: final text "Thai basil chicken from X — you haven't had Thai in 9 days and it's raining."
  ├─ Adapter: send HTML message + inline keyboard [✅ 🎲 ❌] (callback_data = decision_id)
  └─ Usage row recorded
User taps ✅ → callback → status='accepted', pref update, log note appended
```

---

## 10. Telegram specifics

- **Allowlist:** `ALLOWED_TELEGRAM_IDS` env (`<id>:<slug>,<id>:<slug>`, first entry is the admin) seeds `users`; any other `from.id` is dropped before any processing or API call. This applies to messages, edits, callbacks and reactions.
- **Chats:** the shared group is primary; DMs are secondary.

  | | Shared group (primary) | Private DM (secondary) |
  |---|---|---|
  | Bot sees | **Every message** (privacy mode **off**, see §10.1) | Every message |
  | Bot replies | When it judges it should (§10.2) | Always |
  | Default `for_users` | `both`, unless message says "just me/for me" | The sender, unless message says "we/us/both/together" |
  | History | Per `chat_id` (DM context never shown in the group) | Per `chat_id` |
  | Memory | Shared second brain across chats; owner filters apply as usual | Same |
  | Allowed group | Only the one group whose `chat.id` is `GROUP_CHAT_ID` (env); the bot leaves any other group it's added to. If `GROUP_CHAT_ID` is unset and the admin adds the bot to a group, it posts the chat id (to put in `.env`) and leaves. A group→supergroup upgrade (`migrate_to_chat_id`) is followed by updating the stored id and logging a warning to update `.env`. | |

  Claude can override the default `for_users` when context is clear; the default only applies when ambiguous.
- **Formatting:** `parse_mode=HTML`. Claude writes plain text with a minimal markdown subset (`**bold**`, `_italic_`, `[text](url)`); code escapes `<`, `>`, `&`, converts that subset to HTML and splits at 4096 chars on the plain text first, so tags are always balanced. Claude never emits HTML.
- **Single poller:** exactly one replica; a second instance causes `409 Conflict` from `getUpdates`. Compose `deploy.replicas` not used; documented in runbook.
- **Commands:** `/pick <category>`, `/options <category>`, `/remember <text>`, `/forget <text>`, `/think <question>`, `/quiet [duration]`, `/unquiet`, `/inbox` (admin, M3: review pending memories), `/settopic` (admin, M4: make this topic the answer topic, §10.4), `/help`. `/remember` and `/forget` are passed to Claude as ordinary messages; the rules tell it to use `write_note`.

### 10.1 Seeing all group messages

By default Telegram bots in groups only receive commands, @mentions and replies ("privacy mode"). To let the bot follow the conversation:

- **Recommended:** make the bot a **group admin**. That bypasses privacy mode *and* is required for Telegram to deliver `message_reaction` updates (needed for 👎 feedback, §10.2); `allowed_updates` must include `message_reaction`.
- Alternative (no reaction feedback): BotFather → `/setprivacy` → **Disable**, then **remove and re-add** the bot to the group (the setting only applies on join).
- Every group message from an allowlisted user is persisted to `messages`, whether or not the bot replies. This is the context the bot reads when it does step in.

### 10.2 Speak-or-stay-silent (ambient participation)

The bot behaves like a quiet friend in the chat: it listens, and only speaks when it adds something. Silence is the default.

**Stage 1: hard rules (code, no LLM call)**

| Situation | Action |
|---|---|
| @mention, reply to a bot message, or `/command` | **Always respond** (skips stage 2 and the cooldown, even when muted); cancels any pending burst |
| `ambient.enabled` is false | Silent (rule `disabled`) |
| Group is muted (`/quiet`, or a mute phrase such as "bot shh") | Silent until mute expires (still logs messages) |
| Unprompted cooldown active (bot spoke unprompted < `ambient.cooldown_min` × `cooldown_multiplier`, default 20 min, ago) | Silent |
| Daily unprompted cap reached (`ambient.max_per_day`, default 5) | Silent |
| The burst contains only stickers/photos/emoji/voice | Silent (rule `media_only`) |

Rules are evaluated in that order when the debounce timer fires (not per message), so the state at judging time counts. Every burst produces one `ambient_log` row: `skipped_rule` (with the rule name; also `budget` and `judge_error`), `silent`, or `respond`. Ambient participation applies to the group only; DMs always get a reply.

**Stage 2: debounce, then a cheap judgement call**

1. **Debounce:** people type in bursts, so wait until the group has been quiet for `ambient.debounce_s` (default 30 s) and judge the whole burst at once, not every line. Pending bursts live in memory; a restart drops them (the next message starts a new burst).
2. **Judge:** one Haiku-tier call (`models.judge`, structured output via `output_config.format`) with the last `ambient.window_messages` (default 20) group messages, a `--- new ---` marker before the burst, the rolling chat summary, today's decisions, and (from M3) pinned constraint notes. It returns:
   ```json
   {"action": "respond" | "silent", "reason": "string", "confidence": 0.0}
   ```
3. Respond only if `action == "respond"` **and** `confidence ≥ ambient.threshold` (default 0.75). Then run the normal orchestrator flow (§9) to produce the reply, with the judge's reason added to the dynamic context ("nobody mentioned you; you chose to step in because…"). The reply is not quoted (no `reply_to`). If the orchestrator can't produce a real reply (Claude down, budget), nothing is sent: an unprompted "my brain's offline" is worse than silence. Invalid judge output is treated as silence.

**Judge guidelines (in its prompt, editable in dashboard):**

| Step in when | Stay silent when |
|---|---|
| Explicit indecision: "idk", "anything lah", "you decide", "what to eat ah" | Chit-chat, jokes, logistics ("reaching in 5") |
| Going back and forth on a choice for several messages with no resolution | A decision was already made ("ok let's do ramen") |
| A suggestion conflicts with a known constraint (e.g. allergy) | Personal, emotional or relationship conversations |
| A factual question the second brain can answer ("what was that place we liked in Tiong Bahru?") | Anything it would only add a "+1" or small talk to |
| | When unsure. Silence is the safe default |

**Feedback loop:**
- Every judge decision is logged (`ambient_log` table: message window, action, reason, confidence) and viewable in the dashboard, so thresholds can be tuned against what actually happened.
- If someone reacts 👎 to an unprompted bot message, or says "not now"/"didn't ask" within `ambient.negative_window_min` (default 15) of one, that's logged as a false positive and the cooldown doubles for the rest of the day (once per unprompted message). 👍 is logged as `positive`.
- A mute phrase ("bot shh", "bot keep quiet", …) or `/quiet [duration]` mutes; `/unquiet` lifts it. Default duration `ambient.default_mute_min` (120); `/quiet` accepts `30m`, `2h`, `1d`, bare minutes; capped at 7 days.
- Negative and mute cues are matched **in code** (normalised whole-phrase match against the editable lists `ambient.negative_phrases` / `ambient.mute_phrases`), not by Claude: they must work even when Claude is unavailable, and they cost nothing.

**Cost:** the judge runs once per burst, not per message, on Haiku-tier with the static prompt prefix cached. For a couple's group chat this is plausibly tens of calls per day, i.e. cents. It is still counted in `usage` and the budget caps; when the budget is exhausted ambient mode switches off and only direct mentions get (fallback) answers.

**Implicit memories from group chatter:** since the bot now reads everything, durable facts mentioned in passing ("I'm off seafood this month") can be proposed via `propose_memory`. They go to the inbox for approval (§6.7), never straight into the vault.

### 10.3 Scheduled nudges (optional, off by default)

A nudge means the bot **starts** a conversation without anyone asking, on a schedule. For example, posting "Dinner tonight? I'm thinking Thai, you haven't had it in 9 days 🎲" in the group at 17:30 on weekdays.

- Configured in dashboard: time, days, category, target chat.
- Skipped automatically if a decision for that category was already made today, or the group is muted.
- Off by default, since the group is meant to be used on demand.
- When an answer topic is configured (§10.4), nudges are posted into that topic (`message_thread_id` set explicitly).

### 10.4 Forum topics: read everywhere, answer in one topic (M4)

The group has **Topics** enabled. Tykee **reads and learns from every topic**, but only **speaks in one answer topic** (initially **#Tykee**), which the admin can change from the dashboard at any time. Telegram has no per-topic bot permission (member permissions are group-wide, and as a group admin the bot receives updates from every topic), so this split is enforced in Tykee's code.

**How Telegram identifies topics:** in a forum supergroup, messages in a topic carry `message_thread_id` (the topic's id) and `is_topic_message = true`. Messages in the built-in **General** topic may arrive without a `message_thread_id`; Tykee stores those as `thread_id = 1` (General's id) so they still belong to a topic. Using a dedicated #Tykee topic as the answer topic avoids that ambiguity.

#### Read vs. answer

| | Answer topic (#Tykee) | Other topics | Ignored topics (optional) |
|---|---|---|---|
| Persisted to `messages` (with `thread_id`) | Yes | Yes | **No** (dropped at the gate) |
| Speak-or-stay-silent judge (§10.2) | **Yes**, only here | No | No |
| Tykee replies / decision keyboards / nudges | **Yes**, only here | Never (see off-topic mentions) | Never |
| Builds memory | Yes, via the orchestrator's own `propose_memory` / `write_note` (§6.7) | Yes, via the **memory harvester** below | No |
| Observed decisions feed recency (§8.3) | Yes (live decisions) | Yes, via the harvester | No |

- **Off-topic commands and @mentions** are handled per `telegram.off_topic_mention`: default **`ignore`**, or `redirect` = reply once with "Ask me in #Tykee 👋", rate-limited to once per topic per day.
- DMs are unaffected.

#### Settings (all dashboard-editable, Users → Telegram)

| Key | Default | Purpose |
|---|---|---|
| `telegram.answer_topic_id` | NULL | The only topic Tykee may speak in. NULL = pre-M4 behaviour (speaks anywhere in the group), shown as a warning tile. Optional env `GROUP_TOPIC_ID` seeds it on first run. |
| `telegram.ignored_topic_ids` | `[]` | Topics Tykee must not read at all (e.g. a private topic). Dropped at the gate: never stored, never sent to Claude. |
| `telegram.off_topic_mention` | `ignore` | `ignore` \| `redirect`. |
| `harvest.enabled` | `true` | Memory harvester on/off. |
| `harvest.interval_min` | 30 | How often the harvester runs. |
| `harvest.min_new_messages` | 5 | Skip a topic until it has this many new messages (or the oldest unharvested one is > 6 h old). |

**Changing the answer topic from the dashboard:** Users → Telegram shows a **dropdown of known topics** (name, message count, last activity) and the current answer topic. Selecting a new one takes effect immediately (settings are hot-reloaded); Tykee posts a one-line "I'll hang out here now 👋" in the new topic. `/settopic` sent by the admin inside a topic does the same from Telegram. Only the admin (first user in `ALLOWED_TELEGRAM_IDS`) can change it.

**Learning topic names:** a `forum_topics` table is filled from `forum_topic_created` / `forum_topic_edited` / `forum_topic_closed` / `forum_topic_reopened` service messages, and from the topic-creation message that topic messages reference via `reply_to_message` where Telegram includes it. Topics whose name is unknown are shown as "Topic <id>" until a name is seen; the admin can label them in the dashboard.

#### Gate order (per update)

1. Allowlist + allowed group (§10).
2. `thread_id` in `ignored_topic_ids` → drop.
3. Persist message with `thread_id`.
4. `thread_id == answer_topic_id` (or `answer_topic_id` is NULL) → normal flow: commands, mentions, debounce + judge (§10.2).
5. Otherwise → stop (no judge, no reply), except the off-topic mention handling above. The message waits for the harvester.

#### Sending

Every outbound group message (replies, decision keyboards, ambient interjections, scheduled nudges, budget/admin notices) goes through one `send_to_group()` helper that sets `message_thread_id = answer_topic_id`. Replies via aiogram `message.answer()` already inherit the thread, but anything from the scheduler/orchestrator without an incoming message must use the helper, so nothing slips into General.

#### Memory harvester (other topics → second brain)

Messages in non-answer topics never trigger a Claude call in real time. Instead a scheduled job turns them into memory in small batches:

**Two stages: a cheap code check every 30 min, then the LLM only when there's new chat.**

Stage 1 is the **tick**: pure code, no LLM, no API cost. Every `harvest.interval_min` the scheduler runs one indexed SQL query comparing each topic's newest message with its harvest cursor:

```sql
SELECT COALESCE(m.thread_id, 0)      AS thread_id,
       COUNT(*)                      AS new_msgs,
       MIN(m.created_at)             AS oldest_new,
       MAX(m.id)                     AS newest_id
FROM messages m
LEFT JOIN topic_harvest h
       ON h.chat_id = m.chat_id AND h.thread_id = COALESCE(m.thread_id, 0)
WHERE m.chat_id = :group_chat_id
  AND m.role = 'user'
  AND COALESCE(m.thread_id, 0) <> :answer_topic_id
  AND m.id > COALESCE(h.last_msg_id, 0)
GROUP BY COALESCE(m.thread_id, 0);
```

- **No rows → done.** Nothing is sent anywhere. This is the normal case on quiet days and costs a few milliseconds.
- A topic qualifies for stage 2 only if `new_msgs ≥ harvest.min_new_messages` **or** `oldest_new` is older than 6 h (so a few stray messages are still picked up eventually).
- The tick records its result (`last_tick_at`, qualifying topics) in `topic_harvest` / logs, so the dashboard can show "last checked 12:30, nothing new" vs. "harvested #Food, 23 messages".

Stage 2 is the **LLM harvest**, only for qualifying topics:

```
for each qualifying topic:
    window the new messages (same windowing as import §15.3, plus ~10 messages of prior context)
    one Haiku-tier call (models.harvest, purpose='harvest'), same JSON schema and safe-topic rules as import extraction (§15.3.1, §15.4):
      facts     → propose_memory (inbox; auto_approve respected), source: topic:<name>/msg:<id>
      episodes (chosen) → decisions(source='observed', status='accepted') when they map to an
                  EXISTING category/alias; unknown categories are not created (avoids sprawl) and are
                  shown as suggestions in the inbox instead
      options   → suggestions in the inbox (not added automatically)
    advance cursor
```

- Uses the same **topic allowlist** as the import (§15.4): food & drink, places, entertainment, activities, shopping preferences, routines, dietary restrictions/allergies. Health beyond diet, finances, work and relationship/intimate content are never extracted.
- Pre-M4 group rows (no `thread_id`) are treated as one "unknown topic" and harvested once like any other.
- Cost: zero when the group is quiet (the tick is code only); otherwise Haiku-tier, a handful of calls per day for a couple's group, so cents. Counted in `usage` and budget caps; when the budget hits 80%, the harvester pauses before anything user-facing degrades.
- The dashboard Ambient page shows harvester runs (topic, messages processed, items proposed).

#### Data model (M4 migration)

```sql
ALTER TABLE messages ADD COLUMN thread_id INTEGER;   -- NULL for DMs and pre-M4 rows
CREATE INDEX ix_messages_chat_thread ON messages(chat_id, thread_id, id);

CREATE TABLE forum_topics (
  chat_id       INTEGER NOT NULL,
  thread_id     INTEGER NOT NULL,
  name          TEXT,                    -- NULL until seen; admin can label
  closed        INTEGER NOT NULL DEFAULT 0,
  last_seen_at  TEXT,
  PRIMARY KEY (chat_id, thread_id)
);

CREATE TABLE topic_harvest (
  chat_id         INTEGER NOT NULL,
  thread_id       INTEGER NOT NULL,      -- 0 = pre-M4 'unknown topic'
  last_msg_id     INTEGER NOT NULL,      -- messages.id cursor
  last_run_at     TEXT,                  -- last LLM harvest
  last_tick_at    TEXT,                  -- last code-only check
  PRIMARY KEY (chat_id, thread_id)
);
-- usage.purpose gains 'harvest'; decisions.source gains 'observed'
```

Conversation history for replies (§7.2) is per `(chat_id, answer topic)`: Tykee's prompt contains the #Tykee conversation, not the other topics' chatter. What it learned elsewhere reaches it through the second brain (retrieval + pinned notes) and observed decisions.

#### Edge cases

- **Answer topic deleted/closed:** a send failing with a thread error raises a red health tile and DMs the admin to pick a new topic in the dashboard or via `/settopic`. Reading/harvesting continues.
- **Topics disabled for the group:** `message_thread_id` disappears, so everything lands in General (`thread_id = 1`). The health tile warns when the answer topic has been silent for 24 h while the group is active.
- **Group → supergroup migration:** topic ids are kept; only the chat id changes (§10).

**Ambient (§10.2) interaction:** debounce windows, judge calls, cooldowns and daily caps are computed over answer-topic messages only.

---

## 11. Admin dashboard

LAN-only at `http://<nas>:8080`. Single admin password (argon2 hash in env), signed session cookie, CSRF tokens on forms. Remote access, if ever needed, via Tailscale, never port-forwarding.

| Page | Contents |
|---|---|
| **Overview** | Today's/month's spend vs. caps, message count, recent decisions, health (Telegram poller, last Claude call, index status). |
| **Behaviour** | Persona/system prompt editor (with version history in `settings`), model per task, max history turns, escalation heuristic, feature toggles (proactive nudges, implicit memory, auto-approve). **Web (M7):** enable toggle, max searches/fetches per reply, daily search cap, location, domain allow/block lists, searches today. |
| **Categories & Options** | Feed of newly created categories with usage counts; merge/rename categories; edit τ, default N, allow_generated, aliases; CRUD options (tags, base weight, owner), view per-user pref multipliers, reset prefs. |
| **Memory** | Browse vault tree, search (same hybrid retriever), view/edit/delete notes, toggle pinned, **Inbox** approve/reject. |
| **Import** | Telegram export upload wizard: validate, choose chats/date range, map senders, cost estimate + consent, progress/cancel, review with evidence, apply (§15.2). |
| **Ambient** | Speak-or-silent settings (debounce, threshold, cooldown, daily cap, judge prompt), `ambient_log` timeline with reasons and feedback, mute status, scheduled nudges. |
| **Conversations** | Per-chat transcript including tool calls (debugging). |
| **Users** | Names, Telegram IDs, timezone, nudge schedule. **Telegram (M4, §10.4):** allowed group id; **answer topic dropdown** (known topics with names, message counts, last activity; change takes effect immediately); ignored topics; off-topic mention mode (`ignore`/`redirect`); label unnamed topics. |
| **System** | Reindex vault, run backup now, download backup, view logs tail. |

All settings are read from SQLite on each request, so changes apply instantly with no restart.

---

## 12. Security & privacy

| Risk | Mitigation |
|---|---|
| Strangers using the bot / burning API credit | Telegram ID allowlist enforced before any processing. |
| Dashboard access | LAN-only bind, password + session, CSRF, no default credentials. |
| Secrets | `.env` file readable only by container user; never logged; never shown in dashboard. |
| Bot memory/state leaking into the code repo | Memory (vault), the SQLite DB and its WAL/SHM, the model cache, backups and imports live under `/data`, which on the NAS is outside the repo. Belt and braces: `.gitignore` ignores them by file shape wherever `TYKEE_DATA_DIR` points (`*.db*`, `vault/`, `models--*/`, `*.onnx`, `**/imports/*.json\|zip`), `.dockerignore` keeps them out of the image, `deploy.sh` refuses to start if the data dir is inside the repo and not ignored, and CI fails any PR that tracks such a file (§14.5). The vault's own nightly git commit (§14.2) is a separate local repo with **no remote**. |
| Prompt injection via notes / user text | Tools are narrow; `write_note` path-restricted to vault folders; pinned notes immutable implicitly; no shell tools. The only network access is Anthropic's server-side web search/fetch (M7). |
| Prompt injection / bad data via web content (M7) | Web results treated as untrusted data; web-sourced memories always require human approval in the inbox; web tools only on the orchestrator call; optional domain allow/block lists; daily search cap (§7.5). |
| Path traversal in note paths | Resolve and enforce `vault_root in path.parents`; reject symlinks. |
| Bot reads all group messages | Only the configured group and allowlisted senders are persisted; topics in `telegram.ignored_topic_ids` are dropped unread. Other topics are read and stored with `thread_id` and sent to Anthropic only in harvester batches (Haiku-tier, topic allowlist, §10.4); Tykee speaks only in the answer topic. Both users know the bot is listening (stated on join). |
| Cross-user leakage (in chat) | Owner filter in retrieval and pinned injection; one user's private memories only enter the other's chat when `for_users=both`, and then only constraint-type notes. **Accepted:** facts learned in a DM (`memories/<slug>/`) can surface in group answers, since the group defaults to `for_users=both`. No per-note `private` flag. |
| Admin visibility | **Decided:** admin (Jack) can see all conversations and all memories, including the partner's, in the dashboard. The bot states this once to each user on first contact (`/start`) so it is transparent. |
| Data sent to Anthropic | Only the assembled prompt; documented to both users. API data is not used for training by default per Anthropic's commercial terms (verify current terms). |

---

## 13. Cost control

- Every Claude response's `usage` block is recorded with computed cost (price table in `settings`).
- `budget.daily_usd` and `budget.monthly_usd` caps: at 80% → dashboard warning; at 100% → fallback mode (§8.5) until reset.
- Levers: Haiku-tier by default, prompt caching on static system blocks, history capped at N turns plus rolling summary, `max_tokens` capped (default 400) for replies.
- Web search (M7): per-search fee + extra input tokens; capped per reply and per day, and disabled first when the budget reaches 80% (§7.5).

---

## 14. Deployment & operations

### 14.1 Compose

```yaml
services:
  tykee:
    build: .
    container_name: tykee
    restart: unless-stopped
    env_file: .env
    environment:
      - TZ=Asia/Singapore           # adjust
    ports:
      - "8080:8080"                 # dashboard, LAN only
    volumes:
      - /volume_nvme/tykee/data:/data        # bot.db, vault/, models cache, backups
    mem_limit: 1g
    healthcheck:
      test: ["CMD", "python", "-m", "app.healthcheck"]
      interval: 60s
```

`.env`:

```
TELEGRAM_BOT_TOKEN=...
ANTHROPIC_API_KEY=...
ALLOWED_TELEGRAM_IDS=111111111:jack,222222222:partner   # <id>:<slug>; first = admin
GROUP_CHAT_ID=-100...                                   # optional until the bot has joined (§10)
TZ=Asia/Singapore                                       # household timezone for day boundaries
DASHBOARD_PASSWORD_HASH='$argon2id$...'                 # single-quoted: compose interpolates $ in env_file
SESSION_SECRET=...
```

Notes:
- Place `/data` on the **NVMe** volume (SQLite fsync latency, no HDD spin-up delays).
- Use the official `python:3.12-slim` image; its `sqlite3` supports `enable_load_extension` for `sqlite-vec`.
- Bake the embedding model into the image at build time (or cache under `/data/models`) so startup works offline.

### 14.2 Backups

| What | How | When |
|---|---|---|
| SQLite | `VACUUM INTO '/data/backups/bot-YYYYMMDD.db'` (consistent online copy) | Nightly 03:00, keep 14 |
| Vault | `git add -A && git commit` inside `/data/vault` | Nightly 03:05 |
| Whole `/data` | UGOS btrfs snapshot of the share | Daily, per NAS policy |

The vector/FTS index is in the DB backup, but can always be rebuilt from the vault, so the vault plus app tables are what truly matter.

### 14.3 Observability

- Structured JSON logs to stdout (Docker log rotation on).
- Dashboard health tiles: poller last-update time, Claude error rate (24h), index chunk count, DB size.

### 14.4 Failure modes

| Failure | Behaviour |
|---|---|
| Claude API error / timeout | Retry with backoff (2×), then fallback pick + "my brain's offline, here's a random pick". |
| Budget exhausted | Fallback mode; notify admin via Telegram DM once per day. |
| Telegram network blip | aiogram polling retries automatically. |
| Embedding failure on write | Note file still written; chunk marked `embed_model='pending'`; reconcile job retries. |
| Corrupt/missing index | `System → Reindex` drops index tables and rebuilds from vault. |
| NAS reboot | `restart: unless-stopped`; startup reconcile. |

### 14.5 CI and deploying

**CI** (`.github/workflows/ci.yml`, on every pull request and on pushes to `main`):

| Job | What |
|---|---|
| lint · types · tests | `ruff check`, `ruff format --check`, `mypy --strict`, `pytest` (offline unit tests; `-m integration` stays opt-in), in `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`, the same image `make check` uses, so local and CI results match (its Python allows loading `sqlite-vec`) |
| shellcheck | `deploy.sh` |
| no bot data committed | fails if any tracked path looks like a DB, vault, model file or `.env` |
| docker build | `linux/amd64` image build (includes baking the embedding model), after the checks pass; layer cache in GitHub Actions; nothing is pushed |

No secrets are used: unit tests run with fakes and no API key. There's no deploy from CI; the NAS is LAN-only.

**Deploying** (`deploy.sh`, on the NAS): `./deploy.sh [branch]` → pull (optionally switch branch) → check `.env` (token, allowlist), data dir ownership (uid 1000) and that the data dir isn't inside the repo un-ignored → `docker compose build` → `up -d` → wait for the `polling` log line; on a startup error it prints the logs and the rollback command (`./deploy.sh <previous commit>`).

---

## 15. Bootstrap import (past 6 months of chat history)

### 15.1 Source: Telegram export, uploaded via the dashboard

**Decided:** Jack exports Telegram chat history manually and uploads it through the dashboard's **Import** page. The Bot API **cannot** read messages from before the bot existed or from chats it isn't in, so an export is the only way to get history.

#### How to export (Telegram Desktop only; the phone apps can't export)

1. Install **Telegram Desktop** (desktop.telegram.org) and log in. The macOS App Store "Telegram for macOS" app has different export options; use Telegram Desktop.
2. Open the chat to import (your DM with each other, and/or the group).
3. Click **⋮** (top right) → **Export chat history**.
4. Untick photos, videos, voice messages, video messages, stickers, GIFs and files. Only text is needed, and it keeps the file small.
5. **Format: Machine-readable JSON**. Set the date range to "from" 6 months ago if offered.
6. Export. Telegram writes a folder containing `result.json`. Telegram may ask you to confirm the export on another device, or enforce a short security delay; that's expected.
7. Repeat per chat. Each upload becomes its own `import_jobs` row.

Also accepted: a **full-account export** (Settings → Advanced → Export Telegram data, JSON). Its `result.json` contains `chats.list[]`, and the dashboard lets you choose which chats to import.

#### Accepted upload formats

| Upload | Handling |
|---|---|
| `result.json` (single chat) | Parsed directly. |
| `result.json` (full-account export) | Parsed; chat picker shown (§15.2 step 3). |
| `.zip` of the export folder | Only `result.json` is extracted and the rest is ignored. Zip-bomb guard: reject if uncompressed `result.json` > 500 MB or compression ratio > 100×. |

Upload limit 200 MB (configurable). FastAPI streams the upload to `/data/imports/<sha256>.json` rather than holding it in memory.

#### Parsing

Relevant fields: `messages[].type` (`message` vs `service`, skip service), `id`, `date`, `from` (display name), `from_id` (`"user<telegram_id>"`), `text` (either a string or a list of strings/entity objects, flattened to plain text), `reply_to_message_id`, `forwarded_from`. Stickers, photos and voice notes become placeholders like `[photo]` and forwarded messages are prefixed `[fwd]`. Parsing is streaming (`ijson`) so big exports don't spike RAM on the NAS.

Parsers are pluggable (`parse(file) -> Iterator[NormalisedMessage]`), so a WhatsApp `.txt` parser can be added later without touching the rest of the pipeline.

### 15.2 Dashboard import flow

```
Import page
 ① Upload ─► ② Validate ─► ③ Choose chats & range ─► ④ Map senders ─► ⑤ Preview & cost ─► ⑥ Run ─► ⑦ Review ─► ⑧ Apply
```

| Step | What the admin sees / does | Behind the scenes |
|---|---|---|
| ① Upload | Drag-and-drop `result.json` or `.zip`, with a link to the export how-to above. | Stream to disk, compute sha256; reject a duplicate file with a link to the earlier job. |
| ② Validate | "Telegram single-chat export: *Jack & Partner*, 18,432 messages, 2025-09-28 → 2026-09-30" or a clear error. | Detect format (single chat vs full account), check JSON structure, count messages. |
| ③ Choose chats & range | Checkbox list of chats (full-account exports only). Date range defaults to **last 6 months**. | Filter. Duplicates against earlier imports are skipped by `(chat_ref, telegram message id)`. |
| ④ Map senders | Table of each distinct sender: name, Telegram id, message count → dropdown **Jack / Partner / Other**. | Auto-mapped where `from_id` matches `users.telegram_id`; anything unmapped defaults to Other (included as context, never an owner of facts). |
| ⑤ Preview & cost | Message and window counts, a sample of normalised transcript lines, estimated tokens and **estimated USD**, remaining monthly budget. Required checkbox: *"Both participants agreed to this import being processed by the Anthropic API."* | Windowing runs here, so the estimate is exact on input tokens. |
| ⑥ Run | Progress bar per stage (extracting n/N windows → consolidating → ready for review), live cost so far, **Cancel** button. Safe to close the browser. | Job runs in the scheduler; HTMX polls job status every 5 s. Cancel stops new batch submissions and marks the job `cancelled`. |
| ⑦ Review | Tabs: **Categories** (review first, see Tuning in §15.5), **Options**, **Notes**, **Decisions**. Each item shows evidence quotes with dates and confidence; edit inline; approve/reject; bulk "approve all ≥ 0.8". | `import_items` rows updated. |
| ⑧ Apply | Summary ("12 categories, 85 options, 40 notes, 210 decisions") → **Apply**. | Single transaction for DB rows, NoteStore writes for notes, then raw text purge (§15.3 APPLY). Option to delete the uploaded export file (default: delete). |

Schema additions for this flow:

```sql
ALTER TABLE import_jobs ADD COLUMN chats_json      TEXT;   -- selected chats
ALTER TABLE import_jobs ADD COLUMN sender_map_json TEXT;   -- {"user123": "jack", ...}
ALTER TABLE import_jobs ADD COLUMN consent_at      TEXT;   -- when the consent box was ticked
ALTER TABLE import_jobs ADD COLUMN est_cost_usd    REAL;
-- status gains: 'uploaded' | 'configured' | 'cancelled'
```

(In the initial migration these are folded into the `CREATE TABLE`; shown separately here for readability.)

### 15.3 Pipeline

```
upload + configure in dashboard (§15.2 steps ①–⑤)               import_jobs: configured
  │  sha256 dedupe · selected chats · date range · sender map applied
  ▼
windowing                                                         import_windows: pending
  │  split on gaps > 2 h or ~6k tokens · lines: "[2026-04-01 19:02] jack: ..."
  ▼
EXTRACT (map) – Opus-tier via Message Batches API (async, ~50% cheaper)    → extracting
  │  per window, JSON-schema output (safe-topic rules §15.4 applied here first):
  │   episodes:  decision episodes, see §15.3.1
  │   facts:     [{owner, type, statement, quote, ts, confidence}]
  │   options:   [{category_phrase, name, tags, sentiment: -1..1}]
  │   skipped_out_of_scope: integer      (count only, never content)
  ▼
CONSOLIDATE (reduce) – deterministic code + Opus-tier calls         → consolidating
  │  1. categories: Opus reads ALL decision episodes and designs the category set
  │     (§15.3.2); every episode is mapped to a category or marked unmapped
  │  2. options: normalise names (casefold + rapidfuzz ≥ 90) within category;
  │     mean sentiment → suggested base_weight / per-user pref multiplier
  │  3. facts: group by owner, cluster by embedding → one Opus call per cluster merges
  │     into note sections; newer evidence wins conflicts; drop singletons with confidence < 0.6
  │  4. decisions: chosen episodes → decisions under their Opus-assigned category,
  │     original timestamps kept; undecided/abandoned episodes kept as evidence only
  ▼
REVIEW (dashboard → Import → job)                                  → review
  │  grouped by kind; every item shows evidence quotes + dates; edit / approve / reject;
  │  "approve all ≥ 0.8 confidence" bulk action
  ▼
APPLY                                                              → done
     notes → NoteStore (provenance: source: import:<job_id>)
     categories → resolver-compatible rows + aliases (created_by='import')
     options → options (created_by='import'); prefs → option_prefs
     decisions → decisions(source='import', status='accepted' for chosen)
     then: null out import_windows.text, VACUUM, optionally delete the uploaded export
```

**Why imported decisions matter:** they seed the recency penalty and preference multipliers from day one. Without them, the bot would happily suggest what you ate yesterday.

#### 15.3.1 Decision episodes (extraction)

The extractor doesn't just look for final choices. It finds every **decision episode**: a stretch of conversation where you two discussed and made (or failed to make) a choice, such as "what to eat tonight", "which movie", "where to go Saturday", "which sofa to buy".

```json
{
  "episode_id": "w12-e3",
  "summary": "Deciding dinner on Friday; Jack wanted spicy, partner wanted something light",
  "category_phrase": "dinner",            // the kind of decision, in the chat's own words if possible
  "phrases_seen": ["makan where", "dinner", "晚餐吃什么"],
  "for_users": "both",                     // jack | partner | both
  "options_considered": [
    {"name": "Thai basil chicken", "by": "jack", "stance": "proposed"},
    {"name": "Yong tau foo", "by": "partner", "stance": "proposed"},
    {"name": "Mala", "by": "partner", "stance": "rejected", "reason": "too heavy"}
  ],
  "outcome": "chosen",                     // chosen | undecided | abandoned
  "choice": "Yong tau foo",
  "reasons": ["wanted something light", "near the MRT"],
  "ts_start": "2026-05-02T18:40:00+08:00",
  "ts_end": "2026-05-02T19:05:00+08:00",
  "quotes": ["eh not mala again lah", "ok ytf"],
  "confidence": 0.86
}
```

- Episodes can span window boundaries; windows overlap by ~10 messages and consolidation de-duplicates episodes with the same category phrase, overlapping time range and same choice.
- Rejected options and reasons are kept, because they become option sentiment and memory facts ("partner finds mala too heavy on weekdays").

#### 15.3.2 Category creation by Opus (consolidation step 1)

Categories for the import are **designed by Opus from the episodes**, not by embedding clustering. Embeddings proved unreliable for telling category phrasings apart (§8.1, v1.9).

1. Code builds a compact episode list, one line each: `id | date | category_phrase | phrases_seen | summary | options | outcome/choice`. Six months is expected to be hundreds to low thousands of episodes, which fits a single Opus call. If it doesn't fit the token budget, it's split by month and a final Opus pass merges the partial taxonomies.
2. One Opus call (`models.import_consolidate`, structured JSON output) returns:
   ```json
   {
     "categories": [
       {"slug": "dinner", "display_name": "Dinner", "description": "What to eat for dinner, eat out or delivery",
        "recency_tau_days": 3, "default_n": 1, "allow_generated": true,
        "aliases": ["makan where", "dinner", "晚餐", "what to eat tonight"],
        "episode_ids": ["w12-e3", "..."]}
     ],
     "unmapped": [{"episode_id": "w40-e1", "why": "one-off, no recurring pattern"}]
   }
   ```
3. **Rules given to Opus:**
   - A category = a **recurring kind of decision** you two actually make. Granularity follows how you decide: split "lunch" from "dinner" only if they're decided differently; keep "weekend activity" as one category unless the episodes clearly show distinct kinds.
   - Minimum support: at least 3 episodes, or 2 with a chosen outcome. Anything below folds into a broader category or goes to `unmapped`.
   - Aim for 5–25 categories; no near-duplicates.
   - Aliases include the actual phrasings seen, including Singlish and Chinese, so the live resolver (§8.1) hits them deterministically from day one.
   - τ from the observed cadence (e.g. ate the same thing at most every N days), within the typical ranges in §8.1.
   - **Never create a category in an excluded domain (§15.4)**, even if episodes slipped through extraction; such episodes go to `unmapped` with `why: "out_of_scope"` and are discarded.
4. Code validates the result: unique slugs, every episode either mapped exactly once or unmapped, τ clamped to [0.5, 365], aliases normalised as in §8.1, and aliases that collide across categories dropped from both and flagged for review.
5. Options and chosen decisions are then attached to these categories (steps 2 and 4 of consolidation).

**Review:** the Categories tab is reviewed **first**. Each proposed category shows its episode count, a few sample episodes with quotes and dates, proposed τ and aliases. You can approve, rename, edit τ/aliases, **merge** two proposals, or reject. Rejecting a category drops its decisions and options from the apply step; its facts remain reviewable separately. Unmapped episodes are listed for information and can be assigned to a category by hand.

### 15.4 Extraction scope (privacy)

**Safe-topic rules** apply to everything the import (and the memory harvester, §10.4) produces: episodes, categories, options, facts and decisions.

| Allowed | Skipped |
|---|---|
| Food & drink, places, entertainment, activities, outings & trips, shopping preferences (what to buy, brands, styles), routines/schedules, dietary restrictions & allergies | **Health** (conditions, medication, doctor visits, mental health; only allergies/diet survive) · **Money** (salaries, budgets, savings, investments, debts, bills, price negotiations) · **Work** (jobs, colleagues, office matters) · **Relationship talk** (disagreements, feelings, intimacy, family conflicts) · personal details about third parties |

Enforced in three layers:
1. **Extraction (Opus):** the prompt lists both columns; out-of-scope discussions produce no episode, fact or option. Only a count (`skipped_out_of_scope`) is returned, never the content.
2. **Category creation (Opus):** no category may be created in a skipped domain. Any episode that slipped through is sent to `unmapped` as `out_of_scope` and discarded (§15.3.2).
3. **Review (you):** anything that still looks out of scope is rejected in the dashboard. The job summary shows how many windows had out-of-scope content skipped.

Edge cases: a dinner choice that mentions price ("cheaper one lah") keeps the choice but drops the money detail; "which phone to buy" is shopping and allowed, but budgets or amounts are not stored. Out-of-scope content is never written to the vault or the DB, even if it appears in a window.

Both people's raw messages are sent to the Anthropic API during extraction, so both should agree to the import before it runs. The dashboard enforces this with a required consent checkbox (§15.2 step ⑤), recorded in `import_jobs.consent_at`.

### 15.5 Operational notes

- **Resumable:** each window has its own status; a crash or restart resubmits only `pending`/`failed` windows. Batches are polled by the scheduler (every 5 min) until complete.
- **Idempotent:** the same export (`file_sha256`) can't be imported twice. A later export overlapping the same period is deduped by `(chat_ref, telegram message id)`.
- **Cost:** two people over 6 months is plausibly 10–30k messages, i.e. a few hundred thousand input tokens. Defaults are **Opus-tier for both stages**: the import is a one-off whose quality defines the bot's day-one memory, and Opus is noticeably better at implicit preferences, Singlish and half-finished decisions. Rough estimate at current pricing: ~$2 extraction (batch) + ~$1.60 consolidation ≈ **$3–4** total; even at 3× the token estimate, ~$10 once. Both stages remain configurable (`models.import_extract`, `models.import_consolidate`); a cheaper Haiku/Sonnet run can be piloted on one month and compared in the review screen. The dashboard shows an estimate before submission and the job counts against the monthly budget.
- **Tuning:** review the proposed categories before approving anything else; approved categories and their aliases become what the resolver (§8.1) matches against.

---

## 16. Milestones

| # | Scope | Done when |
|---|---|---|
| M1 | Skeleton: compose, config, SQLite + migrations, aiogram polling, allowlist, group (privacy off) + DM handling, Claude chat with history (last N turns), mention-only replies, `LLMClient` with usage/cost recording and budget checks | Bot answers when mentioned in the group; others ignored. |
| M2 | Decision engine: dynamic categories + resolver (§8.1), options, `random_pick`, inline buttons, feedback learning, fallback | "Dinner?" and "what movie?" each create/resolve a category and return a weighted, non-repeating pick. |
| M2b | Ambient participation: debounce, stage-1 rules, judge call, cooldown/caps, `/quiet`, `ambient_log`, rolling chat summaries | Bot steps in on "idk you decide" and stays silent through small talk. |
| M3 | Second brain: NoteStore, chunking, `Embedder` (fastembed, model baked into image, int8 vs fp32 eval per §6.9), FTS5 + sqlite-vec, hybrid retrieval, pinned notes, memory tools, inbox, decision log mirror | "Remember I hate coriander" changes future picks. |
| M4 | Dashboard: auth, all pages in §11 (incl. category merge), usage & budget UI (enforcement itself lands in M1); **forum topics (§10.4)**: read all topics with `thread_id`, answer only in the dashboard-selected answer topic (dropdown + `/settopic`), ignored-topic list, `forum_topics` name tracking, `send_to_group()` helper, off-topic mention handling, **memory harvester** for other topics | All behaviour controllable without touching code. Tykee learns from every topic but only speaks in the answer topic; switching the answer topic in the dashboard takes effect immediately; a preference mentioned in another topic shows up in the memory inbox. |
| M5 | Bootstrap import: dashboard upload wizard (§15.2), Telegram JSON/zip parser (single chat + full account), windowing, batch extraction, consolidation, review UI, apply; decision-episode extraction (§15.3.1), **Opus-designed categories** with review/merge (§15.3.2), three-layer safe-topic enforcement (§15.4) | 6 months of history reviewed and applied; bot knows both users on day one. Categories reflect the decisions you actually discussed; nothing about health, money, work or relationship talk is stored. |
| M6 | Scheduled nudges (optional), backups, healthcheck, polish, runbook | Runs unattended for 2 weeks. |
| M7 | Web access (§7.5): web search + fetch server tools on the orchestrator call, `web.*` settings + dashboard toggles, persona rules, web-sourced memory → inbox only, usage/cost recording of searches & fetches, daily cap + budget-based disable, error fallback | "Is that new ramen place in Tanjong Pagar any good?" gets a short, cited answer; a web fact never lands in the vault without approval; tests pass offline with web tools faked. |

Deferred / later: voice notes (requires separate STT), photo input (fridge contents, works with Claude vision), location-aware suggestions (Maps/Places API), weather context, WhatsApp import parser.

---

## 17. Open questions

1. **Consent:** partner agrees to their past messages being processed by the import (§15.4); enforced by the consent checkbox in the wizard.

### Resolved

| Question | Decision |
|---|---|
| Language | Mainly English, some Singlish, a little Chinese → `multilingual-e5-small`, see §6.8. |
| Embedding precision | int8 model weights by default (memory headroom under 1 GB), fp32 vector storage; confirmed by an int8-vs-fp32 recall eval in M3, see §6.9. |
| Admin visibility | Admin sees all conversations and memories, see §12. |
| Chat mode | Both DMs and one shared group, see §10. |
| Categories | Inferred and created dynamically, with resolver + merge, see §8.1. |
| Bootstrap | From the past 6 months of chat history, see §15. Opus finds decision episodes and designs the category set from them; safe-topic rules skip health, money, work and relationship talk (§15.3–15.4). |
| LLM provider | Claude API (API key) for all runtime LLM calls; Claude Code for development only, see §7.0. |
| Web access | Agent with Anthropic server-side web search/fetch, orchestrator only, capped, web-sourced memories need approval. Milestone M7, see §7.5. |
| Import source | Telegram Desktop JSON export, uploaded via the dashboard Import wizard; chats chosen at upload time, see §15.1–15.2. |
| Group behaviour | Group is primary; bot reads everything and decides when to speak, silent by default, see §10.2. |
| Group topics | Tykee reads and builds memory from **all** topics (except an optional ignore list) but only speaks in one **answer topic** (initially #Tykee), changeable from the dashboard. Enforced in code; milestone M4, see §10.4. |
| Scheduled nudges | Supported but off by default, see §10.3. |
