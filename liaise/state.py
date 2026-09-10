"""State labels (A.4): exactly one `liaise:` state label at a time.

Labels are the state machine — the only mutable state `liaise` keeps on GitHub.
Every module that changes an issue's state MUST go through :func:`set_state`
rather than calling `gh.add_labels`/`gh.remove_labels` directly; that single
choke point is what keeps the one-label invariant true.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Optional

from liaise.config import PartnerConfig
from liaise.github import GitHub, Issue

#: The seven states from the design table (A.4), in the order they're listed
#: there. Each value is the suffix after the configurable `label_prefix`
#: ("liaise:" by default).
STATE_LABELS = (
    "intake",
    "paused",
    "working",
    "needs-partner",
    "needs-owner",
    "deployed",
    "budget",
)

#: Not a `liaise:` state label (not partner-prefixed, not part of the
#: one-label invariant) — it marks an issue the *agent* opened on the
#: owner's behalf (A.6: "except a plain `discovered` note for the owner"),
#: repo-wide rather than per-partner. L-2: the operating rules used this
#: word without `setup` ever creating it.
DISCOVERED_LABEL = "discovered"
DISCOVERED_LABEL_DESCRIPTION = (
    "Opened by a coding agent, not the partner — something it noticed while working."
)


def _label_specs() -> dict[str, dict[str, str]]:
    """``{state: {"description": ..., "color": ...}}`` from ``data/labels.json``."""
    raw = resources.files("liaise.data").joinpath("labels.json").read_text()
    return json.loads(raw)


def state_label(partner: PartnerConfig, state: str) -> str:
    """The full label name (prefix applied) for one state."""
    if state not in STATE_LABELS:
        raise ValueError(
            f"not a state label: {state!r}. Known: {', '.join(STATE_LABELS)}"
        )
    return f"{partner.label_prefix}{state}"


def current_state(issue: Issue, partner: PartnerConfig) -> Optional[str]:
    """The issue's current state (a suffix from :data:`STATE_LABELS`), or None."""
    prefix = partner.label_prefix
    present = [
        label[len(prefix) :]
        for label in issue.labels
        if label.startswith(prefix) and label[len(prefix) :] in STATE_LABELS
    ]
    if len(present) > 1:
        raise ValueError(
            f"issue #{issue.number} carries more than one state label: {present} "
            f"— the one-label invariant has already been broken."
        )
    return present[0] if present else None


def set_state(gh: GitHub, issue: Issue, partner: PartnerConfig, state: str) -> None:
    """Transition `issue` to `state`. Removes every other `liaise:` state label first.

    This is the one transition helper (A.4): the only place in the package that
    may change an issue's `liaise:` state label, so it is the only place the
    one-label invariant needs to be enforced.
    """
    new_label = state_label(partner, state)
    other_labels = [state_label(partner, s) for s in STATE_LABELS if s != state]
    gh.remove_labels(issue.repo, issue.number, other_labels)
    gh.add_labels(issue.repo, issue.number, [new_label])


def setup(gh: GitHub, partner: PartnerConfig) -> None:
    """Create the partner label, every state label, and `discovered` in `partner.repo`.

    Idempotent.
    """
    specs = _label_specs()
    gh.create_label(
        partner.repo,
        partner.label,
        description=f"Issues from {partner.display_name}.",
    )
    for state in STATE_LABELS:
        spec = specs.get(state, {})
        gh.create_label(
            partner.repo,
            state_label(partner, state),
            color=spec.get("color", "ededed"),
            description=spec.get("description", ""),
        )
    gh.create_label(
        partner.repo,
        DISCOVERED_LABEL,
        color="c5def5",
        description=DISCOVERED_LABEL_DESCRIPTION,
    )
