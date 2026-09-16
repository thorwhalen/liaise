"""Tests for liaise.gate: the filters, their order, and the decision every filter reaches together.

The policy tests run through ``run_gate`` with the default filters, so taking
``outbound_policy`` out of ``DFLT_OUTBOUND_FILTERS`` fails them (one test checks that it
does). The approval tests are the binding of liaise ADR 0002: an approval releases a
message past what it names, only while the message and its audience are the ones it was
given for, and never past a refusal.

Each context's audience is what liaise.testing's fake GitHub channel says: a private
repository its owner (a user) owns, unless a test makes it public. The run's provenance is
clean unless a test says otherwise. No test here reads the operator's real people records:
an autouse fixture makes ``acquaint`` unimportable, and the tests that need it install a
fake. Leak samples are built by concatenation, so the no-personal-data guard does not flag
this file.
"""

from __future__ import annotations

import functools
import sys
import time
import types
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from liaise.detect import LABELS
from liaise.gate import (
    CASELESS_REASON,
    DELAY_HELD,
    DFLT_OUTBOUND_FILTERS,
    OUTSIDE_A_CASE,
    Divert,
    GateContext,
    GateDecision,
    Outbound,
    Pass,
    approval_for,
    deslop,
    notify_recipient,
    outbound_policy,
    outside_a_case,
    run_gate,
    writing_card,
)
from liaise.model import Approval, Case
from liaise.outbound import need_to_know_disclosure
from liaise.policy import Provenance, payload_hash
from liaise.release import audience_of
from liaise.subjects import Policy, Subject
from liaise.testing import FakeGitHubChannel, demo_registry

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
REF = "github:example/app#12"
TEXT = "Fixed: the button on the first page works again."
KEY = b"k" * 32
TOKEN = "ghp_" + "b" * 36
CLEAN = Provenance.clean("every message on the case is from someone trusted")
DRAFT_REASON = "draft reply mode for pat: the operator releases every message"
CASE = Case(
    id="pat-1",
    subject="pat",
    conversations=(REF,),
    reporter="pat",
    state="working",
    created_at=NOW,
    updated_at=NOW,
)


@pytest.fixture(autouse=True)
def no_real_acquaint(monkeypatch):
    monkeypatch.setitem(sys.modules, "acquaint", None)


class AcquaintError(Exception):
    """Stands in for acquaint's own error, raised for a person it does not know."""


def _subject(**policy) -> Subject:
    fields = dict(people={"github:pat": "pat"}, roles={"pat": "partner"}, default_reply_mode="direct")
    fields.update(policy)
    return Subject(slug="pat", bindings=("github:example/app?labels=partner:pat",), policy=Policy(**fields))


def _audience(visibility="private"):
    """Who reads ``example/app`` when it has ``visibility``, as the fake GitHub channel answers."""
    probe = Outbound(ref=REF, channel="github", recipient="pat", purpose="reply", text="")
    return audience_of(probe, registry=demo_registry(github=FakeGitHubChannel(visibility=visibility)))


def _context(subject=None, *, visibility="private", provenance=CLEAN, case=True, approval=None) -> GateContext:
    return GateContext(
        subject=subject or _subject(),
        case=CASE if case else None,
        now=NOW,
        audience=_audience(visibility),
        provenance=provenance,
        approval=approval,
        fingerprint_key=KEY,
    )


def _outbound(text=TEXT, **fields) -> Outbound:
    values = dict(case_id="pat-1", ref=REF, channel="github", recipient="pat", purpose="reply", text=text)
    values.update(fields)
    return Outbound(**values)


def _gate(text=TEXT, *, subject=None, visibility="private", provenance=CLEAN, case=True, **fields) -> GateDecision:
    """Run the gate with its default filters, as the tick does."""
    context = _context(subject, visibility=visibility, provenance=provenance, case=case)
    return run_gate(_outbound(text, **fields), context)


def _rules(decision: GateDecision) -> list:
    return [concern.rule for concern in decision.concerns]


def _approved(decision: GateDecision, **changes) -> Approval:
    return approval_for(decision, by="operator", at=NOW, **changes)


def _install_acquaint(monkeypatch, **functions) -> None:
    module = types.ModuleType("acquaint")
    module.disclosure = lambda people, **_: need_to_know_disclosure(people)
    for name, function in functions.items():
        setattr(module, name, function)
    monkeypatch.setitem(sys.modules, "acquaint", module)


def _raising(error):
    def function(*args, **kwargs):
        raise error

    return function


def _clean_lint(text, *, recipient=None):
    return {"ok": True, "findings": [], "summary": "0 finding(s) to fix"}


# ---- reply mode, a row of the policy table ----


def test_draft_reply_mode_holds_the_message_for_the_operator():
    decision = _gate(subject=_subject(default_reply_mode="draft"))
    assert (decision.send, decision.flow, _rules(decision)) == (None, "approve", ["reply mode"])
    assert (decision.diverted, decision.diverted_by) == (DRAFT_REASON, "outbound_policy")


def test_direct_reply_mode_sends():
    decision = _gate()
    assert (decision.diverted, decision.flow, decision.verdict.flow) == (None, "send", "send")
    assert decision.send == _outbound("@pat " + TEXT)


def test_a_person_override_to_direct_sends_under_a_draft_default():
    decision = _gate(subject=_subject(default_reply_mode="draft", reply_modes={"pat": "direct"}))
    assert decision.diverted is None
    assert decision.send is not None


def test_a_person_override_to_draft_diverts_under_a_direct_default():
    decision = _gate(subject=_subject(reply_modes={"pat": "draft"}))
    assert (decision.send, decision.diverted) == (None, DRAFT_REASON)


def test_another_persons_override_does_not_apply():
    subject = _subject(default_reply_mode="draft", reply_modes={"someone-else": "direct"})
    assert _gate(subject=subject).diverted == DRAFT_REASON


# ---- a message outside a case (#28) ----


@pytest.mark.parametrize("mode", ["direct", "draft"])
def test_a_message_outside_a_case_is_held_whatever_its_reply_mode(mode):
    decision = _gate(subject=_subject(default_reply_mode=mode), case=False, case_id=None)
    assert decision.send is None and decision.concerns[0].rule == OUTSIDE_A_CASE
    assert (decision.diverted_by, decision.diverted.split("; ")[0]) == ("outside_a_case", CASELESS_REASON)


# ---- the operator's approval, bound to hashes (ADR 0002) ----


def test_an_approval_of_the_decision_shown_releases_what_it_names_and_says_so():
    subject = _subject(default_reply_mode="draft")
    shown = _gate(subject=subject)
    approval = _approved(shown, justification="  Pat asked for exactly this  ")

    assert (approval.rules_overridden, approval.justification) == (("reply mode",), "Pat asked for exactly this")
    assert (approval.payload_hash, approval.audience_hash) == (shown.payload_hash, shown.audience_hash)
    released = run_gate(_outbound(), _context(subject, approval=approval))

    assert released.send == _outbound("@pat " + TEXT) and released.bound
    assert [concern.rule for concern in released.settled] == ["reply mode"]
    assert f"released by operator at {NOW.isoformat()}, past: reply mode (Pat asked for exactly this)" in released.notes


def test_an_approval_settles_only_the_rules_it_names():
    subject = _subject(default_reply_mode="draft")
    approval = replace(_approved(_gate(subject=subject)), rules_overridden=("taint",))

    decision = run_gate(_outbound(), _context(subject, approval=approval))

    assert (decision.send, _rules(decision), decision.settled) == (None, ["reply mode"], ())


def test_a_refusal_is_never_settled_even_by_an_approval_that_names_it():
    subject = _subject(default_reply_mode="draft")
    leaky = "Use " + TOKEN
    shown = _gate(leaky, subject=subject)
    assert (shown.flow, shown.overridable) == ("refuse", ("reply mode",))
    approval = replace(_approved(shown), rules_overridden=("secrets", "reply mode"))

    decision = run_gate(_outbound(leaky), _context(subject, approval=approval))

    assert (decision.send, decision.flow, _rules(decision)) == (None, "refuse", ["secrets"])
    assert [concern.rule for concern in decision.settled] == ["reply mode"]


@pytest.mark.parametrize("change, what", [("text", "the message changed"), ("audience", "its audience changed")])
def test_an_approval_for_another_text_or_audience_is_void_and_the_new_verdict_stands(change, what):
    subject = _subject(default_reply_mode="draft")
    approval = _approved(_gate(subject=subject))
    outbound, context = _outbound(), _context(subject, approval=approval)
    if change == "text":
        outbound = _outbound(TEXT + " The totals row is back too.")
    else:
        context = replace(context, audience=_audience("public"))  # the repository went public meanwhile

    decision = run_gate(outbound, context)

    assert (decision.send, decision.settled, decision.bound) == (None, (), False)
    (void,) = [concern for concern in decision.concerns if concern.filter == "gate"]
    assert f"{what} since it was given" in void.text and not void.settleable
    assert "reply mode" in _rules(decision)
    if change == "audience":
        assert "irreversibility" in _rules(decision)


def test_an_approval_bound_to_nothing_releases_nothing_nor_does_a_stand_in():
    subject = _subject(default_reply_mode="draft")
    unbound = Approval(by="operator", at=NOW, rules_overridden=("reply mode",))
    decision = run_gate(_outbound(), _context(subject, approval=unbound))
    assert decision.send is None and decision.diverted_by == "gate"
    assert f"the approval by operator at {NOW.isoformat()} is void: it is bound to no message" in decision.diverted
    for stand_in in ({"by": "operator", "at": NOW, "rules_overridden": ["reply mode"]}, "operator", True):
        context = replace(_context(subject), approval=stand_in)
        assert run_gate(_outbound(), context).diverted == DRAFT_REASON


def test_an_approval_never_settles_a_concern_that_has_no_rule():
    no_handle = _subject(default_reply_mode="draft", people={"webinbox:pat": "pat"})
    approval = replace(_approved(_gate(subject=no_handle)), rules_overridden=("reply mode", "notify_recipient"))

    decision = run_gate(_outbound(), _context(no_handle, approval=approval))

    assert (decision.diverted_by, decision.diverted) == ("notify_recipient", "no handle to notify pat")


def test_the_tick_context_carries_no_approval_audience_or_provenance_unless_given():
    context = GateContext(subject=_subject(), now=NOW)
    assert (context.approval, context.audience, context.provenance) == (None, None, None)


def test_without_an_audience_or_a_provenance_the_policy_assumes_public_and_tainted():
    decision = run_gate(_outbound(), GateContext(subject=_subject(), case=CASE, now=NOW, fingerprint_key=KEY))
    assert decision.send is None
    assert {"taint", "irreversibility", "unknown audience"} <= set(decision.verdict.rules)
    assert decision.verdict.axes["audience"] == "public" and decision.verdict.axes["tainted"] is None


# ---- the outbound policy (mutation-checked) ----

LEAK_TERMS = ("example-internal",)
LEAKS = {
    "macos-home-path": ("local path", "The log is at " + "/Us" + "ers/someone/app/log.txt"),
    "linux-home-path": ("local path", "I looked in " + "/ho" + "me/someone/app/src"),
    "root-home-path": ("local path", "It lives in " + "/ro" + "ot/app/config"),
    "windows-home-path": ("local path", "It lives in " + "C:" + "\\Users\\someone\\app"),
    "email": ("email", "Write to " + "someone" + "@" + "example.com" + " for access."),
    "email-with-a-local-part-past-the-bound": ("email", "Write to " + "some.one" * 12 + "@" + "example.com"),
    "classic-github-token": ("token", "Use " + "ghp_" + "a" * 36),
    "fine-grained-github-token": ("token", "Use " + "github_pat_" + "a" * 40),
    "sk-key": ("token", "Use " + "sk-" + "a" * 24),
    "aws-key": ("token", "Use " + "AKIA" + "A" * 16),
    "hugging-face-token": ("token", "Use " + "hf_" + "a" * 34),
    "slack-token": ("token", "Use " + "xox" + "b-" + "1" * 12 + "-" + "a" * 24),
    "token-wrapped-across-lines": ("token", "Use " + "ghp_" + "a" * 18 + "\n" + "a" * 18),
    "windows-home-path-json-escaped": (
        "local path",
        '{"log": "' + "C:" + "\\\\" + "Users" + "\\\\" + "someone" + "\\\\" + "app.log" + '"}',
    ),
    "wsl-mounted-home-path": ("local path", "It lives in " + "/mnt/c" + "/Us" + "ers/someone/app"),
    "macos-private-var-path": ("local path", "The log is at " + "/private" + "/var/folders/xy/T/app.log"),
    "macos-var-folders-path": ("local path", "The log is at " + "/var" + "/folders/xy/T/app.log"),
    "env-file-path": ("env file", "The key is in " + "app/" + ".env" + "."),
    "private-key": ("private key", "-----BEGIN " + "OPENSSH PRIVATE KEY" + "-----\nb3BlbnNzaC1rZXktdjE\n"),
    "pgp-private-key": ("private key", "-----BEGIN " + "PGP PRIVATE KEY BLOCK" + "-----\n\nlQOYBGZ\n"),
    "leak-term": ("leak term", "This is on the Example-Internal board."),
}
#: The rule that holds each 0.1 leak back on a public repository, and its flow: a leak term
#: is a label no reader is cleared for, which a case can revise.
ON_A_PUBLIC_REPOSITORY = {
    "local path": ("exfiltration", "refuse"),
    "env file": ("exfiltration", "refuse"),
    "email": ("personal, public", "refuse"),
    "private key": ("secrets", "refuse"),
    "token": ("secrets", "refuse"),
    "leak term": ("no write-down", "revise"),
}


@pytest.mark.parametrize("kind, text", list(LEAKS.values()), ids=list(LEAKS))
def test_the_policy_holds_back_every_0_1_leak_on_a_public_repository(kind, text):
    decision = _gate(text, subject=_subject(leak_terms=LEAK_TERMS), visibility="public")
    rule, flow = ON_A_PUBLIC_REPOSITORY[kind]
    assert (decision.send, decision.flow, decision.concerns[0].rule) == (None, flow, rule)
    assert decision.concerns[0].findings


@pytest.mark.parametrize("visibility", ["public", "private"])
@pytest.mark.parametrize("kind, text", list(LEAKS.values()), ids=list(LEAKS))
def test_the_audience_decides_not_the_channel_name(kind, text, visibility):
    """``public_channels`` is read and decides nothing: a public repository is public whatever
    the list says, and a leak to a private one is still held back."""
    subject = _subject(leak_terms=LEAK_TERMS, public_channels=())
    decision = _gate(text, subject=subject, visibility=visibility)
    assert decision.send is None and decision.verdict.findings


def test_without_the_policy_filter_every_leak_would_go_out():
    filters = tuple(f for f in DFLT_OUTBOUND_FILTERS if f is not outbound_policy)
    context = _context(_subject(leak_terms=LEAK_TERMS), visibility="public")
    assert all(run_gate(_outbound(text), context, outbound_filters=filters).send for _, text in LEAKS.values())


def test_the_policy_never_redacts_and_never_repeats_what_it_found():
    decision = _gate("Use " + TOKEN, visibility="public")

    assert decision.send is None
    (secret,) = [concern for concern in decision.concerns if concern.rule == "secrets"]
    ((finding),) = secret.findings
    assert (finding.kind, finding.start, finding.end) == ("secret", 4, 4 + len(TOKEN))
    assert TOKEN not in repr(decision.record()) + repr(decision.notes) + decision.diverted


def test_a_draft_holding_a_token_is_refused_with_the_secret_finding_attached():
    """Acceptance (#36): every filter runs, so draft reply mode no longer hides what a draft holds."""
    decision = _gate("Use " + TOKEN, subject=_subject(default_reply_mode="draft"))

    assert (decision.flow, decision.concerns[0].rule) == ("refuse", "secrets")
    assert [finding.kind for finding in decision.concerns[0].findings] == ["secret"]
    assert "reply mode" in _rules(decision)
    assert decision.summary()["flow"] == "refuse"


def test_a_finding_in_the_title_is_placed_in_the_title():
    decision = _gate(
        subject=_subject(leak_terms=LEAK_TERMS), ref="github:example/app", title="On the Example-Internal board"
    )
    (term,) = [concern for concern in decision.concerns if concern.rule == "no write-down"]
    assert "(at characters 7–23 of the title)" in term.text
    assert term.findings[0].part == "title"


#: A message of a million characters and no leak: one unbroken run of the characters an
#: email's local part may hold, which the 0.1 email pattern scanned in quadratic time.
LONG_WORD = "a-b.c+d%e_" * 100_000
#: How long the policy may take over it; the quadratic pattern took minutes.
LONG_WORD_SCAN_BOUND_S = 5.0


def test_the_policy_over_a_million_character_word_is_linear():
    """S8 #6: every pattern is bounded and anchored, so a long word is scanned once."""
    started = time.perf_counter()
    run_gate(_outbound(LONG_WORD), _context(), outbound_filters=(outbound_policy,))
    elapsed = time.perf_counter() - started
    assert elapsed < LONG_WORD_SCAN_BOUND_S, f"the policy took {elapsed:.1f}s"


def test_the_decision_names_the_filter_of_its_strongest_concern():
    """S8 #2: a notification names the filter, not the reason, which can quote what a filter
    raised."""
    assert _gate("Use " + TOKEN).diverted_by == "outbound_policy"
    assert _gate(case=False, case_id=None).diverted_by == "outside_a_case"
    assert _gate().diverted_by is None


@pytest.mark.parametrize("text", ["The example-internals tab.", "The xexample-internal tab."])
def test_leak_terms_match_whole_words_only(text):
    assert _gate(text, subject=_subject(leak_terms=LEAK_TERMS)).verdict.findings == ()


@pytest.mark.parametrize(
    "text",
    [
        "Thanks @pat, see https://example.com/ho" + "me/page/x",
        "The task-" + "a" * 24 + " step passed.",
        "The sk-" + "a" * 5 + " prefix is too short to be a key.",
        "Install it under " + "C:" + "\\\\" + "Program Files" + "\\\\" + "app",
        "The data sits in " + "/mnt/c" + "/Program Files/app",
        "The server logs to " + "/var" + "/log/app.log",
        "Copy the " + ".env" + " file from the template.",
        "The direnv file is " + "app/" + ".env" + "rc",
        "-----BEGIN " + "PUBLIC KEY" + "-----",
        "-----BEGIN " + "PGP PUBLIC KEY BLOCK" + "-----",
        "-----BEGIN " + "PGP SIGNATURE" + "-----",
        "Call " + "hf_" + "hub_download() for the weights.",
        "Hugs and " + "xox" + "o-xoxo from the team.",
        "The " + "ghp_" + "\n" + "prefix alone is no token.",
    ],
)
def test_what_only_looks_like_a_0_1_leak_is_not_found(text):
    findings = _gate(text).verdict.findings
    assert not [f for f in findings if f.kind == "secret" or f.rule in ("local-path", "env-file", "email-address")]


def test_the_disclosure_seam_is_asked_for_exactly_the_messages_readers():
    asked = []

    def disclosure(people, *, projects, audience, today):
        asked.append((list(people), tuple(projects), audience["ref"], today))
        return {
            "people": {"pat": {"tier": "open", "clearance": "amber"}},
            "least_clearance": "amber",
            "vocabulary": [{"term": "Heron", "entity": "project:heron", "label": "red", "sealed_from": []}],
        }

    policy = functools.partial(outbound_policy, disclosure=disclosure)
    outbound = _outbound("Heron slips to October.", project="heron", cc=("email:cy",))
    decision = run_gate(outbound, _context(), outbound_filters=(policy,))

    assert asked == [(["pat", "email:cy"], ("heron",), REF, NOW.date().isoformat())]
    assert "no write-down" in _rules(decision)
    assert decision.consulted["disclosure"]["labels"] == {"project:heron": "red"}


def test_an_acquaint_that_imports_and_fails_holds_every_message_back(monkeypatch):
    _install_acquaint(
        monkeypatch, disclosure=_raising(AcquaintError("store unreadable")), brief=_raising(AcquaintError("x")),
        style_lint=_clean_lint,
    )
    decision = _gate()
    assert (decision.flow, decision.diverted_by) == ("approve", "outbound_policy")
    assert decision.diverted == "outbound_policy failed: AcquaintError: store unreadable"
    assert decision.concerns[0].rule is None  # no approval releases a message nobody could judge


def test_without_acquaint_every_reader_is_need_to_know_and_leak_terms_are_the_vocabulary():
    decision = _gate("On the Example-Internal board.", subject=_subject(leak_terms=LEAK_TERMS))
    consulted = decision.consulted["disclosure"]
    assert consulted["source"].startswith("no acquaint")
    assert consulted["labels"] == {"policy.leak_terms": LABELS[-1]}
    assert decision.verdict.readers["pat"]["tier"] == "need-to-know"


# ---- writing card and deslop ----


def test_without_acquaint_the_writing_card_and_deslop_degrade_to_notes():
    decision = _gate()
    assert decision.send is not None
    audience_note, card_note, deslop_note, _mention_note = decision.notes
    assert audience_note.startswith("audience: named readers")
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
    assert decision.notes[1:] == ("writing card: brief for Pat (reply)", "added the mention @pat")


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
    assert (decision.send, decision.diverted, decision.diverted_by) == (None, "deslop: 2 enforced finding(s)", "deslop")
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


# ---- order and run_gate: every filter runs ----


def test_default_filters_are_in_the_documented_order():
    assert DFLT_OUTBOUND_FILTERS == (outside_a_case, outbound_policy, writing_card, deslop, notify_recipient)


def test_every_filter_runs_and_the_most_restrictive_concern_leads():
    ran = []

    def first(outbound, ctx):
        ran.append("first")
        return Pass(replace(outbound, text="rewritten"), notes=("first note",))

    def second(outbound, ctx):
        ran.append(("second", outbound.text))
        return Divert("second asks the operator", notes=("second note",))

    def third(outbound, ctx):
        ran.append(("third", outbound.text))
        return Divert("third refuses", flow="refuse")

    def fourth(outbound, ctx):
        ran.append("fourth")
        return Pass(outbound, notes=("fourth note",))

    decision = run_gate(_outbound(), _context(), outbound_filters=(first, second, third, fourth))

    assert ran == ["first", ("second", "rewritten"), ("third", "rewritten"), "fourth"]
    assert (decision.send, decision.flow, decision.diverted_by) == (None, "refuse", "third")
    assert decision.diverted == "third refuses; second asks the operator"
    assert decision.notes == ("first note", "second note", "fourth note")
    assert [(c.filter, c.flow) for c in decision.concerns] == [("third", "refuse"), ("second", "approve")]


def test_a_rewrite_reaches_a_send_only_when_nothing_holds_the_message_back_and_the_hash_is_of_the_text_judged():
    def mention(outbound, ctx):
        return Pass(replace(outbound, text="@pat " + outbound.text))

    def asks(outbound, ctx):
        return Divert("asks")

    assert run_gate(_outbound(), _context(), outbound_filters=(asks, mention)).send is None
    sent = run_gate(_outbound(), _context(), outbound_filters=(mention,))
    assert sent.send == _outbound("@pat " + TEXT)
    assert sent.payload_hash == payload_hash(_outbound())


def test_run_gate_fails_closed_when_a_filter_raises_or_answers_nonsense():
    """Acceptance (#36): a filter that raises contributes approve, with the error as its reason."""

    def broken(outbound, ctx):
        raise RuntimeError("boom")

    def confused(outbound, ctx):
        return True

    def empty_handed(outbound, ctx):
        return Pass(None)

    raised = run_gate(_outbound(), _context(), outbound_filters=(broken,))
    assert (raised.send, raised.flow, raised.diverted) == (None, "approve", "broken failed: RuntimeError: boom")
    assert raised.concerns[0].rule is None
    decision = run_gate(_outbound(), _context(), outbound_filters=(confused, empty_handed))
    assert decision.diverted == (
        "confused returned bool, not a Pass or a Divert; empty_handed returned NoneType, not a Pass or a Divert"
    )


@pytest.mark.parametrize("flow", ["send", "sometime"])
def test_a_divert_with_a_flow_that_holds_nothing_back_is_read_as_approve(flow):
    def odd(outbound, ctx):
        return Divert("odd", flow=flow)

    decision = run_gate(_outbound(), _context(), outbound_filters=(odd,))
    assert (decision.send, decision.flow) == (None, "approve")
    assert decision.diverted == f"odd (odd diverted with the flow {flow!r}, read as approve)"


def test_a_delay_waits_for_the_operator_until_the_outbox_exists():
    decision = _gate(visibility="public")
    assert (decision.send, decision.flow, _rules(decision)) == (None, "delay", ["irreversibility"])
    assert decision.diverted.endswith(DELAY_HELD)
    assert decision.overridable == ("irreversibility",)


def test_run_gate_without_filters_sends_the_message_unchanged():
    decision = run_gate(_outbound(), _context(), outbound_filters=())
    assert (decision.send, decision.diverted, decision.flow, decision.concerns) == (_outbound(), None, "send", ())


def test_the_record_holds_the_verdict_the_consulted_labels_and_the_approval_never_a_term():
    subject = _subject(default_reply_mode="draft", leak_terms=LEAK_TERMS)
    text = "It is on the Example-Internal board."
    shown = _gate(text, subject=subject)
    approval = _approved(shown, justification="that board is public now")

    decision = run_gate(_outbound(text), _context(subject, approval=approval))
    record = decision.record()

    assert (decision.flow, record["flow"], record["approval_bound"]) == ("send", "send", True)
    assert sorted(c["rule"] for c in record["settled"]) == ["no write-down", "reply mode"]
    assert record["approval"]["justification"] == "that board is public now"
    assert sorted(record["approval"]["rules_overridden"]) == ["no write-down", "reply mode"]
    assert record["verdict"]["audience"]["scope"] == "named" and record["verdict"]["mode"] == "enforce"
    (finding,) = record["verdict"]["findings"]
    assert (finding["kind"], finding["start"]) == ("vocabulary", 13) and finding["fingerprint"]
    assert record["consulted"]["disclosure"]["labels"] == {"policy.leak_terms": LABELS[-1]}
    assert record["consulted"]["provenance"]["tainted"] is False
    assert "Example-Internal" not in repr(record) and "example-internal" not in repr(record)
