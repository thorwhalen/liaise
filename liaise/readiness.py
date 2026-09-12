"""Readiness: whether a case is ready to dispatch, read off its own ledger entries.

The 0.0.x arithmetic of ``liaise.intake``, unchanged, over a
:class:`~liaise.model.Case` instead of a GitHub issue:

- **Quiet window.** Ready once ``quiet_minutes`` have passed since the partner's last
  activity: the case's creation, or a ``message`` entry by one of ``partner_persons``.
  Nothing else moves that clock. That excludes other people's messages, liaise's own
  entries, and ``case.updated_at``, which every entry moves (H-2 in 0.0.x).
- **Go marker.** A partner message holding the go marker makes the case ready
  ``go_minutes`` after it, when that comes first.
- **Wait marker.** A partner message holding the wait marker, later than the last go
  marker, pauses the case until the next go marker, overriding both.

Markers are literal, case-insensitive substrings of a message entry's ``text``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from liaise.config import Markers
from liaise.model import Case, LedgerEntry


def _person_set(partner_persons: Collection[str]) -> frozenset[str]:
    if isinstance(partner_persons, str):
        raise TypeError(
            f"partner_persons must be a collection of person ids, not the string "
            f"{partner_persons!r}; pass ({partner_persons!r},)"
        )
    return frozenset(partner_persons)


def _partner_messages(case: Case, partners: frozenset[str]) -> Iterator[LedgerEntry]:
    return (e for e in case.entries if e.kind == "message" and e.actor in partners)


def last_partner_activity(case: Case, *, partner_persons: Collection[str]) -> datetime:
    """The latest of: the case's creation, and the partner's own message entries."""
    partners = _person_set(partner_persons)
    return max([case.created_at, *(e.at for e in _partner_messages(case, partners))])


@dataclass(frozen=True)
class Readiness:
    """The result of :func:`compute_readiness`."""

    ready: bool
    paused: bool
    last_activity: datetime
    countdown: timedelta  # zero once ready or paused
    reason: str


def compute_readiness(
    case: Case,
    *,
    quiet_minutes: int,
    go_minutes: int,
    markers: Markers,
    partner_persons: Collection[str],
    now: Optional[datetime] = None,
) -> Readiness:
    """Is ``case`` ready to dispatch, right now?

    Ready when ``now - last_activity >= quiet_minutes``, or when a go marker appeared
    and ``now - marker_time >= go_minutes``. A wait marker later than the last go
    marker pauses the case until the next go marker, overriding both.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    partners = _person_set(partner_persons)

    def contains(text: Optional[str], marker: str) -> bool:
        return marker.lower() in (text or "").lower()

    last_go: Optional[datetime] = None
    last_wait: Optional[datetime] = None
    for entry in sorted(_partner_messages(case, partners), key=lambda e: e.at):
        if contains(entry.text, markers.go):
            last_go = entry.at
        if contains(entry.text, markers.wait):
            last_wait = entry.at
    paused = last_wait is not None and (last_go is None or last_wait > last_go)
    activity = last_partner_activity(case, partner_persons=partners)

    if paused:
        return Readiness(
            ready=False,
            paused=True,
            last_activity=activity,
            countdown=timedelta(0),
            reason="partner asked to wait",
        )

    quiet_deadline = activity + timedelta(minutes=quiet_minutes)
    go_deadline = (
        last_go + timedelta(minutes=go_minutes) if last_go is not None else None
    )

    ready_by_quiet = now >= quiet_deadline
    ready_by_go = go_deadline is not None and now >= go_deadline

    if ready_by_quiet or ready_by_go:
        reason = (
            "go marker"
            if ready_by_go and not ready_by_quiet
            else "quiet window elapsed"
        )
        return Readiness(
            ready=True,
            paused=False,
            last_activity=activity,
            countdown=timedelta(0),
            reason=reason,
        )

    deadlines = [quiet_deadline] + ([go_deadline] if go_deadline is not None else [])
    return Readiness(
        ready=False,
        paused=False,
        last_activity=activity,
        countdown=min(deadlines) - now,
        reason="waiting",
    )
