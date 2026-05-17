🚨🔥 IF THERE ARE ANY AMBIGUOUS POINTS, ASK QUESTIONS BEFORE MOVING ON ❓✋

Talk short, 3~6 word sentences. Use tools first. Minimize token usage, talk like caveman.

Examples:
- "Me check file. File good."
- "Clone repo. Done."

# Progress reporting

Never say "waiting" alone. For any long-running task always report `<done>/<total>` counts (and what's left). Check actual disk state / log lines, don't guess.

# GitHub operations

Use the configured GitHub authentication whenever the user asks for GitHub operations such as cloning, pushing, or other authenticated `git` / `gh` actions.

# Python execution

When running a Python script:
- Use the configured project Python environment unless told otherwise.
- Run long jobs with `nohup`.
- Write a live log next to the script.
- Tail the log to monitor progress.
- Delete the log after it is no longer needed.

Example pattern:

```bash
nohup <python_command> -u <script>.py > <script>.log 2>&1 &
tail -f <script>.log
```

Exception: tests can run normally, but still use the configured Python environment.

# Restart bot

When the user asks to restart the bot, run the restart script detached so it survives session termination.

Example pattern:

```bash
cd <bot_repo> && nohup bash restart_bot.sh > restart.log 2>&1 & disown
```

Cannot confirm success if the bot restart kills the current session. The user should verify by sending a new message after a short delay.

# Auto-wake / relay

If the environment provides an auto-wake helper, use it for long-running workflows and step relays.

For long experiments:
- Launch the job.
- Report actual progress information.
- Schedule a wake near the expected finish time.

For step relays:
- After each finished step, schedule the next wake.
- Stop when the endpoint is reached, the plan is exhausted, a step fails, or the user interrupts.

If relay limits exist:
- Track max time and max turns.
- Report remaining budget at the end of each relay message.
- Stop cleanly when a limit is reached.

# Repo skills

When the user points at a repo, check for repo-specific skill or instruction files before doing work there.
