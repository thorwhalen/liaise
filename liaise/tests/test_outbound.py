"""Tests for liaise.outbound: what the gate's policy filter gathers, and what a send records in acquaint.

The provenance of a case's run, the disclosure with and without acquaint, the leak terms as
vocabulary, the readers' identities, the findings of each part of a message, and the
``interaction`` entries a sent message appends to its recipients' acquaint records. Every
person and project is invented; acquaint is unimportable unless a test installs a fake.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone

import pytest

from liaise.gate import Outbound
from liaise.model import Case, LedgerEntry
from liaise.outbound import (
    FROM_ACQUAINT,
    LEAK_TERMS_ENTITY,
    LEAK_TERMS_LABEL,
    acquaint_disclosure,
    audience_snapshot,
    case_provenance,
    disclosed_records,
    findings_in,
    identities_for,
    judge,
    need_to_know_disclosure,
    record_disclosure,
    verdict_id,
    with_leak_terms,
)
from liaise.policy import Provenance
from liaise.subjects import Policy, Subject

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
KEY = b"k" * 32
REF = "github:example/app#12"
NAMED = {"ref": REF, "scope": "named", "readers": [{"channel": "github", "native_id": "pat", "handle": "pat"}]}
HERON = {"term": "Heron", "entity": "project:heron", "label": "amber", "sealed_from": []}


@pytest.fixture(autouse=True)
def no_real_acquaint(monkeypatch):
    monkeypatch.setitem(sys.modules, "acquaint", None)


def _subject(**policy) -> Subject:
    fields = dict(
        people={"github:pat": "pat"},
        roles={"pat": "partner", "obi": "observer"},
        relays=("github:example-bot",),
    )
    fields.update(policy)
    return Subject("example-app", ("github:example/app",), Policy(**fields))


def _message(actor, grade="platform", **detail) -> LedgerEntry:
    return LedgerEntry(at=NOW, kind="message", actor=actor, grade=grade, detail=detail)


def _case(*entries) -> Case:
    return Case(
        id="example-app-1",
        subject="example-app",
        conversations=(REF,),
        reporter="pat",
        state="working",
        created_at=NOW,
        updated_at=NOW,
        entries=entries,
    )


def _outbound(text="Heron slips to October.", **fields) -> Outbound:
    values = dict(ref=REF, channel="github", recipient="pat", purpose="reply", text=text, case_id="example-app-1")
    values.update(fields)
    return Outbound(**values)


def _install_acquaint(monkeypatch, **functions) -> None:
    module = types.ModuleType("acquaint")
    for name, function in functions.items():
        setattr(module, name, function)
    monkeypatch.setitem(sys.modules, "acquaint", module)


# ---- provenance ----


def test_a_case_whose_messages_come_from_people_trusted_with_request_work_is_clean():
    provenance = case_provenance(_case(_message("pat"), _message("liaise", role="self")), _subject())
    assert provenance.tainted is False
    assert provenance.evidence == ("every message on example-app-1 is from someone trusted with request_work",)


@pytest.mark.parametrize(
    "entry, why",
    [
        (_message("obi"), "a message from obi, whose role observer does not grant request_work"),
        (_message("pat", grade="claimed"), "a message from pat at grade claimed, which request_work does not accept"),
        (_message(None), "a message from an unattributed sender, who has no role on example-app"),
        (_message("github:example-bot", role="relay"), "a message from github:example-bot, who has no role on example-app"),
    ],
    ids=["a role without request_work", "a grade request_work does not accept", "nobody", "a relay's own words"],
)
def test_a_message_the_subject_does_not_trust_for_request_work_taints_the_run(entry, why):
    provenance = case_provenance(_case(_message("pat"), entry), _subject())
    assert (provenance.tainted, provenance.evidence) == (True, (why,))


def test_each_reason_to_distrust_is_given_once_and_other_entries_do_not_count():
    note = LedgerEntry(at=NOW, kind="note", actor="obi", text="not something a run read as a message")
    provenance = case_provenance(_case(_message("obi"), _message("obi"), note), _subject())
    assert len(provenance.evidence) == 1
    assert case_provenance(_case(note), _subject()).tainted is False


# ---- the disclosure ----


def test_without_acquaint_every_named_reader_is_need_to_know():
    answer = acquaint_disclosure(["pat", "email:cy", "pat"], audience=NAMED, today="2026-09-15")
    assert answer == need_to_know_disclosure(["pat", "email:cy"], today="2026-09-15")
    assert list(answer["people"]) == ["pat", "email:cy"]
    assert (answer["least_clearance"], answer["vocabulary"]) == ("clear", [])


def test_acquaint_is_asked_with_the_audience_as_json_and_its_answer_is_marked(monkeypatch):
    asked = []

    def disclosure(people, *, projects=None, audience=None, today=None):
        asked.append((people, projects, json.loads(audience), today))
        return {"people": {}, "least_clearance": "clear", "vocabulary": [HERON]}

    _install_acquaint(monkeypatch, disclosure=disclosure)
    answer = acquaint_disclosure(("pat",), projects=(), audience=NAMED, today="2026-09-15")

    assert asked == [(["pat"], None, NAMED, "2026-09-15")]
    assert (answer["source"], answer["vocabulary"]) == (FROM_ACQUAINT, [HERON])


def test_leak_terms_join_the_vocabulary_at_the_most_restrictive_label_and_blank_ones_do_not():
    answer = with_leak_terms({"vocabulary": [HERON]}, ["example-internal", "  "])
    assert answer["vocabulary"] == [
        HERON,
        {"term": "example-internal", "entity": LEAK_TERMS_ENTITY, "label": LEAK_TERMS_LABEL, "sealed_from": []},
    ]
    assert LEAK_TERMS_LABEL == "red"


# ---- readers and findings ----


def test_copies_and_listed_readers_are_resolved_and_a_failing_resolver_resolves_nobody():
    def resolver(address, subject):
        if address == "email:broken":
            raise RuntimeError("the people store is locked")
        return {"github:pat": "pat", "email:cy": "cy"}.get(address)

    snapshot = audience_snapshot(NAMED, REF)
    outbound = _outbound(cc=("email:cy", "email:broken"), bcc=("email:nobody",))

    identities = identities_for(outbound, snapshot, subject=_subject(), resolver=resolver)

    assert identities == {"email:cy": "cy", "email:broken": None, "email:nobody": None, "github:pat": "pat"}


def test_findings_in_the_title_and_attachment_names_name_their_part():
    disclosure = {"vocabulary": [HERON]}
    outbound = _outbound("Heron is late.", title="About Heron", attachments=("heron-plan.pdf", "notes.txt"))

    found = findings_in(outbound, disclosure, subject=_subject(), key=KEY)

    assert sorted((f.part or "", f.start) for f in found if f.kind == "vocabulary") == [
        ("", 0),  # the text
        ("attachment name", 0),
        ("title", 6),
    ]


def test_an_unknown_audience_is_the_public_one_of_the_messages_destination():
    snapshot = audience_snapshot(None, REF)
    assert (snapshot["ref"], snapshot["scope"], snapshot["defaulted"]) == (REF, "public", True)


# ---- after a send ----


def _judged(disclosure_answer, **fields):
    outbound = _outbound(**fields)
    judgement = judge(
        outbound,
        subject=_subject(),
        now=NOW,
        audience=NAMED,
        provenance=Provenance.clean("a trusted case"),
        key=KEY,
        disclosure=lambda people, **_: disclosure_answer,
        resolver=lambda address, subject: {"email:cy": "cy"}.get(address),
    )
    return outbound, judgement


def test_a_sent_message_tells_each_recipient_which_labelled_records_it_identified_never_its_text(monkeypatch):
    remembered = []

    def remember(entity, text, *, source=None, kind="observation", disclosed=None):
        remembered.append((entity, text, source, kind, disclosed))
        return {"ok": True}

    _install_acquaint(monkeypatch, remember=remember)
    answer = {"people": {"pat": {"tier": "open", "clearance": "red"}}, "least_clearance": "red", "vocabulary": [HERON]}
    outbound, judgement = _judged(with_leak_terms(answer, ["October"]), cc=("email:cy", "email:nobody"))

    assert disclosed_records(judgement.verdict) == ("project:heron",)  # never the leak terms' entity
    assert record_disclosure(outbound, judgement.verdict, judgement.consulted) is None
    assert remembered == [
        (person, f"liaise sent a reply on {REF}", "liaise", "interaction", ["project:heron"]) for person in ("pat", "cy")
    ]
    assert not any("slips" in str(entry) for entry in remembered)


def test_nothing_is_recorded_when_nothing_labelled_was_identified_or_acquaint_does_not_import(monkeypatch):
    outbound, judgement = _judged({"vocabulary": [HERON]}, text="The export is fixed.")
    assert disclosed_records(judgement.verdict) == () and record_disclosure(outbound, judgement.verdict, {}) is None
    outbound, judgement = _judged({"vocabulary": [HERON]})
    assert record_disclosure(outbound, judgement.verdict, judgement.consulted) is None  # acquaint is unimportable


def test_a_record_that_cannot_be_written_is_reported_never_raised(monkeypatch):
    def remember(entity, text, **kwargs):
        raise LookupError(f"no entity matches {entity!r}")

    _install_acquaint(monkeypatch, remember=remember)
    outbound, judgement = _judged({"vocabulary": [HERON]})

    failure = record_disclosure(outbound, judgement.verdict, judgement.consulted)

    assert failure == "pat: LookupError: no entity matches 'pat'"


def test_a_verdict_is_named_by_its_content():
    _, first = _judged({"vocabulary": [HERON]})
    _, again = _judged({"vocabulary": [HERON]})
    _, other = _judged({"vocabulary": [HERON]}, text="Heron slips to November.")
    assert verdict_id(first.verdict) == verdict_id(again.verdict) != verdict_id(other.verdict)
    assert len(verdict_id(first.verdict)) == 64
