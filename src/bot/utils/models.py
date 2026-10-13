"""The models a chat may switch between, and what to call them.

A fixed list rather than free text. The CLI accepts anything and only fails on
the *next* message, by which point the mistake looks like the bot broke, so the
set is pinned here and anything else is refused at the command.

Some models exist on the account but need a newer Claude Code than the one a
given bot is running — the API rejects them with "requires version X or newer".
Each entry therefore carries the version it needs, and the list is filtered
against whichever CLI *this* bot actually spawns. The same code then offers a
model on a bot with the new CLI and explains the refusal on a bot still on the
bundled one, instead of hiding it or lying about it.
"""

from __future__ import annotations

import subprocess
from typing import Dict, List, NamedTuple, Optional, Tuple


class ModelChoice(NamedTuple):
    alias: str  # what you type
    model_id: str  # what the CLI is given
    label: str  # what the banner shows
    note: str
    min_cli: str = ""  # Claude Code version this model needs, if any


CHOICES = (
    ModelChoice("opus", "opus", "opus", "latest Opus"),
    ModelChoice("sonnet", "sonnet", "sonnet", "latest Sonnet — faster, cheaper"),
    ModelChoice("haiku", "haiku", "haiku", "fastest, cheapest"),
    ModelChoice("opus5", "claude-opus-5", "opus-5", "pinned Opus 5"),
    ModelChoice(
        "opus5.5", "claude-opus-5-5", "opus-5.5", "pinned Opus 5.5", min_cli="2.1.280"
    ),
    ModelChoice(
        "fable", "claude-fable-5-1", "fable-5.1", "pinned Fable 5.1", min_cli="2.1.251"
    ),
)

_BY_ALIAS = {c.alias: c for c in CHOICES}
_BY_ID = {c.model_id: c for c in CHOICES}


def _parse(version: str) -> Tuple[int, ...]:
    """`2.1.139` -> (2, 1, 139). Anything unparsable sorts as (0,)."""
    parts: List[int] = []
    for chunk in (version or "").split("-")[0].split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts) or (0,)


def supports(choice: ModelChoice, cli_version: Optional[str]) -> bool:
    """Whether a CLI of this version can run this model.

    An unknown version is treated as new enough: refusing everything because
    the version could not be read would be worse than letting the API answer.
    """
    if not choice.min_cli or not cli_version:
        return True
    return _parse(cli_version) >= _parse(choice.min_cli)


def available(cli_version: Optional[str]) -> List[ModelChoice]:
    """The models this CLI can actually run."""
    return [c for c in CHOICES if supports(c, cli_version)]


def unavailable(cli_version: Optional[str]) -> Dict[str, str]:
    """alias -> why it cannot be selected on this CLI."""
    return {
        c.alias: _reason(c, cli_version)
        for c in CHOICES
        if not supports(c, cli_version)
    }


def _reason(choice: ModelChoice, cli_version: Optional[str]) -> str:
    return (
        f"needs Claude Code {choice.min_cli}; "
        f"this bot runs {cli_version or 'unknown'}"
    )


def resolve(text: str, cli_version: Optional[str] = None) -> Optional[ModelChoice]:
    """Find a selectable choice by alias or by full model id.

    None when the name is unknown *or* when this CLI is too old for it; the
    caller tells the two apart with `blocked_reason`.
    """
    key = (text or "").strip().lower()
    choice = _BY_ALIAS.get(key) or _BY_ID.get(key)
    if choice is None or not supports(choice, cli_version):
        return None
    return choice


def blocked_reason(text: str, cli_version: Optional[str] = None) -> Optional[str]:
    """Why a known-but-unusable model is refused, if it is one."""
    key = (text or "").strip().lower()
    choice = _BY_ALIAS.get(key) or _BY_ID.get(key)
    if choice is None or supports(choice, cli_version):
        return None
    return _reason(choice, cli_version)


def label_for(model_id: Optional[str]) -> Optional[str]:
    """Short display name for a model id, for the banner.

    An id set in the env but absent from the list still gets shown — the point
    is to report what is running, not to validate it. A trailing snapshot date
    (``-20250929``) is dropped: it adds noise without telling a human anything
    they can act on, and when the SDK reports the full dated id as a fallback
    the banner shouldn't suddenly double in width because of it.
    """
    if not model_id:
        return None
    choice = _BY_ID.get(model_id)
    if choice:
        return choice.label
    trimmed = model_id.replace("claude-", "")
    # Trailing `-YYYYMMDD`; eight digits is unambiguous enough to be safe.
    tail = trimmed.rsplit("-", 1)[-1]
    if len(tail) == 8 and tail.isdigit():
        trimmed = trimmed[: -(len(tail) + 1)]
    return trimmed


# --- which CLI is this bot actually running ---------------------------------

_version_cache: Dict[str, Optional[str]] = {}


def cli_version(cli_path: Optional[str] = None) -> Optional[str]:
    """The version of the CLI this bot spawns.

    With no explicit path the SDK uses the CLI bundled in the package, whose
    version it records — cheaper and more reliable than running it. With a path
    set, ask that binary once and remember the answer; it cannot change while
    the process lives.
    """
    key = cli_path or ""
    if key in _version_cache:
        return _version_cache[key]

    version: Optional[str] = None
    if not cli_path:
        try:
            from claude_agent_sdk._cli_version import __cli_version__

            version = __cli_version__
        except Exception:
            version = None
    else:
        try:
            out = subprocess.run(
                [cli_path, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            # "2.1.296 (Claude Code)"
            parts = (out.stdout or "").strip().split()
            version = parts[0] if parts else None
        except Exception:
            version = None

    _version_cache[key] = version
    return version
