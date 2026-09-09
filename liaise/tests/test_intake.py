"""Tests for liaise.intake: identification, readiness (quiet window, markers), poll."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from liaise.cli import poll
from liaise.config import Markers, PartnerConfig
from liaise.github import Comment, FakeGitHub, Issue
from liaise.intake import (
    compute_readiness,
    find_partner_issues,
    is_partner_issue,
    last_partner_activity,
)

REPO = "example/app"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _partner(**overrides) -> PartnerConfig:
    fields = dict(
        slug="pat",
        display_name="Pat",
        github_logins=("pat",),
        repo=REPO,
        brief="brief.md",
        label="partner:pat",
        quiet_minutes=10,
        go_minutes=2,
        markers=Markers(go="#startwork#", wait="#wait#"),
    )
    fields.update(overrides)
    return PartnerConfig(**fields)


def _issue(
    *,
    number=1,
    author="pat",
    body="something is broken",
    labels=(),
    comments=(),
    created_at=T0,
    updated_at=None,
):
    return Issue(
        repo=REPO,
        number=number,
        title="a bug",
        author=author,
        body=body,
        created_at=created_at,
        updated_at=updated_at if updated_at is not None else created_at,
        state="open",
        labels=labels,
        comments=comments,
    )


# ---- identification ----


def test_identified_by_author():
    partner = _partner()
    issue = _issue(author="pat", labels=())
    assert is_partner_issue(issue, partner)


def test_identified_by_label_when_author_is_not_partner():
    partner = _partner()
    issue = _issue(author="the-app", labels=("partner:pat",))
    assert is_partner_issue(issue, partner)


def test_not_identified_when_neither_author_nor_label_match():
    partner = _partner()
    issue = _issue(author="someone-else", labels=("bug",))
    assert not is_partner_issue(issue, partner)


# ---- last_partner_activity: quiet-window clock rules ----


def test_last_activity_is_issue_creation_when_no_further_activity():
    partner = _partner()
    issue = _issue(created_at=T0, updated_at=T0)
    assert last_partner_activity(issue, partner) == T0


def test_last_activity_moves_with_partner_comment():
    partner = _partner()
    comment = Comment(
        author="pat", body="also this", created_at=T0 + timedelta(minutes=5), updated_at=T0 + timedelta(minutes=5)
    )
    issue = _issue(created_at=T0, updated_at=T0, comments=(comment,))
    assert last_partner_activity(issue, partner) == T0 + timedelta(minutes=5)


def test_non_partner_comment_does_not_move_the_clock():
    partner = _partner()
    owner_comment = Comment(
        author="owner", body="looking into it", created_at=T0 + timedelta(hours=1), updated_at=T0 + timedelta(hours=1)
    )
    issue = _issue(created_at=T0, updated_at=T0, comments=(owner_comment,))
    # last activity stays at issue creation, NOT the owner's much later comment
    assert last_partner_activity(issue, partner) == T0


def test_non_partner_comment_does_not_make_issue_ready_early():
    partner = _partner(quiet_minutes=10)
    owner_comment = Comment(
        author="owner", body="looking into it", created_at=T0 + timedelta(minutes=9), updated_at=T0 + timedelta(minutes=9)
    )
    issue = _issue(created_at=T0, updated_at=T0, comments=(owner_comment,))
    # 11 minutes after creation: ready by quiet window even though the owner commented
    # at +9m, which would otherwise still be within a 10-minute quiet window.
    now = T0 + timedelta(minutes=11)
    readiness = compute_readiness(issue, partner, now=now)
    assert readiness.ready


# ---- readiness: quiet window ----


def test_not_ready_before_quiet_window_elapses():
    partner = _partner(quiet_minutes=10)
    issue = _issue(created_at=T0, updated_at=T0)
    readiness = compute_readiness(issue, partner, now=T0 + timedelta(minutes=5))
    assert not readiness.ready
    assert readiness.countdown == timedelta(minutes=5)


def test_ready_once_quiet_window_elapses():
    partner = _partner(quiet_minutes=10)
    issue = _issue(created_at=T0, updated_at=T0)
    readiness = compute_readiness(issue, partner, now=T0 + timedelta(minutes=10))
    assert readiness.ready
    assert readiness.countdown == timedelta(0)


# ---- readiness: go marker ----


def test_go_marker_in_body_shortens_wait():
    partner = _partner(quiet_minutes=60, go_minutes=2)
    issue = _issue(body="please fix this #startwork#", created_at=T0, updated_at=T0)
    # far short of the 60-minute quiet window, but past the 2-minute go window
    readiness = compute_readiness(issue, partner, now=T0 + timedelta(minutes=3))
    assert readiness.ready
    assert readiness.reason == "go marker"


def test_go_marker_in_partner_comment():
    partner = _partner(quiet_minutes=60, go_minutes=2)
    comment = Comment(
        author="pat", body="ok #startwork#", created_at=T0 + timedelta(minutes=5), updated_at=T0 + timedelta(minutes=5)
    )
    issue = _issue(created_at=T0, updated_at=T0, comments=(comment,))
    readiness = compute_readiness(issue, partner, now=T0 + timedelta(minutes=8))
    assert readiness.ready


def test_go_marker_in_non_partner_comment_is_ignored():
    partner = _partner(quiet_minutes=60, go_minutes=2)
    comment = Comment(
        author="owner", body="#startwork#", created_at=T0 + timedelta(minutes=5), updated_at=T0 + timedelta(minutes=5)
    )
    issue = _issue(created_at=T0, updated_at=T0, comments=(comment,))
    readiness = compute_readiness(issue, partner, now=T0 + timedelta(minutes=8))
    assert not readiness.ready


def test_marker_matched_case_insensitively():
    partner = _partner(quiet_minutes=60, go_minutes=2)
    issue = _issue(body="Ready now #STARTWORK#", created_at=T0, updated_at=T0)
    readiness = compute_readiness(issue, partner, now=T0 + timedelta(minutes=3))
    assert readiness.ready


# ---- readiness: wait marker pauses ----


def test_wait_marker_pauses_even_past_quiet_window():
    partner = _partner(quiet_minutes=10)
    issue = _issue(body="not yet #wait#", created_at=T0, updated_at=T0)
    readiness = compute_readiness(issue, partner, now=T0 + timedelta(hours=5))
    assert readiness.paused
    assert not readiness.ready


def test_go_after_wait_unpauses():
    partner = _partner(quiet_minutes=60, go_minutes=2)
    wait_comment = Comment(
        author="pat", body="#wait#", created_at=T0 + timedelta(minutes=1), updated_at=T0 + timedelta(minutes=1)
    )
    go_comment = Comment(
        author="pat", body="#startwork#", created_at=T0 + timedelta(minutes=10), updated_at=T0 + timedelta(minutes=10)
    )
    issue = _issue(created_at=T0, updated_at=T0, comments=(wait_comment, go_comment))
    readiness = compute_readiness(issue, partner, now=T0 + timedelta(minutes=13))
    assert readiness.ready
    assert not readiness.paused


def test_wait_after_go_stays_paused():
    partner = _partner(quiet_minutes=60, go_minutes=2)
    go_comment = Comment(
        author="pat", body="#startwork#", created_at=T0 + timedelta(minutes=1), updated_at=T0 + timedelta(minutes=1)
    )
    wait_comment = Comment(
        author="pat", body="#wait#", created_at=T0 + timedelta(minutes=10), updated_at=T0 + timedelta(minutes=10)
    )
    issue = _issue(created_at=T0, updated_at=T0, comments=(go_comment, wait_comment))
    readiness = compute_readiness(issue, partner, now=T0 + timedelta(minutes=13))
    assert readiness.paused


# ---- find_partner_issues: union of author + label queries, deduped ----


def test_find_partner_issues_unions_author_and_label_queries():
    partner = _partner()
    fake = FakeGitHub(
        [
            _issue(number=1, author="pat", labels=(), created_at=T0),
            _issue(number=2, author="the-app", labels=("partner:pat",), created_at=T0 + timedelta(minutes=1)),
            _issue(number=3, author="stranger", labels=(), created_at=T0),
        ]
    )
    issues = find_partner_issues(fake, partner)
    assert [i.number for i in issues] == [1, 2]  # oldest first, stranger excluded


# ---- liaise poll: read-only reporting ----


def test_poll_prints_countdown_and_changes_nothing(tmp_path):
    root = tmp_path / "config"
    (root / "partners").mkdir(parents=True)
    (root / "briefs").mkdir()
    (root / "briefs" / "pat.md").write_text("hi\n")
    (root / "config.toml").write_text(
        f'owner_login = "owner"\nstate_dir = "{tmp_path / "state"}"\n'
    )
    (root / "partners" / "pat.toml").write_text(
        f'display_name = "Pat"\n'
        f'github_logins = ["pat"]\n'
        f'repo = "{REPO}"\n'
        f'brief = "{root / "briefs" / "pat.md"}"\n'
        f'quiet_minutes = 10\n'
    )

    fake = FakeGitHub([_issue(number=1, author="pat", created_at=T0)])
    output = poll(root=str(root), partner="pat", gh=fake)

    assert "partner: pat" in output
    assert "#1" in output
    assert "in " in output or "READY" in output
    # changes nothing: FakeGitHub records no label/comment mutations
    assert fake.labels_created(REPO) == {}
    assert fake.get_issue(REPO, 1).comments == ()
