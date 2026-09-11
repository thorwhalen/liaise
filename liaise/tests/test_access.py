"""Tests for liaise.access: resolving senders, and the label-as-claim rule.

The label-as-claim tests are the mutation check the spec asks for. Drop the relay
check and the untrusted-author tests fail. Drop label claims and the relay test fails.
Look at labels before the author's own role and the role-holder tests fail.

No test here ever reads the operator's real people records: an autouse fixture makes
``acquaint`` unimportable, and the tests that need it install a fake.
"""

from __future__ import annotations

import sys
import types

import pytest
from correspond.model import Grade
from correspond.testing import demo_message

from liaise.access import AccessDecision, authorize, is_relay, resolve_person
from liaise.subjects import Policy, Subject

REF = "github:example/app#12"
RELAY = "github:example-bot"


@pytest.fixture(autouse=True)
def no_real_acquaint(monkeypatch):
    monkeypatch.setitem(sys.modules, "acquaint", None)


def _subject(**policy) -> Subject:
    fields = dict(
        people={"github:pat": "pat"},
        roles={"pat": "partner"},
        relays=(RELAY,),
        claim_labels={"partner:pat": "pat"},
    )
    fields.update(policy)
    return Subject(
        slug="pat",
        bindings=("github:example/app?labels=partner:pat",),
        policy=Policy(**fields),
    )


def _message(handle="pat", *, labels=(), grade=Grade.PLATFORM):
    return demo_message(conversation=REF, handle=handle, labels=labels, grade=grade)


def _fake_acquaint(monkeypatch, result=None, *, error=None) -> list[str]:
    calls: list[str] = []

    def resolve(handle, *, data_dir=None):
        calls.append(handle)
        if error is not None:
            raise error
        return result

    module = types.ModuleType("acquaint")
    module.resolve = resolve
    monkeypatch.setitem(sys.modules, "acquaint", module)
    return calls


# ---- label-as-claim (mutation-checked) ----


def test_relay_author_with_a_claim_label_is_attributed_to_the_claimed_person():
    decision = authorize(_message("example-bot", labels=("partner:pat",)), _subject(), "report")
    assert decision == AccessDecision(
        person="pat",
        role="partner",
        grade="platform",
        permission="report",
        allowed=True,
        reason="allowed",
        via="relay-label",
    )


def test_claim_label_from_an_author_who_is_not_a_relay_is_refused():
    decision = authorize(_message("ada-lovelace", labels=("partner:pat",)), _subject(), "report")
    assert not decision.allowed
    assert decision.reason == "label claim by an untrusted author"
    assert (decision.person, decision.via) == (None, "none")


def test_claim_label_from_a_resolved_author_without_a_role_is_refused():
    subject = _subject(people={"github:pat": "pat", "github:ada-lovelace": "ada-lovelace"})
    decision = authorize(_message("ada-lovelace", labels=("partner:pat",)), subject, "report")
    assert not decision.allowed
    assert decision.reason == "label claim by an untrusted author"
    assert (decision.person, decision.via) == ("ada-lovelace", "none")


def test_role_holding_author_is_allowed_by_handle_without_any_label():
    decision = authorize(_message("pat"), _subject(), "request_work")
    assert decision.allowed
    assert (decision.person, decision.role, decision.via) == ("pat", "partner", "handle")


def test_role_holding_author_is_attributed_by_handle_whatever_the_labels():
    subject = _subject(
        roles={"pat": "partner", "ada-lovelace": "observer"},
        claim_labels={"partner:pat": "pat", "observer:ada-lovelace": "ada-lovelace"},
    )
    message = _message("pat", labels=("observer:ada-lovelace",))
    decision = authorize(message, subject, "request_work")
    assert decision.allowed
    assert (decision.person, decision.role, decision.via) == ("pat", "partner", "handle")


def test_relay_claim_is_carried_at_the_relay_grade():
    message = _message("example-bot", labels=("partner:pat",), grade=Grade.CLAIMED)
    decision = authorize(message, _subject(), "request_work")
    assert not decision.allowed
    assert decision.reason == "grade claimed not accepted for request_work"
    assert (decision.person, decision.via, decision.grade) == ("pat", "relay-label", "claimed")


def test_relay_without_a_claim_label_is_an_unresolved_sender():
    decision = authorize(_message("example-bot", labels=("bug",)), _subject(), "report")
    assert (decision.allowed, decision.reason, decision.via) == (False, "unresolved sender", "none")


def test_relay_labels_claiming_two_people_are_refused():
    subject = _subject(
        roles={"pat": "partner", "ada-lovelace": "partner"},
        claim_labels={"partner:pat": "pat", "partner:ada-lovelace": "ada-lovelace"},
    )
    message = _message("example-bot", labels=("partner:pat", "partner:ada-lovelace"))
    decision = authorize(message, subject, "report")
    assert not decision.allowed
    assert decision.reason == "label claims name more than one person"


# ---- the other refusals ----


def test_unresolved_sender_is_refused():
    decision = authorize(_message("ada-lovelace"), _subject(), "report")
    assert (decision.allowed, decision.reason, decision.person, decision.via) == (
        False,
        "unresolved sender",
        None,
        "none",
    )


def test_resolved_sender_without_a_role_is_refused():
    subject = _subject(people={"github:pat": "pat", "github:ada-lovelace": "ada-lovelace"})
    decision = authorize(_message("ada-lovelace"), subject, "report")
    assert (decision.allowed, decision.reason, decision.person) == (
        False,
        "no role on this subject",
        "ada-lovelace",
    )


def test_grade_not_accepted_for_the_permission_is_refused():
    decision = authorize(_message("pat", grade=Grade.CLAIMED), _subject(), "request_work")
    assert not decision.allowed
    assert decision.reason == "grade claimed not accepted for request_work"
    assert decision.via == "handle"


def test_claimed_grade_is_enough_to_report():
    assert authorize(_message("pat", grade=Grade.CLAIMED), _subject(), "report").allowed


def test_forged_grade_is_refused_even_to_report():
    decision = authorize(_message("pat", grade=Grade.FORGED), _subject(), "report")
    assert decision.reason == "grade forged not accepted for report"


def test_role_without_the_permission_is_refused():
    decision = authorize(_message("pat"), _subject(roles={"pat": "observer"}), "request_work")
    assert not decision.allowed
    assert decision.reason == "role observer lacks request_work"


def test_unknown_permission_raises():
    with pytest.raises(ValueError, match="permission 'deploy'"):
        authorize(_message("pat"), _subject(), "deploy")


def test_resolver_is_injectable():
    def resolver(address, subject):
        return "pat" if address == "github:pat" else None

    decision = authorize(_message("pat"), _subject(people={}), "report", resolver=resolver)
    assert decision.allowed
    assert decision.via == "handle"


# ---- resolve_person ----


def test_policy_people_is_consulted_before_acquaint(monkeypatch):
    calls = _fake_acquaint(monkeypatch, {"ok": True, "matches": [{"id": "ada-lovelace"}]})
    assert resolve_person("github:pat", _subject()) == "pat"
    assert calls == []


def test_acquaint_resolves_a_handle_with_exactly_one_match(monkeypatch):
    match = {
        "id": "ada-lovelace",
        "name": "Ada",
        "platform": "github",
        "value": "ada-lovelace",
        "evidence": "fixture",
        "status": "active",
    }
    calls = _fake_acquaint(monkeypatch, {"ok": True, "matches": [match]})
    assert resolve_person("github:ada-lovelace", _subject()) == "ada-lovelace"
    assert calls == ["github:ada-lovelace"]


@pytest.mark.parametrize(
    "result",
    [
        {"ok": False, "matches": []},
        {"ok": False, "matches": [{"id": "ada-lovelace"}]},
        {"ok": True, "matches": []},
        {"ok": True, "matches": [{"id": "ada-lovelace"}, {"id": "ada-lovelace"}]},
    ],
)
def test_acquaint_resolves_to_none_unless_ok_with_exactly_one_match(monkeypatch, result):
    _fake_acquaint(monkeypatch, result)
    assert resolve_person("github:ada-lovelace", _subject()) is None


def test_acquaint_raising_resolves_to_none(monkeypatch):
    _fake_acquaint(monkeypatch, error=RuntimeError("store unreadable"))
    assert resolve_person("github:ada-lovelace", _subject()) is None


def test_without_acquaint_an_unknown_handle_resolves_to_none():
    assert resolve_person("github:ada-lovelace", _subject()) is None


def test_authorize_uses_acquaint_through_the_default_resolver(monkeypatch):
    _fake_acquaint(monkeypatch, {"ok": True, "matches": [{"id": "pat"}]})
    decision = authorize(_message("pat"), _subject(people={}), "report")
    assert (decision.allowed, decision.person, decision.via) == (True, "pat", "handle")


# ---- addresses compare without regard to case (GitHub logins are case-insensitive) ----


def test_people_lookup_ignores_the_case_of_the_address():
    assert resolve_person("github:Pat", _subject()) == "pat"
    assert resolve_person("github:pat", _subject(people={"github:PAT": "pat"})) == "pat"
    decision = authorize(_message("PAT"), _subject(), "request_work")
    assert (decision.allowed, decision.person, decision.via) == (True, "pat", "handle")


def test_is_relay_compares_addresses_without_regard_to_case():
    assert is_relay("github:Example-Bot", _subject())
    assert is_relay("github:example-bot", _subject(relays=("github:EXAMPLE-BOT",)))
    assert not is_relay("github:pat", _subject())
    assert not is_relay(RELAY, _subject(relays=()))


def test_relay_lookup_ignores_the_case_of_the_address():
    decision = authorize(_message("Example-Bot", labels=("partner:pat",)), _subject(), "report")
    assert (decision.allowed, decision.person, decision.via) == (True, "pat", "relay-label")
    decision = authorize(
        _message("example-bot", labels=("partner:pat",)), _subject(relays=("github:EXAMPLE-BOT",)), "report"
    )
    assert (decision.allowed, decision.via) == (True, "relay-label")
