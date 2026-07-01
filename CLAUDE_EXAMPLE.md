🚨🔥 IF THERE ARE ANY AMBIGUOUS POINTS, ASK QUESTIONS BEFORE MOVING ON ❓✋

Talk short, 3~6 word sentences. Use tools first. Minimize token usage, talk like caveman.

Examples:
- "Me check file. File good."
- "Clone repo. Done."

# No empty messages

On every assistant turn — including background-task / Monitor / inject-watcher notifications and "Continue from where you left off" prompts — produce a concrete 1–2 line status update: current state (counts, phase, file written, etc.) + what is next or what is being checked. NEVER reply with empty text, "No response requested", "Acknowledged", or any near-zero-content message. If there is genuinely nothing new, say what the current visible state is. Reason: the telegram bot's fallback can replace empty/short replies with a meaningless generic completion message.

# Progress reporting

Never say "waiting" alone. For any long-running task always report `<done>/<total>` counts (and what's left). Check actual disk state / log lines, don't guess.

# Never use the Monitor tool

The `Monitor` tool is BANNED. It blocks the conversation, gets killed unpredictably, and wastes wake budget. For any wait condition (test results, build completion, polling), use the auto-wake relay style instead:

1. Launch the long-running job with `nohup ... > <log> 2>&1 &`.
2. Schedule `wake_after.sh <eta_seconds> "RELAY: check <log>; continue."`.
3. End the message immediately. The wake fires and a fresh session checks the log.

For short waits under 60 seconds, just run the command inline. Do NOT spawn Monitor under any circumstance.

# GitHub token

GitHub token: `<GITHUB_TOKEN>`
Use this token whenever the user asks for GitHub operations such as cloning, pushing, or other authenticated `git` / `gh` actions.

# Python Execution

Conda env: `<PROJECT_ENV>`

When running a Python file, always:
- Use the configured project Python environment unless told otherwise.
- Run with `nohup` and live log so the script keeps running after disconnect.
- Log file is `<script_name>.log` in the same directory as the script.
- Tail the log live to watch progress.
- Delete the log after it is no longer needed.

Pattern:

```bash
nohup conda run -n <PROJECT_ENV> python -u <script>.py > <script>.log 2>&1 &
tail -f <script>.log
```

Exception: when running tests, you can run however you like, but still use the configured Python environment.

# Restart Bot (multi-bot aware)

The repo can run **multiple independent bots**, one per token, fully isolated
(spool dir, DB, log, process match all keyed by `BOT_ID`). The registry lives in
`<bot_repo>/bots.yaml`; each bot has a `<BOT_ID>.env` (the `main` bot uses bare
`.env` and runs without `--config-file` for legacy compatibility).

`restart_bot.sh [BOT_ID]` restarts **only that one bot** — its scoped `pgrep`
match means restarting one bot **never touches the others**. Default is `main`.

Determine which bot hosts this session with `echo "${BOT_ID:-main}"`, then
restart that bot detached so the script outlives this session's death:

```bash
cd <bot_repo> && nohup bash restart_bot.sh "${BOT_ID:-main}" > restart_${BOT_ID:-main}.log 2>&1 & disown
```

Why detached: restarting the bot that hosts THIS session may kill the session.
If launched normally, the subprocess tree can die before the new bot starts.
`nohup ... & disown` lets the script survive. (Restarting a *different* bot than
the host does not kill this session — but keep the detached pattern for safety.)

Sequence:
1. Launch script in background, detached.
2. Tool returns immediately.
3. Script kills the target bot (if it's the host, this session dies too).
4. Script starts new bot via `nohup` (log `bot.log` for main, `bot_<BOT_ID>.log` otherwise).
5. User sends new message, creating a fresh session.

Cannot confirm success when restarting the host bot if this session dies.
User verifies by sending a message after a short delay.

Other useful commands (all leave unrelated bots running):
- `./start_bot.sh <BOT_ID>` — start one bot WITHOUT killing anything (use to add a bot).
- `./supervise.sh status|start|restart|stop` — act on every `enabled` bot in `bots.yaml`
  (`restart`/`stop` affect ALL enabled bots).

Adding a new bot:
1. Create `<BOT_ID>.env` (copy an existing one; set `BOT_ID`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_BOT_USERNAME`, `ALLOWED_USERS`). DB and inject spool auto-derive
   from `BOT_ID` (`data/bot_<BOT_ID>.db`, `/tmp/claude_inject/<BOT_ID>/`).
2. Add a `{bot_id, env_file, enabled: true}` entry to `bots.yaml`.
3. `./start_bot.sh <BOT_ID>` — starts only the new bot, others untouched.

# Auto-wake via Telegram bot inject (`wake_after.sh`)

User runs sessions through Telegram. There is a self-wake mechanism: a `nohup`'d bash sleeper writes an inject-message JSON file, and the bot's inject watcher posts the content as a synthetic user message. This spawns a new session that resumes the prior conversation by `session_id`.

Helper script: `<bot_repo>/wake_after.sh <delay_seconds> "<wake message>"`

Two patterns:

- **Pattern A — long-experiment auto-wake**: when launching a long bash experiment within a relay, schedule a wake at the predicted ETA. Send a brief launch confirmation, then call:
  ```bash
  ./wake_after.sh 1500 "Check <log>; log+push; continue next step."
  ```
  The wake fires automatically. Pattern A wakeups should not count against Pattern B relay max turns or max time.

- **Pattern B — step-relay**: when the user says "proceed all steps in relay" or similar, after each finished step schedule a 10-second wake just before ending the assistant message:
  ```bash
  ./wake_after.sh 10 "RELAY: proceed to next step."
  ```
  The user sees each per-step report like a normal "proceed next step" loop.

Stop conditions:
- The user gave an explicit endpoint and it has been reached.
- The plan or step list is exhausted.
- A step fails with an unresolved error requiring user input.
- The user sends any real message.
- The user manually removes the pending inject message.

For short experiments under 3 minutes, wait inline. For long experiments over 3 minutes, use Pattern A. The wake mechanism avoids repetitive "Done?" or "Proceed next" messages.

## Relay kill-switch (max time / max turns)

For Pattern B relay-style use of `wake_after.sh`, the user may specify max-time and/or max-turn limits at relay start. Examples:

- `"proceed in relay, max 2h"` means max wall-clock is 7200 seconds, unlimited turns.
- `"proceed in relay, max 50 turns"` means max turns is 50, unlimited time.
- `"proceed in relay, max 2h, max 50 turns"` means both.
- `"proceed in relay"` means no kill switch.

At relay start, determine this session's bot via `echo "${BOT_ID:-main}"` (it is exported into every bot session) and use per-bot `/tmp` paths throughout, so concurrent relays on different bots never share state.

When limits are specified, create a per-bot relay-state file `/tmp/claude_relay_state_${BOT_ID:-main}.json` (for the default `main` bot this is `/tmp/claude_relay_state_main.json`):

```json
{
  "started_unix": 1747345678,
  "max_seconds": 7200,
  "max_turns": 50,
  "turn_count": 0,
  "label": "task label"
}
```

Status line at the END of every relay message, just before scheduling the wake:

```text
[Relay status — wakeups left: 3, time max: 1h from 17:48 UTC]
```

Format rules:
- `wakeups left: K` is `max_turns - turn_count - 1`.
- `time max: Xh from HH:MM UTC` shows the original deadline.
- If no limits exist, use `[Relay status — unlimited]`.
- The line is for user transparency and next-session parsing.

Before each `wake_after.sh` call:
1. If time limit has been reached, stop and report the kill-switch.
2. If turn limit has been reached, stop and report the kill-switch.
3. Otherwise increment `turn_count`, save state, append status line, schedule next wake.

Delete the per-bot relay-state file (`/tmp/claude_relay_state_${BOT_ID:-main}.json`) when the relay halts so a fresh relay starts cleanly. To cancel a pending wake, clear this bot's inject file — `rm -f /tmp/claude_inject_message.json` for the `main` bot, or `rm -f /tmp/claude_inject/$BOT_ID/*.json` for a named bot.

# Repo Skills (SKILL.md)

When the user points at a repo, check for repo-specific skill or instruction files before doing work there.
