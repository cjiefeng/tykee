# Tykee runbook

Day-to-day operations on the NAS. Design background: [design.md](design.md) §11 (dashboard),
§14 (deployment & operations). Commands assume you're in the repo checkout on the NAS; prefix
`docker` with `sudo` if your user isn't in the `docker` group.

## What's running

| Thing | Where |
|---|---|
| Container | `tykee` (one replica only: a second poller gets 409 Conflict from Telegram) |
| Dashboard | `http://<nas>:8081` (LAN or Tailscale only) |
| Data | `$TYKEE_DATA_DIR` from `.env` (on the NAS `/volume_nvme/tykee/data`), mounted at `/data` |
| Logs | `docker compose logs -f tykee` (JSON lines, rotated at 5 × 10 MB); last 200 lines on System |

Inside the data dir:

```
bot.db (+ -wal, -shm)   SQLite: everything except notes; the search index is derived
vault/                  markdown notes (source of truth for memory) + its own local git history
backups/bot-YYYYMMDD.db nightly DB copies (keep 14 by default)
imports/                uploaded Telegram exports (§15)
.heartbeat              touched every minute by the event loop (healthcheck fallback)
```

## Deploy, test a branch, roll back

```bash
./deploy.sh              # latest commit of the current branch
./deploy.sh m6-ops       # try a PR branch on the real bot
./deploy.sh main         # back to main
./deploy.sh <commit>     # roll back (deploy.sh prints the exact command after every deploy)
```

`deploy.sh` checks `.env` and data dir ownership (uid 1000), builds, restarts, waits for the
`polling` log line, then for Docker to report the container healthy. Migrations run on startup;
they only ever add, so rolling back the code after a migration is safe for the app tables
(an older build ignores new tables/columns).

## Is it healthy?

1. **Dashboard → Overview → Health.** Every tile should be green: Telegram (last update), Claude
   (error rate), answer topic, memory index (no pending embeddings), harvester, backups (last
   backup < 36 h).
2. **`docker compose ps`** shows `healthy`. The healthcheck (`python -m app.healthcheck`) calls
   `/healthz`, which fails if the event loop doesn't answer or the scheduler hasn't ticked for
   5 minutes. Without dashboard secrets it checks that `.heartbeat` is under 3 minutes old.
3. **Self-recovery.** Docker doesn't restart a container just for being *unhealthy*, so the bot
   watches itself: if the event loop stalls for 10 minutes, a watchdog thread logs
   `event loop stalled; exiting` and exits, and `restart: unless-stopped` brings it back. NAS
   reboots also restart it; startup reconciles the vault with the index.

## Backups

| What | When | Where |
|---|---|---|
| DB copy (`VACUUM INTO`, consistent while running) | nightly at `backup.time` (03:00) | `backups/bot-YYYYMMDD.db`, newest `backup.keep` (14) kept |
| Vault commit (local git, **never a remote**) | right after the DB copy | `vault/.git` |
| Whole data dir | daily, UGOS btrfs snapshot | NAS snapshot policy |

If the NAS was off at 03:00, the backup runs when it's back the same day. **System → Back up
now** makes one immediately (replacing today's file); each file has a Download link. Settings:
`backup.enabled`, `backup.time`, `backup.keep`.

Check a copy without touching the live DB:

```bash
sqlite3 "$TYKEE_DATA_DIR/backups/bot-20261002.db" "PRAGMA integrity_check; SELECT COUNT(*) FROM decisions;"
```

### Restore the database

You lose app state since that backup (decisions, history, usage, settings changes). Notes are
unaffected: the vault is separate and the index is reconciled from it on startup.

```bash
docker compose stop tykee
cd "$TYKEE_DATA_DIR"
mkdir -p broken && mv bot.db bot.db-wal bot.db-shm broken/ 2>/dev/null
cp backups/bot-20261002.db bot.db && chown 1000:1000 bot.db
cd - && docker compose start tykee
```

### Restore notes

```bash
git -C "$TYKEE_DATA_DIR/vault" log --oneline -- people/jack.md        # find the version
git -C "$TYKEE_DATA_DIR/vault" checkout <commit> -- people/jack.md    # bring it back
```

Then **System → Reindex vault** (or restart) so the index matches the files. Don't edit the vault
by hand otherwise; the bot is its only writer.

## Routine tasks

| Task | How |
|---|---|
| Rebuild search index | System → Reindex vault (drops the derived index, rebuilds from the files) |
| Change budget caps | Behaviour, or Settings → `budget.daily_usd` / `budget.monthly_usd` |
| Mute the bot in the group | `/quiet 2h` in Telegram, or Ambient → Unmute to undo |
| Move it to another topic | Users → answer topic, or `/settopic` inside the topic |
| Scheduled nudges | Ambient → Scheduled nudges: turn on, add time/days/category/target, "Send now" to test |
| Rotate Telegram token | BotFather → `/revoke`, put the new token in `.env`, `./deploy.sh` |
| Rotate Anthropic key | Console → new key, update `.env`, `./deploy.sh`, delete the old key |
| Change dashboard password | `docker compose exec tykee python -m app.dashboard.hashpw`, put both values in `.env` (single-quote the hash), `./deploy.sh`. A new `SESSION_SECRET` logs everyone out |
| Restart without redeploying | `docker compose restart tykee` |
| Fix a place (typo, duplicate, someone's home slipped through) | Memory → Places: rename, merge into the right one, or delete (removes its note too) |
| Make "near home" work / add pets | Memory → Places → Your areas (e.g. Home around Bishan, also called "home, us") and Pets (`Mochi dog small`) |
| Correct a place's pet info | Memory → Places → Attributes: set the value (counts as "you confirmed", beats any web label) or remove it |
| Refresh the areas gazetteer | Download the three data.gov.sg GeoJSON files listed in `scripts/build_areas.py`, run `uv run python -m scripts.build_areas <folder>` in `make shell`, commit `app/seed/areas.json`; new areas are added on the next start |

Changes to user names/timezones and `embedding.precision` need a restart; everything else applies
immediately.

## Troubleshooting

| Symptom | Likely cause → fix |
|---|---|
| Bot silent in the group | Muted (Ambient shows it) → Unmute. Not mentioned and judge said no → expected. Answer topic deleted (red tile, admin DM) → pick a new topic. |
| "My brain's offline" replies | Claude errors or no API key → check the Claude tile and logs for `anthropic`. |
| Random picks only, admin got a 💸 DM | Budget cap reached → raise the cap or wait for the daily/monthly reset. |
| Pending embeddings on the index tile | Embedding model failed → retried hourly; check logs for `embedding`. Restart if it persists. |
| Container `unhealthy` | `docker compose logs --tail 100 tykee`. If the loop is wedged the watchdog restarts it within 10 min; otherwise `docker compose restart tykee`. |
| `409 Conflict` in logs | A second instance is polling (another machine or a dev `make up`) → stop it. |
| Startup error after deploy | `deploy.sh` prints the logs and the rollback command. |
| `cannot write to /data` | Data dir ownership → `sudo chown -R 1000:1000 "$TYKEE_DATA_DIR"`. |
| Backups tile amber/red | System page shows the last error. Disk full? `df -h /volume_nvme`. |
| Maps links not recognised (Memory → Places shows `failed`) | The NAS needs outbound HTTPS to `maps.app.goo.gl`; check DNS/egress. Failed links get one retry after 6 h. `blocked_host` means Google redirected somewhere off the allowlist: expected for non-Maps links. |
| "Eating here" + link gets no 👌 | Only in the answer topic, and only if the category from the message or the meal slot exists (Memory → Places → meal slots). Logs say `shared place: no category`. |
| Vault commit "has a remote" | Someone added a git remote to the vault → `git -C vault remote remove <name>`. Memory must never be pushed anywhere (§12). |

## Two-week unattended check (M6)

Before leaving it alone: Overview all green, `docker compose ps` healthy, a backup from last
night in System, and `docker system df` / `df -h` show room to grow. After two weeks: 14 backups
present, no restarts you didn't cause (`docker inspect -f '{{.RestartCount}}' tykee`), no
`ERROR` lines you can't explain (`docker compose logs tykee | grep '"level": "ERROR"'`), spend
within caps.
