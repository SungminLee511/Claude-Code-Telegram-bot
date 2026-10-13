"""How hard the model should think, and what to call each level.

`effort` is the CLI's dial on thinking depth, from `low` to `max`. The levels
are fixed by the API, so unlike the model list there is nothing to discover at
runtime — but the same rule applies: only these five words are accepted, and
anything else is refused at the command rather than at the next message.

Leaving it unset is a real choice and the default one: the model then decides
for itself how much to think, which is right for most turns.
"""

from __future__ import annotations

from typing import NamedTuple, Optional, Tuple


class EffortChoice(NamedTuple):
    alias: str  # what you type, and what the CLI is given
    note: str


# Ordered cheapest to most thorough, which is how the list reads best.
CHOICES: Tuple[EffortChoice, ...] = (
    EffortChoice("low", "barely thinks — fastest, cheapest"),
    EffortChoice("medium", "moderate thinking"),
    EffortChoice("high", "thinks hard"),
    EffortChoice("xhigh", "thinks very hard — slow"),
    EffortChoice("max", "no limit — slowest, most expensive"),
)

# Typing `/effort auto` is how you undo a choice and hand the decision back.
AUTO = "auto"

_BY_ALIAS = {c.alias: c for c in CHOICES}


def resolve(text: str) -> Optional[EffortChoice]:
    """Find a level by name. None if it is not one of the five."""
    return _BY_ALIAS.get((text or "").strip().lower())


def is_auto(text: str) -> bool:
    """Whether this asks to clear the setting rather than set one."""
    return (text or "").strip().lower() in (AUTO, "default", "none", "off")


def label_for(effort: Optional[str]) -> str:
    """What the banner shows.

    Nothing pinned is still a state worth naming — it is the one where the
    model picks its own depth per turn — so it reads `auto` rather than
    vanishing. A banner that silently omits the setting looks like the
    setting is broken.
    """
    if not effort:
        return AUTO
    return effort if effort in _BY_ALIAS else str(effort)
