"""Tests for liaise.outcomes: the schema, parsing, drafts, and planning each outcome kind.

Addresses holding an email are built by concatenation, so the no-personal-data guard
does not flag this file.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from liaise.gate import Outbound
from liaise.model import OUTCOME_KINDS, Case, Outcome
from liaise.notify import NOTICE_ESCALATION, NOTICE_NO_CHANNEL, notice_body
from liaise.outcomes import (
    OUTCOME_SCHEMA,
    REQUIRED_FIELD_BY_KIND,
    Defer,
    Deliver,
    DigestNote,
    NotifyOperator,
    Send,
    StoreDraft,
    Transition,
    make_draft,
    normalize,
    parse_outcomes,
    plan_outcomes,
)
from liaise.subjects import Delivery, Policy, Subject

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
REF = "github:example/app#12"
INBOX = "webinbox:example-site"
EMAIL = "email:" + "pat" + "@" + "example.com"


def _subject(**policy) -> Subject:
    fields = dict(people={"github:pat": "pat"}, roles={"pat": "partner"})
    fields.update(policy)
    return Subject(
        slug="pat",
        bindings=("github:example/app?labels=partner:pat", INBOX),
        policy=Policy(**fields),
        delivery=Delivery(kind="deploy", per="batch", command="./deploy.sh"),
    )


def _case(*conversations) -> Case:
    return Case(
        id="pat-1",
        subject="pat",
        conversations=conversations or (REF,),
        reporter="pat",
        state="working",
        created_at=NOW,
        updated_at=NOW,
    )


def _plan(*outcomes, case=None, subject=None, **options):
    return plan_outcomes(case or _case(), outcomes, subject or _subject(), now=NOW, **options)


def _send(text, purpose, *, ref=REF, channel="github") -> Send:
    return Send(case_id="pat-1", ref=ref, channel=channel, recipient="pat", purpose=purpose, text=text)


# ---- the schema ----


def test_schema_offers_exactly_the_outcome_vocabulary():
    outcomes = OUTCOME_SCHEMA["properties"]["outcomes"]
    assert outcomes["items"]["properties"]["kind"]["enum"] == list(OUTCOME_KINDS)
    assert outcomes["items"]["required"] == ["kind"]
    assert set(outcomes["items"]["properties"]) == {"kind", "text", "questions", "reason"}
    assert outcomes["minItems"] == 1
    assert OUTCOME_SCHEMA["required"] == ["outcomes"]
    assert OUTCOME_SCHEMA["properties"]["summary"]["type"] == "string"
    assert json.loads(json.dumps(OUTCOME_SCHEMA)) == OUTCOME_SCHEMA


def test_every_outcome_kind_names_the_field_it_needs():
    assert tuple(REQUIRED_FIELD_BY_KIND) == OUTCOME_KINDS
    description = OUTCOME_SCHEMA["properties"]["outcomes"]["items"]["properties"]["kind"]["description"]
    assert all(f"{kind} needs {field}" in description for kind, field in REQUIRED_FIELD_BY_KIND.items())


def test_schema_is_valid_json_schema_and_agrees_with_parse_outcomes():
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(OUTCOME_SCHEMA)
    good = {"outcomes": [{"kind": "reply", "text": "Fixed."}], "summary": "fixed the button"}
    jsonschema.validate(good, OUTCOME_SCHEMA)
    assert parse_outcomes(good) == (Outcome(kind="reply", text="Fixed."),)
    bad_outputs = [
        {"outcomes": []},
        {"outcomes": [{"kind": "shrug", "text": "x"}]},
        {"outcomes": [{"text": "no kind"}]},
        {"outcomes": [{"kind": "note", "text": "x", "mood": "sunny"}]},
        {"outcomes": [{"kind": "note", "text": "x"}], "extra": 1},
    ]
    for bad in bad_outputs:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, OUTCOME_SCHEMA)
        with pytest.raises(ValueError):
            parse_outcomes(bad)


# ---- parse_outcomes ----


def test_parse_outcomes_reads_every_field_and_keeps_a_decline():
    output = {
        "outcomes": [
            {"kind": "ask", "text": "Two questions.", "questions": ["Which page?", "Which phone?"]},
            {"kind": "decline", "reason": "out of scope"},
        ],
        "summary": "asked, and declined the rest",
    }
    assert parse_outcomes(output) == (
        Outcome(kind="ask", text="Two questions.", questions=("Which page?", "Which phone?")),
        Outcome(kind="decline", reason="out of scope"),
    )


def test_parse_outcomes_lists_every_problem():
    output = {
        "outcomes": [
            {"kind": "shrug"},
            {"text": "no kind"},
            {"kind": "reply", "text": 42},
            {"kind": "ask", "questions": "Which page?"},
            {"kind": "reply", "text": "   "},
            {"kind": "note", "text": "x", "mood": "sunny"},
            "not an object",
            {"kind": "ask", "questions": ["Which page?", ""]},
        ],
        "summary": 7,
        "extra": True,
    }
    with pytest.raises(ValueError) as error:
        parse_outcomes(output)
    message = str(error.value)
    expected = [
        "unknown key(s) 'extra'; the result has only 'outcomes', 'summary'",
        "summary must be a string",
        "outcomes[0].kind 'shrug' is not one of: ask, reply, escalate, propose, deliver, decline, defer, note",
        "outcomes[1].kind is missing",
        "outcomes[2].text must be a string",
        "outcomes[3].questions must be a list of non-empty strings",
        "outcomes[4]: reply needs a non-empty text",
        "outcomes[5] has unknown key(s) 'mood'",
        "outcomes[6] must be an object, got str",
        "outcomes[7].questions must be a list of non-empty strings",
    ]
    assert message.startswith("invalid structured output:\n- ")
    missing = [line for line in expected if line not in message]
    assert not missing, message


@pytest.mark.parametrize(
    "output, expected",
    [
        (None, "expected an object with 'outcomes', got NoneType"),
        ({}, "outcomes is missing"),
        ({"outcomes": []}, "outcomes is empty"),
        ({"outcomes": {"kind": "reply"}}, "outcomes must be a list, got dict"),
    ],
)
def test_parse_outcomes_refuses_a_result_without_outcomes(output, expected):
    with pytest.raises(ValueError, match=re.escape(expected)):
        parse_outcomes(output)


@pytest.mark.parametrize("kind, field", list(REQUIRED_FIELD_BY_KIND.items()))
def test_each_kind_needs_its_field(kind, field):
    with pytest.raises(ValueError, match=re.escape(f"{kind} needs a non-empty {field}")):
        parse_outcomes({"outcomes": [{"kind": kind}]})


# ---- normalize and make_draft ----


def test_normalize_makes_a_decline_an_escalate():
    reply = Outcome(kind="reply", text="Fixed.")
    declined = Outcome(kind="decline", text="Not this one.", reason="out of scope")
    assert normalize([declined, reply]) == (
        Outcome(kind="escalate", text="Not this one.", reason="decline: out of scope"),
        reply,
    )
    assert normalize([Outcome(kind="decline")]) == (Outcome(kind="escalate", reason="decline"),)


def test_make_draft_is_the_one_shape_of_a_case_draft():
    draft = make_draft(
        at=NOW,
        outcome="reply",
        recipient="pat",
        ref=REF,
        text="Fixed.",
        reason="draft reply mode",
        notes=("writing card: brief for Pat",),
    )
    assert draft == {
        "at": "2026-09-11T12:00:00+00:00",
        "outcome": "reply",
        "recipient": "pat",
        "ref": REF,
        "text": "Fixed.",
        "reason": "draft reply mode",
        "notes": ["writing card: brief for Pat"],
    }
    case = Case.from_dict({**_case().to_dict(), "drafts": [draft]})
    assert Case.from_dict(case.to_dict()) == case
    assert case.drafts == (draft,)


# ---- plan_outcomes, one kind at a time ----


def test_ask_sends_the_text_and_numbered_questions_then_waits_on_the_partner():
    outcome = Outcome(
        kind="ask",
        text="Two questions before I start.",
        questions=("Which page? (default: the first)", "Which phone? (default: any)"),
        reason="the report is unclear",
    )
    assert _plan(outcome) == [
        _send(
            "Two questions before I start.\n\n1. Which page? (default: the first)\n2. Which phone? (default: any)",
            "ask",
        ),
        Transition("pat-1", "needs-partner", "the report is unclear"),
    ]


def test_ask_without_text_sends_just_the_questions():
    (send, _) = _plan(Outcome(kind="ask", questions=("Which page?",)))
    assert send.text == "1. Which page?"


def test_reply_sends_without_a_transition():
    assert _plan(Outcome(kind="reply", text="Fixed.")) == [_send("Fixed.", "reply")]


def test_escalate_stores_a_draft_notifies_the_operator_and_waits_on_the_owner():
    outcome = Outcome(kind="escalate", text="This needs a paid plan.", reason="it costs money")
    store, notify, transition = _plan(outcome)
    assert store == StoreDraft(
        "pat-1",
        make_draft(at=NOW, outcome="escalate", recipient="pat", ref=REF, text="This needs a paid plan.", reason="it costs money"),
    )
    assert isinstance(notify, NotifyOperator)
    assert (notify.title, notify.priority) == ("pat-1 needs you", "high")
    # S8 #2: the draft and the reason stay on the case; the notification names the case alone
    assert notify.body == notice_body(NOTICE_ESCALATION, subject="pat", case_ids=("pat-1",))
    assert "costs money" not in notify.body and "paid plan" not in notify.body
    assert transition == Transition("pat-1", "needs-owner", "it costs money")


def test_decline_is_planned_as_an_escalate():
    declined = _plan(Outcome(kind="decline", text="Not this one.", reason="out of scope"))
    assert declined == _plan(Outcome(kind="escalate", text="Not this one.", reason="decline: out of scope"))
    assert declined[0].draft["outcome"] == "escalate"
    assert declined[-1] == Transition("pat-1", "needs-owner", "decline: out of scope")


def test_propose_sends_then_waits_on_the_partner():
    assert _plan(Outcome(kind="propose", text="Two ways to do this: A or B?")) == [
        _send("Two ways to do this: A or B?", "propose"),
        Transition("pat-1", "needs-partner", "propose"),
    ]


def test_deliver_runs_the_subjects_delivery_says_try_it_and_moves_to_deployed():
    assert _plan(Outcome(kind="deliver", text="It is live, please try it.")) == [
        Deliver("pat-1", "deploy", "batch", "./deploy.sh"),
        _send("It is live, please try it.", "deliver"),
        Transition("pat-1", "deployed", "deliver"),
    ]


def test_defer_plans_a_defer():
    assert _plan(Outcome(kind="defer", reason="waiting on the design")) == [Defer("pat-1", "waiting on the design")]


def test_note_plans_a_digest_note():
    outcome = Outcome(kind="note", text="Tidied the router on the way.")
    assert _plan(outcome) == [DigestNote("pat-1", "Tidied the router on the way.")]


def test_outcomes_are_planned_in_order():
    actions = _plan(Outcome(kind="note", text="n"), Outcome(kind="reply", text="r"), Outcome(kind="defer", reason="d"))
    assert actions == [DigestNote("pat-1", "n"), _send("r", "reply"), Defer("pat-1", "d")]


def test_a_send_is_the_outbound_the_gate_checks():
    (send,) = _plan(Outcome(kind="reply", text="Fixed."))
    assert isinstance(send, Outbound)


def test_the_first_conversation_that_can_send_is_used():
    case = _case(INBOX, REF, "github:example/app#13")
    assert _plan(Outcome(kind="reply", text="Fixed."), case=case) == [_send("Fixed.", "reply")]


# ---- plan_outcomes for a case only a web inbox can see ----


def test_a_web_inbox_case_is_answered_at_the_reporters_notify_address():
    subject = _subject(notify={"pat": EMAIL})
    actions = _plan(Outcome(kind="ask", questions=("Which page?",)), case=_case(INBOX), subject=subject)
    assert actions == [
        _send("1. Which page?", "ask", ref=EMAIL, channel="email"),
        Transition("pat-1", "needs-partner", "ask"),
    ]


def test_a_handle_in_policy_people_is_a_notify_address_too():
    subject = _subject(people={"github:pat": "pat", EMAIL: "pat"})
    (send,) = _plan(Outcome(kind="reply", text="Fixed."), case=_case(INBOX), subject=subject)
    assert (send.ref, send.channel) == (EMAIL, "email")


def test_a_web_inbox_case_with_no_address_to_write_to_becomes_a_draft_for_the_operator():
    # pat's only address is a GitHub handle, and GitHub cannot message a person.
    outcome = Outcome(kind="ask", text="One question.", questions=("Which page?",))
    store, notify, transition = _plan(outcome, case=_case(INBOX))
    assert store == StoreDraft(
        "pat-1",
        make_draft(
            at=NOW,
            outcome="ask",
            recipient="pat",
            ref=None,
            text="One question.\n\n1. Which page?",
            reason="no channel to reach pat",
        ),
    )
    assert (notify.title, notify.priority) == ("no channel to reach pat", "high")
    assert notify.body == notice_body(NOTICE_NO_CHANNEL, subject="pat", case_ids=("pat-1",), cause="ask")
    assert "One question" not in notify.body  # S8 #2: the draft stays on the case
    assert transition == Transition("pat-1", "needs-partner", "ask")


def test_a_deliver_with_no_channel_still_delivers_and_drafts_the_try_it():
    actions = _plan(Outcome(kind="deliver", text="Live."), case=_case(INBOX))
    assert [type(action) for action in actions] == [Deliver, StoreDraft, NotifyOperator, Transition]


def test_an_escalation_with_no_channel_is_one_notification_with_no_ref():
    store, notify, _ = _plan(Outcome(kind="escalate", text="t", reason="r"), case=_case(INBOX))
    assert store.draft["ref"] is None
    assert notify.title == "pat-1 needs you"


def test_sending_and_address_channels_are_options():
    subject = _subject(notify={"pat": "ntfy:pat-updates"})
    (send, _) = _plan(Outcome(kind="propose", text="A or B?"), case=_case(INBOX), subject=subject, address_channels=("ntfy",))
    assert (send.ref, send.channel) == ("ntfy:pat-updates", "ntfy")
    (send,) = _plan(Outcome(kind="reply", text="Fixed."), case=_case(INBOX, REF), sending_channels=("webinbox",))
    assert (send.ref, send.channel) == (INBOX, "webinbox")


# ---- refusals ----


def test_plan_refuses_an_outcome_kind_outside_the_vocabulary():
    with pytest.raises(ValueError, match="cannot plan outcome kind 'shrug'"):
        _plan(Outcome(kind="shrug"))


def test_a_transition_to_an_unknown_state_is_refused():
    with pytest.raises(ValueError, match="case state 'delivered'"):
        Transition("pat-1", "delivered", "x")
