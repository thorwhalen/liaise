"""Intake and readiness (A.3): which issues are the partner's, and when they're ready.

Everything here is read-only — it answers questions about an :class:`~liaise.github.Issue`
and a :class:`~liaise.config.PartnerConfig`. Applying the answer (adding labels on first
sight, dispatching a ready issue) is `run.py`'s job (a later issue); this module only
computes.

**A caveat about "last edit by the partner".** ``issue.updated_at`` looks like the
obvious signal for this — it is not one: GitHub bumps it on *anyone's* comment and on
every label change, including `liaise`'s own. Verified on a live issue: the issue's
`updatedAt` matched its last comment's `createdAt` to the second, regardless of who
posted the comment. Using it here would mean an owner "just thinking out loud" in the
thread, or `liaise`'s own two-label-edit-per-transition `set_state` call, resets the
partner's own quiet window — exactly the rule A.3 forbids ("the owner commenting is not
the partner writing"). There is no clean "the author edited the body at this time"
signal available from `gh`'s JSON (the GraphQL `lastEditedAt` field is not among the
fields `gh issue view --json` exposes), so :func:`last_partner_activity` only trusts the
issue's creation time and the partner's own comments — never `issue.updated_at`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from liaise.config import PartnerConfig
from liaise.github import GitHub, Issue


def is_partner_issue(issue: Issue, partner: PartnerConfig) -> bool:
    """True when `issue` belongs to `partner`: authored by them, or carrying their label.

    (a) the author is one of the partner's `github_logins`, or (b) the issue carries the
    partner's label (filed on their behalf through the owner's credentials, so the author
    is not the partner).
    """
    return issue.author in partner.github_logins or partner.label in issue.labels


def last_partner_activity(issue: Issue, partner: PartnerConfig) -> datetime:
    """The latest of: the issue's creation, and the partner's own comments.

    Deliberately never reads `issue.updated_at` — see the module docstring
    (H-2): that field moves on anyone's activity, not just the partner's.
    Activity by anyone else never contributes here.
    """
    candidates = [issue.created_at]
    for comment in issue.comments:
        if comment.author in partner.github_logins:
            candidates.append(comment.created_at)
    return max(candidates)


def _contains_marker(text: str, marker: str) -> bool:
    return marker.lower() in text.lower()


def _marker_events(issue: Issue, partner: PartnerConfig) -> list[tuple[datetime, bool, bool]]:
    """(timestamp, has_go, has_wait) for the body and every partner comment, time-ordered."""
    events: list[tuple[datetime, bool, bool]] = []

    body_go = _contains_marker(issue.body, partner.markers.go)
    body_wait = _contains_marker(issue.body, partner.markers.wait)
    if body_go or body_wait:
        events.append((issue.created_at, body_go, body_wait))

    for comment in issue.comments:
        if comment.author not in partner.github_logins:
            continue
        go = _contains_marker(comment.body, partner.markers.go)
        wait = _contains_marker(comment.body, partner.markers.wait)
        if go or wait:
            events.append((comment.created_at, go, wait))

    events.sort(key=lambda e: e[0])
    return events


def _marker_state(
    issue: Issue, partner: PartnerConfig
) -> tuple[Optional[datetime], bool]:
    """Return (timestamp of the last go marker, whether a later wait marker pauses it)."""
    last_go: Optional[datetime] = None
    last_wait: Optional[datetime] = None
    for ts, go, wait in _marker_events(issue, partner):
        if go:
            last_go = ts
        if wait:
            last_wait = ts
    paused = last_wait is not None and (last_go is None or last_wait > last_go)
    return last_go, paused


@dataclass(frozen=True)
class Readiness:
    """The result of :func:`compute_readiness`."""

    ready: bool
    paused: bool
    last_activity: datetime
    countdown: timedelta  # zero once ready or paused
    reason: str


def compute_readiness(
    issue: Issue, partner: PartnerConfig, *, now: Optional[datetime] = None
) -> Readiness:
    """Is `issue` ready to dispatch, right now?

    Ready when `now - last_activity >= quiet_minutes`, or when a go marker appeared and
    `now - marker_time >= go_minutes`. A wait marker later than the last go marker
    suspends the issue until the next go marker, overriding both.
    """
    now = now if now is not None else datetime.now(timezone.utc)

    last_go, paused = _marker_state(issue, partner)
    activity = last_partner_activity(issue, partner)

    if paused:
        return Readiness(
            ready=False,
            paused=True,
            last_activity=activity,
            countdown=timedelta(0),
            reason="partner asked to wait",
        )

    quiet_deadline = activity + timedelta(minutes=partner.quiet_minutes)
    go_deadline = (
        last_go + timedelta(minutes=partner.go_minutes) if last_go is not None else None
    )

    ready_by_quiet = now >= quiet_deadline
    ready_by_go = go_deadline is not None and now >= go_deadline

    if ready_by_quiet or ready_by_go:
        reason = "go marker" if ready_by_go and not ready_by_quiet else "quiet window elapsed"
        return Readiness(
            ready=True,
            paused=False,
            last_activity=activity,
            countdown=timedelta(0),
            reason=reason,
        )

    deadlines = [quiet_deadline] + ([go_deadline] if go_deadline is not None else [])
    countdown = min(deadlines) - now
    return Readiness(
        ready=False,
        paused=False,
        last_activity=activity,
        countdown=countdown,
        reason="waiting",
    )


def find_partner_issues(
    gh: GitHub, partner: PartnerConfig, *, state: str = "open"
) -> list[Issue]:
    """Every open issue belonging to `partner`, oldest first.

    Queries `gh` once per `github_login` plus once by label (the union `is_partner_issue`
    describes), then de-duplicates by issue number.
    """
    seen: dict[int, Issue] = {}
    for login in partner.github_logins:
        for issue in gh.list_issues(partner.repo, author=login, state=state):
            seen[issue.number] = issue
    for issue in gh.list_issues(partner.repo, label=partner.label, state=state):
        seen[issue.number] = issue
    return sorted(
        (i for i in seen.values() if is_partner_issue(i, partner)),
        key=lambda i: i.created_at,
    )
