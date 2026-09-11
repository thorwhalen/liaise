"""Tests for liaise.testing: the fakes work through correspond's own verbs, as real channels do."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import correspond
import pytest
from correspond.channels.github import LISTEN_LOOKBACK
from correspond.channels.webinbox import Site, WebInbox, _report, sign_identity
from correspond.errors import ChannelError, InvalidRef
from correspond.model import ConversationRef, Grade

from liaise.subjects import Policy, Subject, check_bindings
from liaise.testing import (
    DFLT_SITE_SECRET,
    SELF_LOGIN,
    FakeGitHubChannel,
    add_webinbox_report,
    demo_registry,
)

T0 = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
REPO = "example/app"
REPO_REF = "github:example/app"
ISSUE_REF = "github:example/app#12"
SITE = "example-site"
WEB_REF = "webinbox:example-site"


@pytest.fixture
def github() -> FakeGitHubChannel:
    fake = FakeGitHubChannel()
    fake.add_issue(
        REPO,
        12,
        author="pat",
        title="Export",
        body="The export drops the last row.",
        labels=("partner:pat",),
        created_at=T0,
    )
    return fake


def _listen(ref, registry, cursors=None):
    return list(correspond.listen(ref, cursors={} if cursors is None else cursors, registry=registry))


# ---- the fake GitHub channel, through correspond.listen ----


def test_listen_yields_the_seeded_issue_and_comment_in_the_github_adapter_shapes(github):
    github.add_comment(REPO, 12, author="Pat", body="Also on Safari.", created_at=T0 + timedelta(minutes=5))
    opening, comment = _listen(REPO_REF, demo_registry(github=github))

    assert (opening.kind, opening.channel) == ("message.created", "github")
    assert opening.delivery_id == "github:example/app:issue-12@2026-09-11T09:00:00Z"
    assert opening.message.id == "issue-12"
    assert opening.message.conversation == ConversationRef(channel="github", id="example/app#12")
    assert opening.message.native == {
        "number": 12,
        "title": "Export",
        "labels": ["partner:pat"],
        "state": "open",
    }
    assert opening.message.author.address == "github:pat"
    assert opening.message.authenticity.grade is Grade.PLATFORM

    assert comment.delivery_id == "github:example/app:issuecomment-1@2026-09-11T09:05:00Z"
    assert comment.message.id == "issuecomment-1"
    assert comment.message.conversation.encoded == ISSUE_REF
    assert comment.message.conversation.parent.encoded == REPO_REF
    assert comment.message.native == {}
    assert (comment.message.text, comment.message.author.handle, comment.message.author.is_self) == (
        "Also on Safari.",
        "Pat",
        False,
    )


def test_the_cursor_yields_only_what_was_added_after_it(github):
    registry, cursors = demo_registry(github=github), {}
    assert len(_listen(REPO_REF, registry, cursors)) == 1
    cursor = cursors[REPO_REF]
    assert isinstance(cursor, str)
    assert _listen(REPO_REF, registry, cursors) == []
    github.add_comment(REPO, 12, author="pat", body="Any news?", created_at=T0 + timedelta(hours=1))
    (event,) = _listen(REPO_REF, registry, cursors)
    assert event.message.text == "Any news?"
    assert cursors[REPO_REF] != cursor


def test_an_issue_poll_yields_only_that_issues_comments(github):
    github.add_issue(REPO, 13, author="pat", title="Chart", body="The chart is blank.", created_at=T0)
    github.add_comment(REPO, 13, author="pat", body="On 13.", created_at=T0)
    github.add_comment(REPO, 12, author="pat", body="On 12.", created_at=T0)
    assert [e.message.text for e in _listen(ISSUE_REF, demo_registry(github=github))] == ["On 12."]


def test_a_malformed_cursor_is_refused(github):
    with pytest.raises(ChannelError, match="not a github listen cursor"):
        _listen(REPO_REF, demo_registry(github=github), {REPO_REF: "2026-09-11"})


def test_parse_ref_lower_cases_and_refuses_what_is_not_a_repo_or_an_issue():
    fake = FakeGitHubChannel()
    assert fake.parse_ref("Example/App#12") == ConversationRef(channel="github", id="example/app#12")
    assert fake.parse_ref("Example/App").kind == "repository"
    with pytest.raises(InvalidRef):
        fake.parse_ref("example")


def test_read_returns_an_issue_with_its_comments(github):
    github.add_comment(REPO, 12, author="pat", body="Also on Safari.", created_at=T0 + timedelta(minutes=5))
    messages = correspond.read(ISSUE_REF, registry=demo_registry(github=github))
    assert [m.id for m in messages] == ["issue-12", "issuecomment-1"]


def test_a_first_poll_with_a_lookback_skips_what_changed_before_it():
    now = T0 + timedelta(days=3)
    fake = FakeGitHubChannel(lookback=LISTEN_LOOKBACK, clock=lambda: now)
    fake.add_issue(REPO, 12, author="pat", title="Export", body="The export drops the last row.", created_at=T0)
    fake.add_comment(REPO, 12, author="pat", body="Still broken.", created_at=now - timedelta(hours=1))
    registry, cursors = demo_registry(github=fake), {}

    assert [e.message.text for e in _listen(REPO_REF, registry, cursors)] == ["Still broken."]
    assert [m.id for m in correspond.read(REPO_REF, registry=registry)] == ["issue-12"]  # read still finds it
    fake.add_comment(REPO, 12, author="pat", body="Any news?", created_at=T0)
    assert [e.message.text for e in _listen(REPO_REF, registry, cursors)] == ["Any news?"]  # a cursor: no lookback


def test_the_channels_own_sends_are_stamped_by_its_clock():
    fake = FakeGitHubChannel(clock=lambda: T0 + timedelta(hours=2))
    fake.add_issue(REPO, 12, author="pat", title="Export", body="Broken.", created_at=T0)
    registry = demo_registry(github=fake)
    assert correspond.send(ISSUE_REF, "Fixed.", registry=registry).ok
    _, reply = correspond.read(ISSUE_REF, registry=registry)
    assert (reply.text, reply.sent_at) == ("Fixed.", T0 + timedelta(hours=2))


def test_commenting_on_an_issue_that_was_never_seeded_raises():
    with pytest.raises(ValueError, match="seed it with add_issue first"):
        FakeGitHubChannel().add_comment(REPO, 12, author="pat", body="Hello.", created_at=T0)


def test_bindings_on_the_fakes_check_clean():
    subject = Subject(
        slug="pat",
        bindings=("github:example/app?labels=partner:pat", WEB_REF),
        policy=Policy(people={"github:pat": "pat"}, roles={"pat": "partner"}),
    )
    assert check_bindings(subject, registry=demo_registry()) == []
    assert {"read", "listen", "send"} <= set(FakeGitHubChannel().capabilities.operations)
    assert FakeGitHubChannel().capabilities.grades == (Grade.PLATFORM,)


# ---- the fake GitHub channel, through correspond.send ----


def test_a_dry_run_send_plans_and_records_nothing(github):
    registry = demo_registry(github=github)
    result = correspond.send(ISSUE_REF, "Fixed, try it now.", dry_run=True, registry=registry)
    assert (result.ok, result.dry_run) == (True, True)
    assert (result.plan["action"], result.plan["body"]) == ("comment", "Fixed, try it now.")
    assert github.sent == []
    assert len(_listen(REPO_REF, registry)) == 1  # the opening, and no comment


def test_a_real_send_is_recorded_and_comes_back_as_the_channels_own(github):
    registry, cursors = demo_registry(github=github), {}
    _listen(REPO_REF, registry, cursors)
    result = correspond.send(ISSUE_REF, "Fixed, try it now.", registry=registry)
    assert (result.ok, result.dry_run, result.message_id) == (True, False, "issuecomment-1")
    assert result.account.id == SELF_LOGIN
    ((ref, draft),) = github.sent
    assert (ref.encoded, draft.text) == (ISSUE_REF, "Fixed, try it now.")
    (event,) = _listen(REPO_REF, registry, cursors)
    assert (event.message.author.handle, event.message.author.is_self) == (SELF_LOGIN, True)


def test_a_send_to_an_issue_that_does_not_exist_fails_as_on_github(github):
    result = correspond.send("github:example/app#99", "Hello.", registry=demo_registry(github=github))
    assert (result.ok, result.error_kind) == (False, "not_found")
    assert github.sent == []


def test_send_error_makes_every_real_send_fail(github):
    github.send_error = ChannelError("gh is not logged in", kind="auth")
    result = correspond.send(ISSUE_REF, "Fixed.", registry=demo_registry(github=github))
    assert (result.ok, result.error_kind) == (False, "auth")
    assert github.sent == []


# ---- the web inbox ----


def test_a_signed_web_inbox_report_is_heard_as_bound_from_its_user():
    registry = demo_registry()
    record = add_webinbox_report(
        registry["webinbox"], SITE, text="The chart is blank.", user="pat", name="Pat", received_at=T0
    )
    (event,) = _listen(WEB_REF, registry)
    assert event.delivery_id == f"webinbox:{SITE}:{record['id']}"
    assert event.message.authenticity.grade is Grade.BOUND
    assert event.message.author.address == "webinbox:pat"
    assert event.message.text == "The chart is blank."
    assert event.message.native["contact"] == {"name": "Pat", "email": None, "signed": True}


def test_an_unsigned_web_inbox_report_is_claimed():
    registry = demo_registry()
    add_webinbox_report(registry["webinbox"], SITE, text="Broken.", name="Pat", received_at=T0)
    (event,) = _listen(WEB_REF, registry)
    assert event.message.authenticity.grade is Grade.CLAIMED
    assert event.message.author.display_name == "Pat"


@pytest.mark.parametrize("signed", [True, False])
def test_a_seeded_report_is_the_record_the_collector_stores(signed):
    moment = T0.timestamp()
    payload = {"text": "The chart is blank.", "name": "Pat"}
    if signed:
        payload["identity"] = sign_identity(DFLT_SITE_SECRET, SITE, "pat", name="Pat", issued_at=moment)
    expected, _ = _report(Site(name=SITE, secret=DFLT_SITE_SECRET), payload, now=moment)
    seeded = add_webinbox_report(
        WebInbox(store={}, blobs={}),
        SITE,
        text="The chart is blank.",
        user="pat" if signed else None,
        name="Pat",
        received_at=T0,
    )
    assert {k: v for k, v in seeded.items() if k != "id"} == {k: v for k, v in expected.items() if k != "id"}
    assert seeded["id"].rsplit("-", 1)[0] == expected["id"].rsplit("-", 1)[0]
