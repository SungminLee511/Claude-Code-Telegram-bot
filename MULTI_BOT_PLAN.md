# Multi-Bot Support Plan — run N Claude-Code Telegram bots concurrently, crash-free

**Goal:** allow multiple independent Telegram bot processes (one per token) to
run on the same host **simultaneously**, with **zero** cross-talk, **zero**
crashes, and **fully independent** self-wake / relay / restart behaviour.

**Total steps: 22** (across 6 parts + testing + rollback).

---

## 0. Why the bots collide today (root-cause inventory)

Read before touching anything. These are the only shared-singleton points; the
plan neutralises each one.

| # | Shared resource | File / line | Failure if 2 bots share it |
|---|-----------------|-------------|----------------------------|
| C1 | **Inject file** `/tmp/claude_inject_message.json` (hardcoded) | `src/bot/inject_watcher.py:34`, `wake_after.sh` | A wake meant for bot A gets eaten by whichever bot's watcher polls first → **wrong bot wakes**, or both race on `os.rename`. THE primary blocker. |
| C2 | **Relay file clobber** `cat > $INJECT_FILE` (truncating write) | `wake_after.sh` | Two near-simultaneous wakes overwrite each other → lost wake. |
| C3 | **SQLite DB** `sqlite:///data/bot.db` (relative path) | `src/utils/constants.py` `DEFAULT_DATABASE_URL` | Same cwd → both bots write the same DB → session_id / auth rows interleave → **session resume targets wrong conversation**, possible `database is locked`. |
| C4 | **restart_bot.sh** kills via `pgrep -f "[s]rc\.main"` | `restart_bot.sh` | Restarting bot A kills bot B too. |
| C5 | **API / webhook ports** `8080` / `8443` | `settings.py` | Only if those servers are enabled; second bot fails to bind → startup crash. |
| C6 | **Relay-state file** `/tmp/claude_relay_state.json` (kill-switch) | CLAUDE.md convention / wake tooling | Two relays share one counter → wrong kill-switch accounting. |

Non-issues (verified, do **not** need changes):
- **Telegram polling**: distinct tokens = distinct `getUpdates` streams → **no
  409 conflict**. (409 only happens with the *same* token polled twice.)
- **Claude SDK / SessionManager**: session_id is stored per-user *in the DB*;
  once each bot has its own DB (C3) there is no shared in-memory state.
- `drop_pending_updates=True` is per-token → harmless.

---

## Design principle

Everything keyed by a single **`BOT_ID`** (a short slug, e.g. `main`, `work`,
`alt`). One `.env` per bot → `<BOT_ID>.env`. All per-bot paths derive from
`BOT_ID`. Default behaviour (no BOT_ID) stays byte-identical to today, so a
single-bot deployment is unaffected (backward compatible).

---

## PART A — Inject queue isolation (fixes C1, C2) — the core change

- **A1** — Add `inject_dir: str = "/tmp/claude_inject"` and
  `bot_id: str = "main"` to `Settings` (`src/config/settings.py`). Each bot
  watches its own subdir `<inject_dir>/<bot_id>/`.
- **A2** — Refactor `inject_watcher.py` from **single-file** to
  **spool-directory** model:
  - Watch `inject_dir/bot_id/` for `*.json` files (instead of one fixed path).
  - Process **oldest-first** (sort by mtime/filename) so ordering is FIFO.
  - Keep the atomic `os.rename(file, file+".processed-<ts>")` claim so two
    watcher iterations never double-fire (race-safe already).
  - Each wake = a **uniquely-named** file (`<unix_ns>-<rand>.json`) → no
    clobber possible (fixes C2 structurally — no shared filename).
- **A3** — Wire `inject_watcher_loop(self.app, inject_path=...)` in
  `src/bot/core.py` to pass the per-bot spool dir from settings (currently it
  passes nothing → global default). Create the dir on startup if missing.
- **A4** — Backward-compat shim: if a legacy `/tmp/claude_inject_message.json`
  appears AND `bot_id == "main"`, still honour it (so old `wake_after.sh`
  invocations don't silently break during migration).

## PART B — Wake tooling per-bot (fixes C2, C6)

- **B1** — Parametrize `wake_after.sh`: accept `BOT_ID` (env or `$3`), resolve
  `INJECT_DIR="/tmp/claude_inject/<BOT_ID>"`, resolve `CHAT_ID` from a per-bot
  map / env (`WAKE_CHAT_ID_<BOT_ID>`).
- **B2** — Write atomically: write to `…/.<name>.tmp`, then `mv` to the final
  `…/<unix_ns>-<rand>.json` (mv is atomic on same filesystem → watcher never
  sees a half-written file).
- **B3** — Make relay-state file per-bot:
  `/tmp/claude_relay_state_<BOT_ID>.json` (fixes C6). Document in CLAUDE.md
  convention notes.

## PART C — Per-bot persistence (fixes C3)

- **C1s** — Derive `DATABASE_URL` default from `BOT_ID` when not explicitly set:
  `sqlite:///data/bot_<bot_id>.db` (absolute path recommended in `.env`). Keeps
  `data/bot.db` for the legacy `main` bot to avoid migrating existing sessions.
- **C2s** — Ensure each bot's `data/` write is process-local; add a startup log
  line echoing the resolved DB path so misconfig is obvious.

## PART D — Launcher / restart isolation (fixes C4)

- **D1** — Rewrite `restart_bot.sh` to take `BOT_ID` and match **only that
  bot's** process. Pass `--config-file <BOT_ID>.env` to `src.main`; match
  `pgrep -f "src\.main.*<BOT_ID>\.env"` so restarting one bot never touches the
  others.
- **D2** — Add `start_bot.sh <BOT_ID>` convenience launcher (nohup + disown,
  per-bot logfile `bot_<BOT_ID>.log`).
- **D3** — Verify `src/main.py --config-file` path already loads the right
  `.env` (it does via `load_config(config_file=...)`); add per-bot log prefix.

## PART E — Port distinctness (fixes C5)

- **E1** — In each `<BOT_ID>.env`, require distinct `API_SERVER_PORT` /
  `WEBHOOK_PORT` when those features are enabled. Add a startup assertion: if
  the port is already bound, fail fast with a clear message (not a stack trace).
- **E2** — If API/webhook disabled (the common case), document that ports are
  irrelevant and bots need no port config at all.

## PART F — Optional supervisor

- **F1** — Add `bots.yaml` (list of `{bot_id, env_file, enabled}`) +
  `supervise.sh` that starts/stops/restarts all enabled bots, each via the
  per-bot scripts from PART D. Pure convenience; bots also run standalone.

## PART G — Testing & rollback

- **G1** — Unit test: spool-dir watcher processes 2 files FIFO, claims atomically,
  ignores other bots' subdirs.
- **G2** — Integration smoke test: launch 2 bots with 2 tokens + 2 DBs; send a
  wake to each via `wake_after.sh <BOT_ID>`; assert each wake reaches **only**
  its own bot (check per-bot logs).
- **G3** — Regression: single-bot default path unchanged (no `BOT_ID` set →
  legacy `/tmp/claude_inject_message.json` + `data/bot.db` still work).
- **G4** — Rollback note: all changes are additive + backward-compatible; revert
  is `git checkout master -- <files>`. No DB migration required for `main`.

---

## Step count summary

PART A: 4 · PART B: 3 · PART C: 2 · PART D: 3 · PART E: 2 · PART F: 1 ·
PART G: 4  →  **22 steps total.**

## Crash-safety argument (why concurrent bots cannot crash)

1. **Different tokens → no Telegram 409.** Polling streams are independent.
2. **Per-bot spool dir + unique filenames → no inject collision/clobber.** Atomic
   `mv` in, atomic `rename` claim out; FIFO ordering.
3. **Per-bot DB → no SQLite lock contention or session cross-talk.**
4. **Per-bot restart match → killing one never kills another.**
5. **Port assertion fails fast** instead of crashing mid-run (and ports are
   off in the default config).
6. **Fully backward compatible** → single-bot deploys behave exactly as today.

Every shared singleton from §0 (C1–C6) is removed or keyed by `BOT_ID`; no
remaining shared mutable state exists between bot processes.

---

## Implementation status — DONE (all 22 steps, branch `multi_bot`)

| Part | Steps | Where | Verified |
|------|-------|-------|----------|
| A | A1–A4 | `settings.py` (`bot_id`, `inject_dir`, `inject_spool_dir`, `relay_state_path`), `inject_watcher.py` (spool-dir FIFO + atomic claim + legacy shim), `core.py` (wires per-bot spool) | unit tests G1/G3 pass |
| B | B1–B3 | `wake_after.sh` (BOT_ID arg, atomic spool writes), `relay_state_path` | spool write verified (unique file, valid JSON) |
| C | C1–C2 | `settings.py` validator (`sqlite:///data/bot_<id>.db`), `main.py` startup log | derivation verified (work→bot_work.db, main→bot.db) |
| D | D1–D3 | `restart_bot.sh` (scoped match), `start_bot.sh` | bash -n + match logic; `--config-file`→`load_dotenv` confirmed |
| E | E1–E2 | `main.py` `_assert_ports_available` | raise-on-collision + pass-when-free tested |
| F | F1 | `bots.yaml`, `supervise.sh` | `status` detects live main |
| G | G1–G4 | `tests/bot/test_inject_watcher_multibot.py` | 6/6 pass |

### How to run a second bot
1. Create `work.env` with `BOT_ID=work`, a second `TELEGRAM_BOT_TOKEN`, and
   (only if API/webhook enabled) distinct `API_SERVER_PORT`/`WEBHOOK_PORT`.
2. `./start_bot.sh work`  (or set `enabled: true` in `bots.yaml` + `./supervise.sh start`).
3. Wake it: `./wake_after.sh 30 "RELAY: ..." work`.

### G2 — manual 2-bot smoke test (needs two real tokens)
Automated isolation is covered by `test_watcher_consumes_only_its_own_spool`.
Full live check:
1. Start both: `./start_bot.sh main` and `./start_bot.sh work`.
2. `./wake_after.sh 5 "hello-A" main` and `./wake_after.sh 5 "hello-B" work`.
3. Assert each wake lands in **only** its own bot (check `bot.log` /
   `bot_work.log` for the synthetic message; confirm DBs `data/bot.db` vs
   `data/bot_work.db` diverge). No 409 in either log.

### G4 — rollback
All changes are **additive + backward compatible** (default `bot_id="main"`
reproduces legacy behaviour exactly). To revert any file:
`git checkout master -- <file>`. No DB migration is required for `main`
(it keeps `data/bot.db`). Deleting the branch removes everything.
