"""Tests for liaise.gate: the outbound filters, their order, and run_gate.

The reply-mode and leak-scan tests are the mutation check the spec asks for. Each runs
through ``run_gate`` with the default filters, so taking ``reply_mode`` or ``leak_scan``
out of ``DFLT_OUTBOUND_FILTERS`` fails them. So does weakening a check: ignoring the
per-person override, dropping a pattern or the whole-word rule, or scanning channels
that are not public.

No test here reads the operator's real people records: an autouse fixture makes
``acquaint`` unimportable, and the tests that need it install a fake. Leak samples are
built by concatenation, so the no-personal-data guard does not flag this file.
"""

from __future__ import annotations

import sys
import types
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from liaise.gate import (
    DFLT_OUTBOUND_FILTERS,
    Divert,
    GateContext,
    GateDecision,
    Outbound,
    Pass,
    deslop,
    leak_scan,
    notify_recipient,
    reply_mode,
    run_gate,
    writing_card,
)
from liaise.model import Case
from liaise.subjects import Policy, Subject

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
REF = "github:example/app#12"
TEXT = "Fixed: the button on the first page works again."


@pytest.fixture(autouse=True)
def no_real_acquaint(monkeypatch):
    monkeypatch.setitem(sys.modules, "acquaint", None)


class AcquaintError(Exception):
    """Stands in for acquaint's own error, raised for a person it does not know."""


def _subject(**policy) -> Subject:
    fields = dict(people={"github:pat": "pat"}, roles={"pat": "partner"}, default_reply_mode="direct")
    fields.update(policy)
    return Subject(slug="pat", bindings=("github:example/app?labels=partner:pat",), policy=Policy(**fields))


def _context(subject=None) -> GateContext:
    case = Case(
        id="pat-1",
        subject="pat",
        conversations=(REF,),
        reporter="pat",
        state="working",
        created_at=NOW,
        updated_at=NOW,
    )
    return GateContext(subject=subject or _subject(), case=case, now=NOW)


def _outbound(text=TEXT, **fields) -> Outbound:
    values = dict(case_id="pat-1", ref=REF, channel="github", recipient="pat", purpose="reply", text=text)
    values.update(fields)
    return Outbound(**values)


def _gate(text=TEXT, *, subject=None, **fields) -> GateDecision:
    """Run the gate with its default filters, as the tick does."""
    return run_gate(_outbound(text, **fields), _context(subject))


def _install_acquaint(monkeypatch, **functions) -> None:
    module = types.ModuleType("acquaint")
    for name, function in functions.items():
        setattr(module, name, function)
    monkeypatch.setitem(sys.modules, "acquaint", module)


def _raising(error):
    def function(*args, **kwargs):
        raise error

    return function


def _clean_lint(text, *, recipient=None):
    return {"ok": True, "findings": [], "summary": "0 finding(s) to fix"}


# ---- reply mode (mutation-checked) ----


def test_draft_reply_mode_diverts():
    decision = _gate(subject=_subject(default_reply_mode="draft"))
    assert (decision.send, decision.diverted) == (None, "draft reply mode")


def test_direct_reply_mode_sends():
    decision = _gate()
    assert decision.diverted is None
    assert decision.send == _outbound("@pat " + TEXT)


def test_a_person_override_to_direct_sends_under_a_draft_default():
    decision = _gate(subject=_subject(default_reply_mode="draft", reply_modes={"pat": "direct"}))
    assert decision.diverted is None
    assert decision.send is not None


def test_a_person_override_to_draft_diverts_under_a_direct_default():
    decision = _gate(subject=_subject(reply_modes={"pat": "draft"}))
    assert (decision.send, decision.diverted) == (None, "draft reply mode")


def test_another_persons_override_does_not_apply():
    subject = _subject(default_reply_mode="draft", reply_modes={"someone-else": "direct"})
    assert _gate(subject=subject).diverted == "draft reply mode"


# ---- leak scan (mutation-checked) ----

LEAK_TERMS = ("example-internal",)
LEAKS = {
    "macos-home-path": ("local path", "The log is at " + "/Us" + "ers/someone/app/log.txt"),
    "linux-home-path": ("local path", "I looked in " + "/ho" + "me/someone/app/src"),
    "root-home-path": ("local path", "It lives in " + "/ro" + "ot/app/config"),
    "windows-home-path": ("local path", "It lives in " + "C:" + "\\Users\\someone\\app"),
    "email": ("email", "Write to " + "someone" + "@" + "example.com" + " for access."),
    "classic-github-token": ("token", "Use " + "ghp_" + "a" * 36),
    "fine-grained-github-token": ("token", "Use " + "github_pat_" + "a" * 40),
    "sk-key": ("token", "Use " + "sk-" + "a" * 24),
    "aws-key": ("token", "Use " + "AKIA" + "A" * 16),
    "leak-term": ("leak term", "This is on the Example-Internal board."),
}


@pytest.mark.parametrize("kind, text", list(LEAKS.values()), ids=list(LEAKS))
def test_leak_scan_diverts_on_a_public_channel(kind, text):
    decision = _gate(text, subject=_subject(leak_terms=LEAK_TERMS))
    assert (decision.send, decision.diverted) == (None, f"leak scan: {kind}")


@pytest.mark.parametrize("kind, text", list(LEAKS.values()), ids=list(LEAKS))
def test_leak_scan_passes_the_same_text_on_a_channel_that_is_not_public(kind, text):
    decision = _gate(text, subject=_subject(leak_terms=LEAK_TERMS, public_channels=()))
    assert decision.diverted is None
    assert decision.send.text == "@pat " + text


def test_leak_scan_never_redacts_and_never_repeats_the_secret():
    token = "ghp_" + "b" * 36
    decision = _gate("Use " + token)
    assert decision.send is None
    assert decision.notes == ("leak scan: token at character 4",)
    assert all(token not in note for note in decision.notes)


def test_leak_scan_names_every_kind_it_found():
    text = "Mail " + "someone" + "@" + "example.com" + " the file in " + "/ho" + "me/someone/app/x"
    decision = _gate(text)
    assert decision.diverted == "leak scan: local path, email"
    assert len(decision.notes) == 2


@pytest.mark.parametrize("text", ["The example-internals tab.", "The xexample-internal tab."])
def test_leak_terms_match_whole_words_only(text):
    assert _gate(text, subject=_subject(leak_terms=LEAK_TERMS)).diverted is None


@pytest.mark.parametrize(
    "text",
    [
        "Thanks @pat, see https://example.com/ho" + "me/page/x",
        "The task-" + "a" * 24 + " step passed.",
        "The sk-" + "a" * 5 + " prefix is too short to be a key.",
    ],
)
def test_leak_scan_ignores_lookalikes(text):
    assert _gate(text).diverted is None


# ---- writing card and deslop ----


def test_without_acquaint_the_writing_card_and_deslop_degrade_to_notes():
    decision = _gate()
    assert decision.send is not None
    card_note, deslop_note, _mention_note = decision.notes
    assert card_note.startswith("writing card unavailable: acquaint could not be imported")
    assert deslop_note.startswith("deslop unavailable: acquaint could not be imported")


def test_when_acquaint_raises_the_writing_card_and_deslop_degrade_to_notes(monkeypatch):
    error = AcquaintError("no entity matches 'pat'")
    _install_acquaint(monkeypatch, brief=_raising(error), style_lint=_raising(error))
    decision = _gate()
    assert decision.send is not None
    assert "writing card unavailable: AcquaintError: no entity matches 'pat'" in decision.notes
    assert "deslop unavailable: AcquaintError: no entity matches 'pat'" in decision.notes


def test_writing_card_notes_the_card_summary(monkeypatch):
    calls = []

    def brief(person, *, purpose=None):
        calls.append((person, purpose))
        return {"ok": True, "name": "Pat", "summary": "brief for Pat (reply)"}

    _install_acquaint(monkeypatch, brief=brief, style_lint=_clean_lint)
    decision = _gate()
    assert calls == [("pat", "reply")]
    assert decision.send is not None
    assert decision.notes == ("writing card: brief for Pat (reply)", "added the mention @pat")


def test_deslop_diverts_on_enforced_findings(monkeypatch):
    findings = [
        {"rule": "em-dash-density", "tier": "B", "message": "too many em dashes", "enforced": True, "excerpt": ""},
        {"rule": "recipient-blocklist", "tier": "E", "message": "never use 'synergy'", "enforced": True, "excerpt": "the synergy"},
        {"rule": "hedging", "tier": "C", "message": "hedges", "enforced": False, "excerpt": ""},
    ]
    calls = []

    def style_lint(text, *, recipient=None):
        calls.append((text, recipient))
        return {"ok": False, "findings": findings, "summary": "2 finding(s) to fix"}

    _install_acquaint(monkeypatch, brief=_raising(AcquaintError("unknown")), style_lint=style_lint)
    decision = _gate()
    assert calls == [(TEXT, "pat")]
    assert (decision.send, decision.diverted) == (None, "deslop: 2 enforced finding(s)")
    assert "deslop: B em-dash-density: too many em dashes" in decision.notes
    assert "deslop: E recipient-blocklist: never use 'synergy' (…the synergy…)" in decision.notes
    assert not any("hedging" in note for note in decision.notes)


def test_deslop_passes_a_clean_message(monkeypatch):
    _install_acquaint(monkeypatch, brief=_raising(AcquaintError("unknown")), style_lint=_clean_lint)
    decision = _gate()
    assert decision.send is not None
    assert not any(note.startswith("deslop") for note in decision.notes)


def test_the_acquaint_filters_never_raise_on_a_malformed_answer(monkeypatch):
    _install_acquaint(monkeypatch, brief=lambda person, *, purpose=None: None, style_lint=lambda text, *, recipient=None: [])
    context, outbound = _context(), _outbound()
    assert writing_card(outbound, context).notes[0].startswith("writing card unavailable: AttributeError")
    assert deslop(outbound, context).notes[0].startswith("deslop unavailable: TypeError")


# ---- notify recipient ----


def test_notify_recipient_adds_the_mention_on_github():
    decision = _gate("Fixed.")
    assert decision.send.text == "@pat Fixed."
    assert "added the mention @pat" in decision.notes


@pytest.mark.parametrize("text", ["@pat Fixed.", "@Pat, fixed.", "  @pat\nFixed."])
def test_notify_recipient_keeps_a_mention_already_there(text):
    assert _gate(text).send.text == text


def test_a_longer_login_is_not_the_recipients_mention():
    assert _gate("@pat-bot fixed it.").send.text == "@pat @pat-bot fixed it."


def test_the_notify_address_wins_over_the_handle():
    subject = _subject(notify={"pat": "github:pat-reports"})
    assert _gate("Fixed.", subject=subject).send.text == "@pat-reports Fixed."


@pytest.mark.parametrize(
    "policy",
    [
        dict(people={}),
        dict(people={"telegram:@pat": "pat"}),
        dict(people={}, notify={"pat": "ntfy:pat-updates"}),
    ],
)
def test_notify_recipient_diverts_without_a_github_address(policy):
    decision = _gate(subject=_subject(**policy))
    assert (decision.send, decision.diverted) == (None, "no handle to notify pat")


def test_other_channels_pass_without_a_mention():
    fields = dict(channel="ntfy", ref="ntfy:pat-updates")
    decision = _gate("Fixed.", subject=_subject(people={}), **fields)
    assert decision.send == _outbound("Fixed.", **fields)


def test_a_notify_address_that_is_no_login_falls_back_to_the_next_github_address():
    """The gate asks Subject.notify_addresses_for (tested in test_subjects) for every GitHub
    address, best first, and mentions the first valid login among them."""
    subject = _subject(notify={"pat": "github:not a login"})
    decision = _gate("Fixed.", subject=subject)
    assert decision.send.text == "@pat Fixed."


# ---- order and run_gate ----


def test_default_filters_are_in_the_documented_order():
    assert DFLT_OUTBOUND_FILTERS == (reply_mode, leak_scan, writing_card, deslop, notify_recipient)


def test_reply_mode_diverts_a_draft_with_a_leak_before_the_leak_scan_sees_it():
    text = "Write to " + "someone" + "@" + "example.com"
    decision = _gate(text, subject=_subject(default_reply_mode="draft"))
    assert decision.diverted == "draft reply mode"
    assert decision.notes == ()


def test_run_gate_stops_at_the_first_divert_and_keeps_the_notes_so_far():
    ran = []

    def first(outbound, ctx):
        ran.append("first")
        return Pass(replace(outbound, text="rewritten"), notes=("first note",))

    def second(outbound, ctx):
        ran.append(("second", outbound.text))
        return Divert("second says no", notes=("second note",))

    def third(outbound, ctx):
        ran.append("third")
        return Pass(outbound)

    decision = run_gate(_outbound(), _context(), outbound_filters=(first, second, third))
    assert ran == ["first", ("second", "rewritten")]
    assert decision == GateDecision(send=None, diverted="second says no", notes=("first note", "second note"))


def test_run_gate_sends_the_message_as_the_filters_left_it():
    def rewrite(outbound, ctx):
        return Pass(replace(outbound, text="rewritten"), notes=("rewrote",))

    decision = run_gate(_outbound(), _context(), outbound_filters=(rewrite,))
    assert decision == GateDecision(send=_outbound("rewritten"), diverted=None, notes=("rewrote",))


def test_run_gate_fails_closed_when_a_filter_raises_or_answers_nonsense():
    def broken(outbound, ctx):
        raise RuntimeError("boom")

    def confused(outbound, ctx):
        return True

    assert run_gate(_outbound(), _context(), outbound_filters=(broken,)).diverted == "broken failed: RuntimeError: boom"
    decision = run_gate(_outbound(), _context(), outbound_filters=(confused,))
    assert (decision.send, decision.diverted) == (None, "confused returned bool, not Pass or Divert")


def test_run_gate_without_filters_sends_the_message_unchanged():
    assert run_gate(_outbound(), _context(), outbound_filters=()) == GateDecision(send=_outbound(), diverted=None)
