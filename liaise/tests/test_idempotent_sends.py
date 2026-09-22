"""Tests for idempotent sends (liaise #52): a message a channel posted and then failed on is never posted twice.

Every channel is a fake (liaise.testing) and every store is under tmp_path or in memory, so
nothing leaves the process. The failure these tests are about is a channel that posts the
message and then raises (a timeout after GitHub accepted the comment): the send fails, the
message becomes a draft, and the operator's release of that draft must not post it again.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import correspond.idempotency
import cw
import pytest
from correspond.errors import ChannelError

from liaise import cases, cli
from liaise.outbox import held_id
from liaise.release import (
    DFLT_SENDS_SUBDIR,
    default_send_store,
    draft_send_key,
    gate_and_send,
    next_attempt_key,
    outbox_key,
    usable_key,
)
from liaise.subjects import RECOMMENDED_DELAY_MINUTES
from liaise.testing import demo_registry
from liaise.tests import test_messages
from liaise.tests.test_send_draft import CASE, TEXT, World, _draft
from liaise.tests.test_tick import (  # noqa: F401  (the fixtures, used by name)
    CASE_1,
    ISSUE_12,
    LATER,
    REPO,
    SLUG,
    fixed_run_suffix,
    no_real_acquaint,
    world,
)

HELD_AT = LATER
DUE = LATER + timedelta(minutes=11)
TIMEOUT = "timed out waiting for GitHub's answer"


def _fail_after_posting(github, monkeypatch, *, post=True):
    """Make ``github`` raise a network error on every real send: after posting, or (``post=False``) before."""
    original = github.send

    def send(ref, draft, *, dry_run=False):
        if dry_run:
            return original(ref, draft, dry_run=True)
        if post:
            original(ref, draft)
        raise ChannelError(TIMEOUT, kind="network")

    monkeypatch.setattr(github, "send", send)
    return lambda: monkeypatch.setattr(github, "send", original)


# ---- the keys ----


def test_a_new_attempt_key_counts_up_and_a_refused_key_moves_on_by_itself():
    assert next_attempt_key("c/h1") == "c/h1~2"
    assert next_attempt_key(next_attempt_key("c/h1")) == "c/h1~3"
    sends = {"c/h1": {"state": "failed"}, "c/h1~2": {"state": "failed"}}
    assert usable_key("c/h1", sends) == "c/h1~3"
    assert usable_key("c/h1", {"c/h1": {"state": "attempted"}}) == "c/h1"
    assert usable_key("c/h1", None) == "c/h1"


def test_a_draft_keeps_its_key_and_one_without_gets_the_same_key_every_time():
    assert draft_send_key(CASE, {**_draft(), "send_key": "k"}) == "k"
    derived = draft_send_key(CASE, _draft())
    assert derived == draft_send_key(CASE, _draft()) and derived.startswith(f"{CASE}/d")
    assert derived != draft_send_key(CASE, _draft("Another text."))


def test_the_default_store_lives_under_the_state_dir(tmp_path):
    assert default_send_store(tmp_path).folder == str(tmp_path / DFLT_SENDS_SUBDIR)


# ---- the outbox: a release that posts and then fails ----


@pytest.fixture
def public(world):
    """The tick's world on a public repository, its outbox on, a reply held in it."""
    world.subject = replace(
        world.subject, policy=replace(world.subject.policy, delay_minutes=RECOMMENDED_DELAY_MINUTES)
    )
    world.github.set_visibility(REPO, "public")
    world.issue()
    world.tick()
    world.tick(HELD_AT)
    return world


def _release_draft(world, **kwargs):
    return cases.send_draft(
        world.ledger,
        {SLUG: world.subject},
        CASE_1,
        by="operator",
        now=DUE + timedelta(minutes=5),
        registry=demo_registry(github=world.github),
        approve_shown=True,
        sends=default_send_store(world.config.state_dir),
        **kwargs,
    )


def test_a_release_that_posted_and_then_failed_is_not_posted_again_by_the_operators_release(public, monkeypatch):
    """The issue's acceptance test: a channel that posts and then raises, then send-draft, posts once."""
    (item,) = public.case().outbox
    restore = _fail_after_posting(public.github, monkeypatch)

    public.tick(DUE)

    assert len(public.github.sent) == 1  # it did go out
    (draft,) = public.case().drafts
    assert draft["send_key"] == outbox_key(CASE_1, held_id(item))
    assert draft["reason"].startswith("send failed: ") and TIMEOUT in draft["reason"]
    failed = [e for e in public.case().entries if e.kind == "gate"][-1]
    assert failed.detail["send_key"] == draft["send_key"]

    restore()
    released = _release_draft(public)

    assert released.attempt.sent and released.attempt.replayed
    assert len(public.github.sent) == 1  # found in the conversation: not posted again
    assert public.case().drafts == ()
    sent = public.case().entries[-1]
    assert sent.detail["decision"] == "send" and sent.detail["replayed"] is True
    assert sent.detail["send_key"] == outbox_key(CASE_1, held_id(item))


def test_an_attempt_that_cannot_be_confirmed_stays_a_draft_until_the_operator_asks_for_a_new_attempt(
    public, monkeypatch
):
    restore = _fail_after_posting(public.github, monkeypatch, post=False)  # its outcome unknown
    public.tick(DUE)
    restore()
    assert public.github.sent == []

    unconfirmed = _release_draft(public)

    assert not unconfirmed.attempt.sent and unconfirmed.attempt.unconfirmed
    assert public.github.sent == []  # never blindly again
    (draft,) = public.case().drafts
    assert draft["reason"].startswith("send unconfirmed: ") and "--new-attempt" in draft["reason"]
    assert ISSUE_12 in draft["reason"]
    assert public.case().entries[-1].detail["unconfirmed"] is True

    checked = _release_draft(public, new_attempt=True)

    assert checked.attempt.sent and not checked.attempt.replayed
    assert len(public.github.sent) == 1
    assert public.case().entries[-1].detail["send_key"].endswith("~2")


def test_a_release_a_crash_interrupted_becomes_a_draft_with_the_releases_key(public):
    case = public.case()
    (item,) = case.outbox
    public.ledger.save_case(replace(case, outbox=({**item, "claimed_at": DUE.isoformat()},)))

    public.tick(DUE + timedelta(minutes=5))

    (draft,) = public.case().drafts
    assert draft["send_key"] == outbox_key(CASE_1, held_id(item))


def test_a_ticks_own_send_that_failed_keeps_its_key_on_the_draft(world, monkeypatch):
    world.issue()
    world.tick()
    _fail_after_posting(world.github, monkeypatch)

    world.tick(LATER)

    assert len(world.github.sent) == 1
    (draft,) = world.case().drafts
    assert draft["send_key"].startswith(f"{CASE_1}/")
    record = default_send_store(world.config.state_dir)[draft["send_key"]]
    assert record["state"] == "attempted"  # its outcome unknown: a release reads it back


# ---- the CLI: send-draft ----


@pytest.fixture
def operator(tmp_path):
    return World(tmp_path)


def test_send_draft_after_a_post_that_failed_finds_it_and_posts_nothing(operator, monkeypatch):
    # The fake posts at its own clock (LATER): the attempt is dated by the same clock, so
    # reading back from it finds the post.
    monkeypatch.setattr(correspond.idempotency, "now", lambda: LATER)
    operator.hold_drafts(_draft())
    restore = _fail_after_posting(operator.github, monkeypatch)
    with pytest.raises(cw.CommandError, match="send failed: "):
        operator.send()
    restore()
    assert len(operator.posted()) == 1
    (kept,) = operator.case().drafts
    assert kept["send_key"] == draft_send_key(CASE, _draft())

    output = operator.send()

    assert len(operator.posted()) == 1
    assert f"  note: {cli.ALREADY_POSTED}" in output.splitlines()
    assert cli.READ_BACK_NOTE in operator.previews[-1]
    assert operator.case().drafts == ()


def test_send_draft_that_cannot_confirm_an_earlier_attempt_says_to_check_and_new_attempt_sends(
    operator, monkeypatch
):
    operator.hold_drafts(_draft())
    restore = _fail_after_posting(operator.github, monkeypatch, post=False)
    with pytest.raises(cw.CommandError):
        operator.send()
    restore()

    with pytest.raises(cw.CommandError, match="--new-attempt") as raised:
        operator.send()

    assert raised.value.code == 1 and "an earlier attempt may have gone out" in str(raised.value)
    assert operator.posted() == []
    (kept,) = operator.case().drafts
    assert kept["reason"].startswith("send unconfirmed: ")

    operator.send(new_attempt=True)

    assert operator.posted() == [f"@pat {TEXT}"] and operator.case().drafts == ()


def test_send_draft_dry_run_of_an_unconfirmed_draft_asks_nothing_and_sends_nothing(operator, monkeypatch):
    operator.hold_drafts(_draft())
    restore = _fail_after_posting(operator.github, monkeypatch, post=False)
    with pytest.raises(cw.CommandError):
        operator.send()
    restore()
    previews = len(operator.previews)

    with pytest.raises(cw.CommandError, match="would not be sent: an earlier attempt may have gone out"):
        operator.send(dry_run=True)

    assert len(operator.previews) == previews and operator.posted() == []


def test_an_edited_draft_after_a_refusal_that_posted_nothing_is_sent(operator):
    operator.hold_drafts(_draft())
    operator.github.send_error = ChannelError("issue is locked", kind="permission")
    with pytest.raises(cw.CommandError):
        operator.send()
    operator.github.send_error = None

    operator.send(edit=True, editor=lambda text: "An edited question.")

    assert operator.posted() == ["@pat An edited question."]


def test_the_send_store_is_the_state_dirs_and_nothing_is_written_to_correspond_s_own(operator, monkeypatch, tmp_path):
    operator.hold_drafts(_draft())

    operator.send()

    store = default_send_store(tmp_path / "state")
    (key,) = list(store)
    assert key == draft_send_key(CASE, _draft()) and store[key]["state"] == "sent"


def test_gate_and_send_without_a_store_sends_unkeyed(operator):
    from liaise.gate import Outbound, GateContext
    from liaise.policy import Provenance
    from liaise.subjects import load_subjects

    subject = load_subjects(operator.root)[operator.case().subject]
    outbound = Outbound(
        ref=operator.case().conversations[0], channel="github", recipient="pat", purpose="ask", text=TEXT
    )
    context = GateContext(subject=subject, now=LATER, provenance=Provenance.unknown("test"))
    attempt = gate_and_send(outbound, context, registry=operator.registry, idempotency_key="k", outbound_filters=())

    assert attempt.sent and attempt.key is None


# ---- a message outside a case ----


@pytest.fixture
def messages_world(tmp_path):
    return test_messages.World(tmp_path)


def test_a_held_message_whose_release_posted_and_failed_is_not_posted_again(messages_world, monkeypatch):
    monkeypatch.setattr(correspond.idempotency, "now", lambda: LATER)
    world = messages_world
    message_id = world.held()
    restore = _fail_after_posting(world.github, monkeypatch)
    with pytest.raises(cw.CommandError, match="send failed: "):
        world.release(message_id)
    restore()
    assert len(world.posted()) == 1
    assert world.message(message_id).state == "held"
    assert world.message(message_id).send_key == message_id

    output = world.release(message_id)

    assert len(world.posted()) == 1 and world.message(message_id).state == "sent"
    assert f"  note: {cli.ALREADY_POSTED}" in output.splitlines()


def test_a_held_message_whose_attempt_is_unconfirmed_goes_out_only_on_a_new_attempt(messages_world, monkeypatch):
    world = messages_world
    message_id = world.held()
    restore = _fail_after_posting(world.github, monkeypatch, post=False)
    with pytest.raises(cw.CommandError):
        world.release(message_id)
    restore()

    with pytest.raises(cw.CommandError, match="--new-attempt"):
        world.release(message_id)
    assert world.posted() == [] and world.message(message_id).state == "held"

    world.release(message_id, new_attempt=True)

    assert len(world.posted()) == 1 and world.message(message_id).state == "sent"
    assert world.message(message_id).send_key == f"{message_id}~2"
