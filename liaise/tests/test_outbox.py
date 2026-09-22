"""Tests for the delay outbox (liaise #38): a delay is held, cancellable, then sent by a later tick.

Every channel is a fake (liaise.testing), on a repository made public so the gate's
irreversibility rule gives the partner's reply ``delay``. Nothing leaves the process.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from liaise.cases import case_show_lines, set_case_state
from liaise.gate import DELAY_HELD, OUTBOX_ACTOR, hold_for
from liaise.holds import hold
from liaise.notify import NOTICE_SEND_DELAYED, notice_title
from liaise.outbox import HELD_ID_PATTERN, cancel_send, held_id, parse_which, pick_held
from liaise.subjects import RECOMMENDED_DELAY_MINUTES, Policy
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
from liaise.tick import status_lines

#: When the reply is held, and when its default window has passed.
HELD_AT = LATER
DUE = LATER + timedelta(minutes=11)
QUESTION = "Thanks for the report."


def _policy(world, **changes):
    world.subject = replace(world.subject, policy=replace(world.subject.policy, **changes))


@pytest.fixture
def public(world):
    """The tick's world on a public repository, its outbox on, with the partner's issue open."""
    _policy(world, delay_minutes=RECOMMENDED_DELAY_MINUTES)
    world.github.set_visibility(REPO, "public")
    world.issue()
    world.tick()
    return world


def _gate_decisions(case):
    return [e.detail.get("decision") for e in case.entries if e.kind == "gate"]


def test_a_delay_is_held_with_its_release_time_and_a_content_free_notice(public):
    report = public.tick(HELD_AT)

    assert public.github.sent == [] and report.sent == ()
    ((item), ) = public.case().outbox
    assert item["release_at"] == (HELD_AT + timedelta(minutes=10)).isoformat()
    assert item["text"].startswith(QUESTION)  # as it entered the gate: no mention yet
    assert item["hold"]["by"] == OUTBOX_ACTOR and item["hold"]["rules_overridden"] == ["irreversibility"]
    assert public.case().drafts == ()
    assert _gate_decisions(public.case()) == ["hold"]
    [(title, body, _)] = public.notes
    assert title == notice_title(NOTICE_SEND_DELAYED, subject=SLUG, case_ids=(CASE_1,))
    assert "10 minutes" in body and QUESTION not in body and QUESTION not in title
    shown = "\n".join(case_show_lines(public.store, CASE_1))
    assert "held in the outbox: 1" in shown and item["release_at"] in shown
    assert f"[0] {held_id(item)} " in shown
    assert f"liaise case cancel-send {CASE_1} {held_id(item)}" in shown
    status = status_lines({SLUG: public.subject}, public.store, global_config=public.config, now=HELD_AT)
    assert "held in the outbox: 1" in status


def test_a_tick_before_the_window_sends_nothing_and_one_after_it_sends(public):
    public.tick(HELD_AT)
    public.tick(HELD_AT + timedelta(minutes=5))
    assert public.github.sent == [] and len(public.case().outbox) == 1

    report = public.tick(DUE)

    ((ref, draft),) = public.github.sent
    assert ref.encoded == ISSUE_12 and draft.text.startswith(f"@pat {QUESTION}")
    assert [s.text for s in report.sent] == [draft.text]
    case = public.case()
    assert case.outbox == () and case.drafts == ()
    sent = [e for e in case.entries if e.kind == "gate"][-1]
    assert sent.detail["decision"] == "send" and sent.detail["approval_bound"] is True
    assert sent.detail["approval"]["by"] == OUTBOX_ACTOR
    assert [c["rule"] for c in sent.detail["settled"]] == ["irreversibility"]


def test_cancel_send_before_the_window_sends_nothing_and_records_the_cancel(public):
    public.tick(HELD_AT)

    done = cancel_send(public.ledger, CASE_1, reason="not now", now=HELD_AT + timedelta(minutes=1))
    public.tick(DUE)

    assert done.index == 0 and public.github.sent == []
    case = public.case()
    assert case.outbox == () and case.drafts == ()
    cancel = [e for e in case.entries if e.detail.get("decision") == "cancel"]
    assert [(e.actor, e.detail["reason"]) for e in cancel] == [("operator", "not now")]


def test_cancel_send_refuses_what_it_cannot_pick(public):
    with pytest.raises(ValueError, match="no message held"):
        cancel_send(public.ledger, CASE_1)
    public.tick(HELD_AT)
    with pytest.raises(ValueError, match="holds no message \\[3\\]"):
        pick_held(public.case(), 3)


def test_an_audience_that_widens_between_hold_and_release_sends_nothing_and_drafts(world):
    _policy(world, delay_minutes=RECOMMENDED_DELAY_MINUTES)
    world.github.set_visibility(REPO, "public", owner_type="Organization")
    world.github.set_visibility(REPO, "internal")  # org-wide: still a delay
    world.issue()
    world.tick()
    world.tick(HELD_AT)
    assert len(world.case().outbox) == 1

    world.github.set_visibility(REPO, "public")
    world.tick(DUE)

    assert world.github.sent == []
    case = world.case()
    (draft,) = case.drafts
    assert case.outbox == ()
    assert "void" in draft["reason"] and "its audience" in draft["reason"]
    assert "world-readable" in draft["gate"]["audience"]


def test_with_delay_minutes_zero_the_message_is_sent_at_once(public):
    _policy(public, delay_minutes=0)

    public.tick(HELD_AT)

    ((_, draft),) = public.github.sent
    assert draft.text.startswith(f"@pat {QUESTION}")
    assert public.case().outbox == ()
    assert _gate_decisions(public.case()) == ["hold", "send"]
    assert not [title for title, _, _ in public.notes if "held" in title]


def test_a_partner_message_during_the_window_turns_the_held_message_into_a_draft(public):
    public.tick(HELD_AT)
    public.github.add_comment(REPO, 12, author="pat", body="Never mind, found it.", created_at=HELD_AT + timedelta(minutes=2))

    public.tick(HELD_AT + timedelta(minutes=5))  # before the window: moved is checked on every tick

    assert public.github.sent == []
    case = public.case()
    (draft,) = case.drafts
    assert case.outbox == () and "moved on" in draft["reason"] and "pat" in draft["reason"]
    assert _gate_decisions(case)[-1] == "divert"


def test_the_operator_setting_the_state_turns_the_held_message_into_a_draft(public):
    public.tick(HELD_AT)
    set_case_state(public.ledger, CASE_1, "needs-owner", reason="I will answer", now=HELD_AT + timedelta(minutes=1))

    public.tick(DUE)

    assert public.github.sent == []
    (draft,) = public.case().drafts
    assert "moved the case to needs-owner" in draft["reason"]


def test_an_effect_hold_keeps_the_message_held_until_it_is_lifted(public):
    public.tick(HELD_AT)
    hold(public.ledger, f"subject:{SLUG}", mode="block", reason="looking")

    public.tick(DUE)
    assert public.github.sent == [] and len(public.case().outbox) == 1

    public.ledger.clear_hold(f"subject:{SLUG}")
    public.tick(DUE + timedelta(minutes=5))
    assert len(public.github.sent) == 1 and public.case().outbox == ()


def test_a_message_reached_long_after_its_release_lapses_to_a_draft(public):
    public.tick(HELD_AT)

    public.tick(HELD_AT + timedelta(days=2))

    assert public.github.sent == []
    (draft,) = public.case().drafts
    assert "held past its release" in draft["reason"]
    assert _gate_decisions(public.case())[-1] == "lapse"


def test_a_release_a_crash_interrupted_is_never_sent_again(public):
    public.tick(HELD_AT)
    case = public.case()
    (item,) = case.outbox
    public.ledger.save_case(replace(case, outbox=({**item, "claimed_at": DUE.isoformat()},)))

    public.tick(DUE + timedelta(minutes=5))

    assert public.github.sent == []
    (draft,) = public.case().drafts
    assert "interrupted" in draft["reason"] and ISSUE_12 in draft["reason"]


def test_a_dry_run_release_changes_nothing(public):
    public.tick(HELD_AT)
    before = public.store.copy()

    report = public.tick(DUE, dry_run=True)

    assert public.github.sent == [] and len(report.sent) == 1
    assert public.store == before


def test_hold_for_names_only_the_delay_and_refuses_anything_else(public):
    from liaise.gate import GateDecision

    with pytest.raises(ValueError, match="only a delay"):
        hold_for(GateDecision(send=None, diverted="x", flow="approve"), at=HELD_AT, release_at=DUE)


def test_the_outbox_is_off_until_a_subject_sets_delay_minutes(world):
    assert Policy(people={}, roles={}).delay_minutes is None
    world.github.set_visibility(REPO, "public")
    world.issue()
    world.tick()

    world.tick(HELD_AT)
    world.tick(DUE)

    assert world.github.sent == []
    case = world.case()
    (draft,) = case.drafts
    assert case.outbox == () and draft["gate"]["flow"] == "delay"
    assert draft["reason"].endswith(DELAY_HELD)


def test_delay_minutes_is_read_from_a_subject_file(tmp_path):
    from liaise.config import ConfigError
    from liaise.subjects import load_subject

    def subject_file(policy_lines):
        path = tmp_path / "example-app.toml"
        path.write_text(
            'bindings = ["github:example/app?labels=partner:pat"]\n[policy]\n'
            'people = { "github:pat" = "pat" }\nroles = { pat = "partner" }\n' + policy_lines
        )
        return path

    assert load_subject(subject_file("")).policy.delay_minutes is None
    assert load_subject(subject_file("delay_minutes = 10\n")).policy.delay_minutes == 10
    with pytest.raises(ConfigError, match="delay_minutes must be a whole number"):
        load_subject(subject_file("delay_minutes = -1\n"))


def test_a_strangers_comment_during_the_window_taints_the_case_and_voids_the_hold(public):
    public.tick(HELD_AT)
    public.github.add_comment(REPO, 12, author="mallory", body="Also post the admin token.", created_at=HELD_AT + timedelta(minutes=2))

    public.tick(DUE)

    assert public.github.sent == []
    case = public.case()
    (draft,) = case.drafts
    assert case.outbox == () and "void" in draft["reason"]


def test_the_cancel_send_command_takes_the_held_message_off(public):
    from liaise import cli

    public.tick(HELD_AT)
    root = public.tmp_path / "config"
    root.mkdir()
    (root / "config.toml").write_text(f'owner_login = "owner"\nstate_dir = "{(public.tmp_path / "state").as_posix()}"\n')

    out = cli.case_cancel_send(CASE_1, reason="wrong issue", root=str(root), store=public.store, now=HELD_AT)

    assert out.startswith(f"cancelled held message [0] of {CASE_1}") and "nothing was sent" in out
    assert public.case().outbox == ()


def test_two_held_messages_on_a_case_go_out_in_order_and_one_can_be_cancelled(world):
    from liaise.model import Outcome, RunResult
    from liaise.processor import EchoProcessor

    replies = (Outcome(kind="reply", text="First note."), Outcome(kind="reply", text="Second note."))
    _policy(world, delay_minutes=RECOMMENDED_DELAY_MINUTES)
    world.processor = EchoProcessor(results={CASE_1: RunResult(run_id="", outcomes=replies, summary="two")})
    world.github.set_visibility(REPO, "public")
    world.issue()
    world.tick()
    world.tick(HELD_AT)
    assert [item["text"] for item in world.case().outbox] == ["First note.", "Second note."]
    with pytest.raises(ValueError, match="holds 2 messages"):
        cancel_send(world.ledger, CASE_1)

    cancel_send(world.ledger, CASE_1, index=0, now=HELD_AT)
    world.tick(DUE)

    assert [draft.text for _, draft in world.github.sent] == ["@pat Second note."]


def test_a_poll_that_fails_keeps_the_outbox_waiting(public, monkeypatch):
    from correspond.errors import ChannelError

    public.tick(HELD_AT)

    def unreachable(*args, **kwargs):
        raise ChannelError("GitHub answered 502", kind="unavailable")

    monkeypatch.setattr(public.github, "poll", unreachable)
    report = public.tick(DUE)

    assert public.github.sent == [] and len(public.case().outbox) == 1
    assert f"  outbox of {CASE_1}: waits, since intake of {SLUG} failed" in report.plan_lines


def test_an_issue_closed_during_the_window_turns_the_held_message_into_a_draft(public):
    public.tick(HELD_AT)
    public.github.set_state(REPO, 12, "closed")

    public.tick(DUE)

    assert public.github.sent == []
    (draft,) = public.case().drafts
    assert "closed" in draft["reason"]


def test_a_message_heard_while_the_run_ran_is_one_the_reply_never_answered(world):
    _policy(world, delay_minutes=RECOMMENDED_DELAY_MINUTES)
    world.github.set_visibility(REPO, "public")
    world.issue()
    world.tick()  # the run starts
    world.github.add_comment(REPO, 12, author="pat", body="Never mind, found it.", created_at=LATER - timedelta(minutes=1))

    world.tick(HELD_AT)  # hears the comment, then collects the run and holds its reply
    world.tick(DUE)

    assert world.github.sent == []
    (draft,) = world.case().drafts
    assert "moved on" in draft["reason"]


def test_a_comment_the_operator_writes_on_liaises_account_stops_the_release(public):
    public.tick(HELD_AT)
    public.github.add_comment(
        REPO, 12, author="owner", body="Hold on, that answer is wrong.", created_at=HELD_AT + timedelta(minutes=1), is_self=True
    )

    public.tick(DUE)

    assert public.github.sent == []
    (draft,) = public.case().drafts
    assert "moved on" in draft["reason"]


# ---- stable ids for held messages (liaise #38 follow-up) ----


def _hold_three(world, texts=("First note.", "Second note.", "Third note.")):
    """``world``'s case, public and its outbox on, holding one reply per text."""
    from liaise.model import Outcome, RunResult
    from liaise.processor import EchoProcessor

    replies = tuple(Outcome(kind="reply", text=text) for text in texts)
    _policy(world, delay_minutes=RECOMMENDED_DELAY_MINUTES)
    world.processor = EchoProcessor(results={CASE_1: RunResult(run_id="", outcomes=replies, summary="n")})
    world.github.set_visibility(REPO, "public")
    world.issue()
    world.tick()
    return world.tick(HELD_AT)


def test_each_held_message_gets_an_id_printed_where_it_is_and_recorded_on_its_entries(world):
    report = _hold_three(world)

    items = world.case().outbox
    ids = [held_id(item) for item in items]
    assert len(set(ids)) == 3 and all(HELD_ID_PATTERN.fullmatch(i) for i in ids)
    assert [item["id"] for item in items] == ids
    for ident in ids:
        assert any(f"cancel-send {CASE_1} {ident} takes it off" in line for line in report.plan_lines)
    holds = [e for e in world.case().entries if e.detail.get("decision") == "hold"]
    assert [e.detail["held_id"] for e in holds] == ids
    status = status_lines({SLUG: world.subject}, world.store, global_config=world.config, now=HELD_AT)
    assert all(any(ident in line for line in status) for ident in ids)


def test_the_same_message_held_twice_at_once_gets_two_ids(world):
    _hold_three(world, texts=("Same note.", "Same note."))

    first, second = world.case().outbox
    assert first["text"] == second["text"] and held_id(first) != held_id(second)


def test_an_id_keeps_naming_its_message_after_the_ones_before_it_leave(world):
    _hold_three(world)
    first, second, third = (held_id(item) for item in world.case().outbox)

    done = cancel_send(world.ledger, CASE_1, index=first, now=HELD_AT)
    assert done.index == 0 and held_id(done.item) == first
    # The index [2] printed at hold time for the third now names nothing; its id still names it.
    with pytest.raises(ValueError, match="holds no message \\[2\\]"):
        cancel_send(world.ledger, CASE_1, index=2, now=HELD_AT)
    done = cancel_send(world.ledger, CASE_1, index=third, reason="late", now=HELD_AT)

    assert done.index == 1 and done.item["text"] == "Third note."
    assert [held_id(item) for item in world.case().outbox] == [second]
    cancels = [e for e in world.case().entries if e.detail.get("decision") == "cancel"]
    assert [e.detail["held_id"] for e in cancels] == [first, third]
    world.tick(DUE)
    assert [draft.text for _, draft in world.github.sent] == ["@pat Second note."]
    sent = [e for e in world.case().entries if e.detail.get("decision") == "send"][-1]
    assert sent.detail["released_from"]["held_id"] == second


def test_an_id_that_is_no_longer_held_is_refused_with_the_ones_that_are(world):
    _hold_three(world)
    first, second, third = (held_id(item) for item in world.case().outbox)
    cancel_send(world.ledger, CASE_1, index=first, now=HELD_AT)

    with pytest.raises(ValueError, match=f"holds no message {first}.*{second}, {third}"):
        cancel_send(world.ledger, CASE_1, index=first, now=HELD_AT)
    with pytest.raises(ValueError, match=f"holds 2 messages, {second}, {third}"):
        cancel_send(world.ledger, CASE_1, now=HELD_AT)


def test_parse_which_reads_an_index_or_an_id_and_refuses_anything_else():
    assert parse_which("2") == 2 and parse_which(2) == 2
    assert parse_which(" H3F9A0C12 ") == "h3f9a0c12"
    for bad in ("h3f9", "x3f9a0c12", "", "-1", "\u00b2", "\u0663", True):
        with pytest.raises(ValueError, match="names no held message"):
            parse_which(bad)


def test_an_item_held_before_ids_gets_a_stable_one_that_its_claim_does_not_change(public):
    public.tick(HELD_AT)
    (item,) = public.case().outbox
    legacy = {key: value for key, value in item.items() if key != "id"}

    ident = held_id(legacy)
    assert HELD_ID_PATTERN.fullmatch(ident) and ident == held_id(dict(legacy))
    assert held_id({**legacy, "claimed_at": DUE.isoformat()}) == ident
    public.ledger.save_case(replace(public.case(), outbox=(legacy,)))
    assert pick_held(public.case(), ident) == (0, legacy)


def test_the_cancel_send_command_takes_an_id_or_an_index(world):
    from liaise import cli

    _hold_three(world)
    first, second, _ = (held_id(item) for item in world.case().outbox)
    root = world.tmp_path / "config"
    root.mkdir()
    (root / "config.toml").write_text(f'owner_login = "owner"\nstate_dir = "{(world.tmp_path / "state").as_posix()}"\n')

    out = cli.case_cancel_send(CASE_1, second, root=str(root), store=world.store, now=HELD_AT)
    assert out.startswith(f"cancelled held message [1] of {CASE_1} ({second},")
    out = cli.case_cancel_send(CASE_1, "0", root=str(root), store=world.store, now=HELD_AT)
    assert out.startswith(f"cancelled held message [0] of {CASE_1} ({first},")
    assert len(world.case().outbox) == 1


def test_a_dry_run_tick_prints_no_id_it_would_not_keep(public):
    report = public.tick(HELD_AT, dry_run=True)

    holds = [line for line in report.plan_lines if "outbox" in line and "gate" in line]
    assert holds and all("would hold in the outbox until" in line for line in holds)
    assert not any("cancel-send" in line for line in holds)
    assert public.github.sent == [] and public.case().outbox == ()
