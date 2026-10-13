"""The models a chat may switch to, and what to call them.

Four buttons — opus, sonnet, haiku, fable — each an *alias* the Claude CLI
resolves to the newest model of that family, so nothing here is pinned and
nothing needs editing when a new version ships.

A specific model (``claude-opus-5``) is chosen by typing ``/model <name>``.
The CLI accepts any string and only fails on the *next* message, so a typed
name is probed with one tiny request first (`probe`) and refused if the CLI
does not recognise it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import NamedTuple, Optional, Tuple


class ModelChoice(NamedTuple):
    alias: str  # what you type, and what the CLI is given
    note: str


CHOICES = (
    ModelChoice("fable", "latest Fable"),
    ModelChoice("opus", "latest Opus"),
    ModelChoice("sonnet", "latest Sonnet — faster, cheaper"),
    ModelChoice("haiku", "latest Haiku — fastest, cheapest"),
)

_BY_ALIAS = {c.alias: c for c in CHOICES}

# Letters, digits and the punctuation real model ids use (``claude-opus-5-5``,
# ``opus[1m]``). Anything else is not a model name and never reaches the CLI.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]:/]{0,79}$")

PROBE_TIMEOUT_SECONDS = 60


def resolve(text: str) -> Optional[ModelChoice]:
    """The button model matching this alias, or None."""
    return _BY_ALIAS.get((text or "").strip().lower())


def is_wellformed(text: str) -> bool:
    """Whether this could be a model name at all."""
    return bool(_NAME.match(text or ""))


def label_for(model_id: Optional[str]) -> Optional[str]:
    """Short display name for a model id, for the banner.

    Reports what is running rather than validating it. ``claude-`` and a
    trailing snapshot date (``-20250929``) are dropped: noise to a human, and
    it keeps a dated id the SDK reports from widening the banner.
    """
    if not model_id:
        return None
    trimmed = model_id.replace("claude-", "")
    tail = trimmed.rsplit("-", 1)[-1]
    if len(tail) == 8 and tail.isdigit():
        trimmed = trimmed[: -(len(tail) + 1)]
    return trimmed


def _cli_binary(cli_path: Optional[str]) -> Optional[str]:
    """The CLI this bot spawns: the configured one, else the SDK's bundled copy."""
    if cli_path:
        return cli_path
    try:
        import claude_agent_sdk

        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
        return str(bundled) if bundled.exists() else None
    except Exception:
        return None


async def probe(name: str, cli_path: Optional[str] = None) -> Tuple[Optional[bool], str]:
    """Ask the CLI whether it knows this model.

    Returns ``(True, "")`` if it does, ``(False, reason)`` if it says not, and
    ``(None, reason)`` when the check itself could not run — the caller decides
    what an unverifiable name deserves.
    """
    binary = _cli_binary(cli_path)
    if not binary:
        return None, "Claude CLI not found"
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "-p", "Reply: ok", "--model", name, "--max-turns", "1",
            "--tools", "",
            "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd="/tmp",
            env={k: v for k, v in os.environ.items() if k != "CLAUDECODE"},
        )
        out, _ = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return None, "check timed out"
    except Exception as exc:
        return None, f"check failed: {exc}"

    text = out.decode("utf-8", "replace")
    if "unrecognized_model" in text:
        return False, "the CLI does not recognise it"
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            result = json.loads(line)
        except ValueError:
            continue
        # Running out of turns still proves the model answered.
        if isinstance(result, dict) and (
            result.get("is_error") is False
            or result.get("subtype") == "error_max_turns"
        ):
            return True, ""
        break
    return None, "check was inconclusive"
