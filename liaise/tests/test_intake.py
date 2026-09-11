"""Tests for liaise.intake.intake: polling, dedupe, routing, access, adoption and dry runs.

Every channel is a fake from liaise.testing, heard through correspond's own listen.
Readiness, which 0.0.x computed in liaise.intake, is tested in test_readiness.py. acquaint
is made unimportable, so no test reads real people records.
"""

from __future__ import annotations

import copy
import importlib
import sys
from collections import ChainMap
from datetime import datetime, timedelta, timezone

import correspond
import pytest
from correspond.channels.github import LISTEN_LOOKBACK
from correspond.channels.webinbox import WebInbox
from correspond.errors import ChannelError

from liaise.intake import intake
from liaise.ledger import Ledger
from liaise.readiness import last_partner_activity
from liaise.subjects import Policy, Subject
from liaise.testing import SELF_LOGIN, FakeGitHubChannel, add_webinbox_report, demo_registry

#: The module itself: `liaise` exports the function `intake` under the module's name.
intake_module = importlib.import_module("liaise.intake")

T0 = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=1)
#: Older than the day correspond's GitHub adapter looks back on its first poll.
OLD = NOW - timedelta(days=3)
REPO = "example/app"
REPO_REF = "github:example/app"
SITE = "example-site"
BINDING = "github:example/app?labels=partner:pat"
AUTHOR_BINDING = "github:example/app?author=pat"
WEB_BINDING = "webinbox:example-site"
ISSUE_12 = "github:example/app#12"


@pytest.fixture(autouse=True)
def no_real_acquaint(monkeypatch):
    monkeypatch.setitem(sys.modules, "acquaint", None)


def _subject(*, bindings=(BINDING, WEB_BINDING), slug="pat") -> Subject:
    return Subject(
        slug=slug,
        bindings=bindings,
        policy=Policy(
            people={"github:pat": "pat", "webinbox:pat": "pat"},
            roles={"pat": "partner"},
            relays=("github:example-bot",),
            claim_labels={"partner:pat": "pat"},
        ),
    )


@pytest.fixture
def github() -> FakeGitHubChannel:
    return FakeGitHubChannel()


@pytest.fixture
def webinbox() -> WebInbox:
    return WebInbox(store={}, blobs={})


@pytest.fixture
def registry(github, webinbox):
    return demo_registry(github=github, webinbox=webinbox)


@pytest.fixture
def ledger() -> Ledger:
    return Ledger({})


def _intake(ledger, registry, subject=None, **kwargs):
    return intake(subject or _subject(), ledger, registry=registry, now=NOW, **kwargs)


def _issue(github, number=12, *, author="pat", labels=("partner:pat",), minutes=0, at=None, **kwargs):
    return github.add_issue(
        REPO,
        number,
        author=author,
        title="Export",
        body="The export drops the last row.",
        labels=labels,
        created_at=at or T0 + timedelta(minutes=minutes),
        **kwargs,
    )


def _looking_back(webinbox):
    """A fake GitHub whose first poll looks back no further than correspond's adapter does, from NOW."""
    github = FakeGitHubChannel(lookback=LISTEN_LOOKBACK, clock=lambda: NOW)
    return github, demo_registry(github=github, webinbox=webinbox)


# ---- new cases ----


def test_a_partner_authored_issue_becomes_an_intake_case(github, registry, ledger):
    _issue(github)
    report = _intake(ledger, registry)

    (case,) = report.new_cases
    assert (case.id, case.subject, case.state, case.reporter) == ("pat-1", "pat", "intake", "pat")
    assert (case.conversations, case.created_at) == ((ISSUE_12,), T0)
    (entry,) = case.entries
    assert (entry.kind, entry.actor, entry.grade, entry.permission) == ("message", "pat", "platform", "report")
    assert (entry.at, entry.text) == (T0, "The export drops the last row.")
    assert entry.delivery_id == "github:example/app:issue-12@2026-09-11T09:00:00Z"
    assert entry.detail["via"] == "handle"
    assert ledger.get_case("pat-1") == case
    assert ledger.seen(entry.delivery_id)
    assert (report.updated_cases, report.unrouted, report.problems) == ((), (), ())
    assert [(e.action, e.case_id) for e in report.events] == [("opened", "pat-1")]


def test_a_relay_authored_issue_with_a_claim_label_is_a_case_for_the_claimed_person(github, registry, ledger):
    _issue(github, 13, author="example-bot")
    (case,) = _intake(ledger, registry).new_cases
    assert case.reporter == "pat"
    (entry,) = case.entries
    assert (entry.actor, entry.detail["via"]) == ("pat", "relay-label")


def test_a_claim_label_from_an_untrusted_author_goes_to_unrouted(github, registry, ledger):
    _issue(github, 14, author="someone-else")
    report = _intake(ledger, registry)

    assert report.new_cases == ()
    assert list(ledger.cases()) == []
    (item,) = report.unrouted
    assert item["reason"] == "label claim by an untrusted author"
    assert (item["author"], item["subject"], item["conversation"]) == (
        "github:someone-else",
        "pat",
        "github:example/app#14",
    )
    (queued,) = ledger.unrouted()
    assert queued["reason"] == "label claim by an untrusted author"
    assert [e.action for e in report.events] == ["unrouted"]


def test_a_web_inbox_report_from_a_person_in_policy_becomes_a_case_of_its_own(registry, ledger, webinbox):
    first = add_webinbox_report(webinbox, SITE, text="The chart is blank.", user="pat", received_at=T0)
    second = add_webinbox_report(
        webinbox, SITE, text="The export is slow.", user="pat", received_at=T0 + timedelta(minutes=1)
    )
    report = _intake(ledger, registry)

    assert [c.conversations for c in report.new_cases] == [
        (f"webinbox:{SITE}#{first['id']}",),
        (f"webinbox:{SITE}#{second['id']}",),
    ]
    case = report.new_cases[0]
    assert (case.reporter, case.state) == ("pat", "intake")
    (entry,) = case.entries
    assert (entry.actor, entry.grade, entry.text) == ("pat", "bound", "The chart is blank.")


def test_an_unsigned_web_inbox_report_is_unrouted(registry, ledger, webinbox):
    add_webinbox_report(webinbox, SITE, text="Broken.", name="Pat", received_at=T0)
    (item,) = _intake(ledger, registry).unrouted
    assert (item["grade"], item["reason"]) == ("claimed", "unresolved sender")


def test_a_closed_issue_and_an_unbound_issue_open_no_case(github, registry, ledger):
    _issue(github, 20, state="closed")
    _issue(github, 21, labels=("bug",))
    report = _intake(ledger, registry)
    assert (report.new_cases, report.unrouted) == ((), ())
    assert [(e.action, e.reason) for e in report.events] == [
        ("ignored", "a closed issue opens no case"),
        ("ignored", "matches no binding"),
    ]
    assert not any(ledger.seen(e.delivery_id) for e in report.events)


# ---- known conversations ----


def test_a_comment_on_a_known_case_adds_an_entry(github, registry, ledger):
    _issue(github)
    _intake(ledger, registry)
    github.add_comment(REPO, 12, author="pat", body="Also on Safari.", created_at=T0 + timedelta(minutes=5))
    report = _intake(ledger, registry)

    assert report.new_cases == ()
    (case,) = report.updated_cases
    assert case.id == "pat-1"
    assert [e.text for e in case.entries] == ["The export drops the last row.", "Also on Safari."]
    latest = case.entries[-1]
    assert (latest.actor, latest.permission, latest.at) == ("pat", "report", T0 + timedelta(minutes=5))
    assert last_partner_activity(case, partner_persons=("pat",)) == T0 + timedelta(minutes=5)


def test_a_self_comment_is_recorded_as_self_and_is_not_partner_activity(github, registry, ledger):
    _issue(github)
    _intake(ledger, registry)
    github.add_comment(
        REPO, 12, author=SELF_LOGIN, body="Working on it.", created_at=T0 + timedelta(minutes=30), is_self=True
    )
    (case,) = _intake(ledger, registry).updated_cases

    latest = case.entries[-1]
    assert (latest.actor, latest.permission, latest.text) == ("self", None, "Working on it.")
    assert latest.detail["role"] == "self"
    assert last_partner_activity(case, partner_persons=("pat",)) == T0


def test_a_relay_comment_is_recorded_as_the_relays_and_is_neither_partner_activity_nor_unrouted(
    github, registry, ledger
):
    _issue(github)
    _intake(ledger, registry)
    github.add_comment(
        REPO, 12, author="example-bot", body="Pat adds: also on Safari.", created_at=T0 + timedelta(minutes=30)
    )
    report = _intake(ledger, registry)

    assert report.unrouted == ()
    (case,) = report.updated_cases
    latest = case.entries[-1]
    assert (latest.actor, latest.permission, latest.grade) == ("github:example-bot", None, "platform")
    assert latest.detail["role"] == "relay"
    assert last_partner_activity(case, partner_persons=("pat",)) == T0
    assert [(e.action, e.reason) for e in report.events] == [("appended", "a relay")]


def test_a_comment_on_a_known_case_from_an_unknown_sender_is_unrouted(github, registry, ledger):
    _issue(github)
    _intake(ledger, registry)
    github.add_comment(REPO, 12, author="someone-else", body="Me too.", created_at=T0 + timedelta(minutes=5))
    report = _intake(ledger, registry)

    assert report.updated_cases == ()
    (item,) = report.unrouted
    assert item["reason"] == "unresolved sender"
    assert len(ledger.get_case("pat-1").entries) == 1


def test_a_message_on_another_subjects_case_is_left_to_that_subject(github, registry, ledger):
    _issue(github)
    _intake(ledger, registry, _subject(slug="example-app"))
    github.add_comment(REPO, 12, author="pat", body="Also on Safari.", created_at=T0 + timedelta(minutes=5))
    del ledger.cursors[REPO_REF]  # so that subject pat hears both again
    report = _intake(ledger, registry)

    assert [e.action for e in report.events] == ["duplicate", "ignored"]
    assert report.events[1].case_id == "example-app-1"
    assert len(ledger.get_case("example-app-1").entries) == 1


# ---- dedupe ----


def test_a_delivery_already_seen_is_skipped(github, registry, ledger):
    _issue(github)
    _intake(ledger, registry)
    del ledger.cursors[REPO_REF]  # the channel repeats itself, as after a crash
    report = _intake(ledger, registry)

    assert [e.action for e in report.events] == ["duplicate"]
    assert (report.new_cases, report.updated_cases) == ((), ())
    assert [c.id for c in ledger.cases()] == ["pat-1"]
    assert len(ledger.get_case("pat-1").entries) == 1


# ---- dry runs ----


def test_a_dry_run_over_a_chainmap_leaves_the_base_store_unchanged(github, registry, webinbox):
    base: dict = {}
    _issue(github)
    _intake(Ledger(base), registry)
    github.add_comment(REPO, 12, author="pat", body="Also on Safari.", created_at=T0 + timedelta(minutes=5))
    _issue(github, 15, minutes=10)
    add_webinbox_report(webinbox, SITE, text="The chart is blank.", user="pat", received_at=T0)
    snapshot = copy.deepcopy(base)

    dry = Ledger(ChainMap({}, base))
    report = _intake(dry, registry, dry_run=True)

    assert report.dry_run
    assert sorted(c.id for c in report.new_cases) == ["pat-2", "pat-3"]
    assert [c.id for c in report.updated_cases] == ["pat-1"]
    assert dry.get_case("pat-2") is not None  # the dry ledger sees its own writes
    assert base == snapshot
    # nothing was committed, so a real intake afterwards takes in the same events
    real = _intake(Ledger(base), registry)
    assert sorted(c.id for c in real.new_cases) == ["pat-2", "pat-3"]
    assert [c.id for c in real.updated_cases] == ["pat-1"]


def test_a_dry_run_over_a_plain_store_writes_nothing_either(github, registry, ledger):
    _issue(github)
    report = _intake(ledger, registry, dry_run=True)
    assert [c.id for c in report.new_cases] == ["pat-1"]
    assert ledger.store == {}


# ---- legacy adoption ----


def test_an_issue_with_a_legacy_state_label_is_adopted_in_that_state(github, registry, ledger):
    _issue(github, 7, labels=("partner:pat", "bug", "liaise:needs-partner"))
    (case,) = _intake(ledger, registry).new_cases

    assert case.state == "needs-partner"
    message, transition, adopted = case.entries
    assert message.kind == "message"
    assert (transition.kind, transition.actor, transition.at) == ("transition", "liaise", NOW)
    # S7 #3: taken over from 0.0.x, it waits for the partner to write after this
    assert (adopted.kind, adopted.at, adopted.detail) == ("run", NOW, {"event": "adopted", "adopted": True})
    assert transition.detail == {
        "from": "intake",
        "to": "needs-partner",
        "reason": "adopted from the label liaise:needs-partner",
    }


def test_an_issue_with_several_state_labels_is_adopted_for_the_operator(github, registry, ledger):
    _issue(github, 8, labels=("partner:pat", "liaise:working", "liaise:deployed"))
    report = _intake(ledger, registry)

    (case,) = report.new_cases
    assert case.state == "needs-owner"
    (problem,) = report.problems
    assert "liaise:working, liaise:deployed" in problem and case.id in problem


# ---- bindings, failures, and the plan ----


def test_a_wildcard_binding_is_reported_as_a_problem_and_not_polled(github, registry, ledger):
    _issue(github)
    report = _intake(ledger, registry, _subject(bindings=("github:example/*?labels=partner:pat", WEB_BINDING)))

    (problem,) = report.problems
    assert "github:example/*?labels=partner:pat" in problem and "wildcard" in problem
    assert report.new_cases == ()
    assert REPO_REF not in ledger.cursors


def test_a_channel_that_fails_is_a_problem_and_the_other_bindings_still_run(github, ledger):
    _issue(github)
    subject = _subject(bindings=(WEB_BINDING, BINDING))
    report = intake(subject, ledger, registry={"github": github}, now=NOW)

    assert [c.id for c in report.new_cases] == ["pat-1"]
    (problem,) = report.problems
    assert "polling webinbox:example-site failed" in problem


def test_plan_lines_show_the_events_the_cases_and_the_problems(github, registry, ledger):
    _issue(github)
    _issue(github, 14, author="someone-else")
    subject = _subject(bindings=(BINDING, WEB_BINDING, "github:example/*"))
    lines = _intake(ledger, registry, subject, dry_run=True).plan_lines()

    assert lines[0] == "intake pat: 2 new events [dry run]"
    assert (
        "  github:example/app#12 from github:pat: opened pat-1 "
        "(github:example/app?labels=partner:pat matched; pat via handle; open before the first poll, adopted)"
    ) in lines
    assert "  github:example/app#14 from github:someone-else: unrouted: label claim by an untrusted author" in lines
    assert "  case pat-1: created, state intake, reporter pat" in lines
    assert any(line.startswith("  problem: ") and "wildcard" in line for line in lines)


# ---- several bindings on one conversation ----


def test_bindings_on_one_conversation_poll_it_once_and_each_message_meets_them_all(
    github, registry, ledger, monkeypatch
):
    # the third binding's conversation differs only in case: correspond keeps one cursor for all three
    subject = _subject(bindings=(BINDING, AUTHOR_BINDING, "github:Example/App?labels=bug"))
    _intake(ledger, registry, subject)  # a first poll, so the one below is a plain poll, with no adoption
    polls = []
    poll = github.poll
    monkeypatch.setattr(github, "poll", lambda ref, **kwargs: polls.append(ref.encoded) or poll(ref, **kwargs))
    _issue(github, 12)
    _issue(github, 13, labels=())
    _issue(github, 14, author="someone-else", labels=())
    report = _intake(ledger, registry, subject)

    assert polls == [REPO_REF]
    assert [(e.conversation, e.action) for e in report.events] == [
        (ISSUE_12, "opened"),
        ("github:example/app#13", "opened"),
        ("github:example/app#14", "ignored"),
    ]
    assert report.events[0].reason.startswith(f"{BINDING} matched")
    assert report.events[1].reason.startswith(f"{AUTHOR_BINDING} matched")


# ---- adopting open issues before a repository's first poll ----


def test_an_open_issue_older_than_the_first_polls_lookback_is_adopted(webinbox, ledger):
    github, registry = _looking_back(webinbox)
    _issue(github, at=OLD)
    _issue(github, 21, labels=("bug",), at=OLD)  # open, but no binding matches it
    assert list(correspond.listen(REPO_REF, cursors={}, registry=registry)) == []  # a poll alone never hears them
    report = _intake(ledger, registry)

    (case,) = report.new_cases
    assert (case.id, case.state, case.reporter, case.conversations) == ("pat-1", "intake", "pat", (ISSUE_12,))
    (entry,) = case.entries
    assert (entry.at, entry.actor, entry.permission, entry.detail["via"]) == (OLD, "pat", "report", "handle")
    assert entry.delivery_id == "github:example/app:issue-12@2026-09-08T10:00:00Z"
    assert ledger.seen(entry.delivery_id)
    ((action, reason),) = [(e.action, e.reason) for e in report.events]
    assert action == "opened" and reason.endswith("; open before the first poll, adopted")
    assert REPO_REF in ledger.cursors  # the poll still ran


def test_an_adopted_issue_is_neither_taken_again_by_the_poll_nor_adopted_again(github, registry, ledger):
    reads = []
    read = github.read
    github.read = lambda ref, **kwargs: reads.append(ref.encoded) or read(ref, **kwargs)
    _issue(github, at=OLD)  # this fake's first poll hears it too, as a lookback covering it would

    first = _intake(ledger, registry)
    second = _intake(ledger, registry)

    assert [(e.action, e.case_id) for e in first.events] == [("opened", "pat-1")]
    (case,) = first.new_cases
    assert len(case.entries) == 1
    assert (second.events, second.new_cases, second.updated_cases) == ((), (), ())
    assert reads == [REPO_REF, ISSUE_12]  # its open issues, then the adopted one's comments
    assert [c.id for c in ledger.cases()] == ["pat-1"]


def test_an_old_issue_with_a_legacy_state_label_is_adopted_in_that_state(webinbox, ledger):
    github, registry = _looking_back(webinbox)
    _issue(github, 7, labels=("partner:pat", "liaise:needs-partner"), at=OLD)
    (case,) = _intake(ledger, registry).new_cases

    assert case.state == "needs-partner"
    message, transition, adopted = case.entries
    assert message.delivery_id == "github:example/app:issue-7@2026-09-08T10:00:00Z"
    assert adopted.detail == {"event": "adopted", "adopted": True}
    assert transition.detail == {
        "from": "intake",
        "to": "needs-partner",
        "reason": "adopted from the label liaise:needs-partner",
    }


def test_an_old_issue_claimed_by_an_untrusted_author_is_unrouted_when_adopted(webinbox, ledger):
    github, registry = _looking_back(webinbox)
    _issue(github, 14, author="someone-else", at=OLD)
    report = _intake(ledger, registry)

    assert report.new_cases == ()
    (item,) = report.unrouted
    assert (item["reason"], item["conversation"]) == ("label claim by an untrusted author", "github:example/app#14")


def test_adoption_in_a_dry_run_writes_nothing_and_a_real_intake_then_adopts(webinbox, ledger):
    github, registry = _looking_back(webinbox)
    _issue(github, at=OLD)
    report = _intake(ledger, registry, dry_run=True)

    assert [c.id for c in report.new_cases] == ["pat-1"]
    assert ledger.store == {}
    assert [c.id for c in _intake(ledger, registry).new_cases] == ["pat-1"]


def test_a_failed_adoption_read_is_a_problem_and_the_repository_is_not_polled_until_it_succeeds(webinbox, ledger):
    github, registry = _looking_back(webinbox)
    _issue(github, at=OLD)

    def refuse(ref, **kwargs):
        raise ChannelError("gh is not logged in", kind="auth")

    github.read = refuse
    report = _intake(ledger, registry)

    (problem,) = report.problems
    assert "polling github:example/app failed" in problem and "gh is not logged in" in problem
    assert REPO_REF not in ledger.cursors
    del github.read
    assert [c.id for c in _intake(ledger, registry).new_cases] == ["pat-1"]


def test_a_repository_with_more_open_issues_than_one_read_takes_says_so(github, registry, ledger, monkeypatch):
    monkeypatch.setattr(intake_module, "ADOPTION_READ_LIMIT", 1)
    _issue(github, 12)
    _issue(github, 13, minutes=1)
    (problem,) = _intake(ledger, registry).problems
    assert "github:example/app has 1 or more open issues" in problem


# ---- an adopted issue's comments (S7 #3) ----


def test_an_adopted_issues_comments_are_taken_in_once_under_the_polls_delivery_ids(webinbox, ledger):
    github, registry = _looking_back(webinbox)
    _issue(github, 7, labels=("partner:pat", "liaise:needs-partner"), at=OLD)
    github.add_comment(REPO, 7, author="pat", body="#wait# until Friday.", created_at=OLD + timedelta(hours=1))
    github.add_comment(
        REPO,
        7,
        author=SELF_LOGIN,
        body="Which page?",
        created_at=OLD + timedelta(hours=2),
        edited_at=NOW - timedelta(minutes=30),  # within the first poll's lookback, edited
        is_self=True,
    )
    github.add_comment(REPO, 7, author="pat", body="The export page.", created_at=NOW - timedelta(hours=1))

    report = _intake(ledger, registry)

    (case,) = report.new_cases
    assert case.state == "needs-partner"
    messages = [e for e in case.entries if e.kind == "message"]
    # the poll hears the two recent comments too, under the same delivery ids: no second entry
    assert [(e.actor, e.text) for e in messages] == [
        ("pat", "The export drops the last row."),
        ("pat", "#wait# until Friday."),
        ("self", "Which page?"),
        ("pat", "The export page."),
    ]
    assert messages[2].delivery_id == "github:example/app:issuecomment-2@2026-09-11T09:30:00Z"
    assert all(ledger.seen(e.delivery_id) for e in messages)
    assert last_partner_activity(case, partner_persons=("pat",)) == NOW - timedelta(hours=1)
    (adopted,) = [e for e in case.entries if e.kind == "run"]
    assert (adopted.at, adopted.actor, adopted.detail) == (NOW, "liaise", {"event": "adopted", "adopted": True})

    del ledger.cursors[REPO_REF]  # adopted again, as after a lost cursor: nothing is taken twice
    again = _intake(ledger, registry)
    assert (again.new_cases, again.updated_cases) == ((), ())
    assert ledger.get_case(case.id).entries == case.entries


def test_a_failed_read_of_an_adopted_issues_comments_retries_the_adoption_next_time(webinbox, ledger):
    github, registry = _looking_back(webinbox)
    _issue(github, 7, at=OLD)
    github.add_comment(REPO, 7, author="pat", body="#wait# until Friday.", created_at=OLD + timedelta(hours=1))
    read = github.read

    def no_comments(ref, **kwargs):
        if "#" in ref.id:
            raise ChannelError("gh is not logged in", kind="auth")
        return read(ref, **kwargs)

    github.read = no_comments
    (problem,) = _intake(ledger, registry).problems
    assert "gh is not logged in" in problem and REPO_REF not in ledger.cursors
    del github.read
    _intake(ledger, registry)
    (case,) = ledger.cases()
    assert [e.text for e in case.entries if e.kind == "message"] == ["The export drops the last row.", "#wait# until Friday."]
