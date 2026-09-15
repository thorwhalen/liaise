"""Tests for liaise.policy: flows and routes, the least-cleared reader, the hashes, purity.

The scenario suite (``liaise/tests/outbound``) covers the rule table row by row; this
module pins the contracts around it: the flow order and the routes, the hash
constructions (the audience hash against correspond's own, the cross-package contract),
the ceiling rules of discussion §4.4, the record shapes, input validation, and that
``evaluate`` does no I/O, imports nothing from correspond or acquaint at module level,
and answers the same for the same inputs.
"""

from __future__ import annotations

import ast
import builtins
import io
import json
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from correspond.model import Audience

from liaise import policy
from liaise.detect import Finding
from liaise.gate import Outbound
from liaise.policy import (
    AXES,
    FLOWS,
    ROUTES,
    RULES,
    Facts,
    OutboundPolicy,
    Provenance,
    Reader,
    Rule,
    Verdict,
    above,
    audience_hash,
    audience_in_words,
    audience_record,
    evaluate,
    facts_of,
    flow_rank,
    least_cleared_reader,
    most_restrictive,
    payload_hash,
    payload_of,
)
from liaise.tests.outbound.fixtures import AT, TOKEN

NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
KEY = b"k" * 32
ADA_ADDRESS = "email:ada" + AT + "example.org"
BRAM_ADDRESS = "email:bram" + AT + "example.org"
PUBLIC_ISSUE = {
    "ref": "github:example/app#12",
    "scope": "public",
    "readers": [{"channel": "github", "native_id": "1", "handle": "octocat"}],
    "classes": ["watchers and participants receive the body by email"],
    "external": True,
    "durability": [
        "indexed",
        "archived_by_others",
        "copies_pushed",
        "edit_history_visible",
    ],
    "widening": ["forks", "visibility_flip"],
    "as_of": "2026-09-15T12:00:00Z",
    "evidence": ["gh api repos/example/app: visibility public"],
}
#: correspond's ``Audience.from_dict(PUBLIC_ISSUE).hash``, pinned: the cross-package contract.
PUBLIC_ISSUE_HASH = "55610b8e01f4478a986dae302e530f870ddf2c69ce3af86679ad36ab0bcf6195"
EMAIL_TO_ADA = {
    "ref": ADA_ADDRESS,
    "scope": "named",
    "readers": [ADA_ADDRESS],
    "complete": False,
    "external": True,
    "durability": ["copies_pushed"],
    "widening": ["forwarding"],
}
ORG_REPO = {"ref": "github:example/app-internal#3", "scope": "org", "complete": False}
OPERATOR = {
    "ref": "macos:notify",
    "scope": "operator",
    "complete": True,
    "retractable": True,
}
ADA = {"tier": "open", "clearance": "amber", "review_by": "2026-11-01", "lapsed": False}
BRAM = {"tier": "reviewed", "clearance": "clear"}
IDENTITIES = {ADA_ADDRESS: "ada", BRAM_ADDRESS: "bram"}
MESSAGE = {
    "ref": "github:example/app#12",
    "channel": "github",
    "recipient": "ada",
    "text": "The export fix is in.",
    "title": None,
    "cc": (),
    "bcc": (),
    "attachments": ("notes.pdf",),
    "case_id": "app-1",
}
#: ``payload_hash(MESSAGE)``, pinned: an approval made today must still bind tomorrow.
MESSAGE_HASH = "798092320181161a24fc79cd82cb2ce1633dd61ff6991874354833e748c76648"


def _finding(
    kind="vocabulary",
    *,
    entity="project:heron",
    label="amber",
    rule="term",
    start=4,
    end=9,
    sealed_from=(),
    severity=3,
):
    return Finding(
        kind=kind,
        start=start,
        end=end,
        entity=entity,
        label=label,
        sealed_from=tuple(sealed_from),
        rule=rule,
        severity=severity,
        fingerprint="f" * 64,
    )


def _evaluate(
    message=MESSAGE,
    *,
    audience=EMAIL_TO_ADA,
    disclosure=None,
    findings=(),
    provenance=False,
    **options,
):
    disclosure = {"people": {"ada": ADA}} if disclosure is None else disclosure
    options.setdefault("identities", IDENTITIES)
    return evaluate(
        message,
        audience=audience,
        disclosure=disclosure,
        findings=findings,
        provenance=provenance,
        now=NOW,
        **options,
    )


# ---- flows and routes ----


def test_flows_order_by_restriction_and_map_to_three_routes():
    assert FLOWS == ("send", "delay", "revise", "approve", "approve_twice", "refuse")
    assert [ROUTES[f] for f in FLOWS] == [
        "send",
        "send",
        "draft",
        "draft",
        "draft",
        "block",
    ]
    assert most_restrictive(["delay", "refuse", "send"]) == "refuse"
    assert most_restrictive([]) == "send"
    assert flow_rank("approve_twice") == 4
    with pytest.raises(ValueError, match="not one of"):
        flow_rank("maybe")


def test_approve_twice_is_reserved_and_never_produced():
    assert all(rule.flow != "approve_twice" for rule in RULES)
    with pytest.raises(ValueError):
        Rule("x", lambda facts: (), "maybe")


def test_the_table_has_the_fourteen_rows_in_the_designs_order():
    assert [rule.name for rule in RULES] == [
        "secrets",
        "seals",
        "exfiltration",
        "personal, public",
        "no write-down",
        "co-ownership",
        "personal, private",
        "tier",
        "stranger",
        "disclosure stance",
        "taint",
        "reply mode",
        "irreversibility",
        "unknown audience",
    ]
    assert [rule.flow for rule in RULES] == [
        "refuse",
        "refuse",
        "refuse",
        "refuse",
        "revise",
        "revise",
        "approve",
        "approve",
        "approve",
        "approve",
        "approve",
        "approve",
        "delay",
        "send",
    ]


def test_above_compares_labels_and_treats_no_ceiling_as_never_above():
    assert above("amber", "clear") and above("red", "amber")
    assert not above("amber", "amber") and not above("green", "amber")
    assert not above("red", None) and not above(None, "clear")


# ---- purity ----


def test_the_module_imports_nothing_from_correspond_or_acquaint():
    tree = ast.parse(Path(policy.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not [
        name for name in imported if name.split(".")[0] in ("correspond", "acquaint")
    ], imported
    for name, value in vars(policy).items():
        module = getattr(value, "__module__", "") or ""
        assert not module.startswith(("correspond", "acquaint")), name


def test_evaluate_does_no_io(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("evaluate touched the outside world")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(io, "open", refuse)
    monkeypatch.setattr(Path, "open", refuse)
    monkeypatch.setattr(Path, "read_text", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    verdict = _evaluate(findings=[_finding()], audience=PUBLIC_ISSUE, provenance=None)
    assert verdict.flow == "refuse"  # taint (unknown) with a leak
    assert verdict.as_of == NOW.isoformat()


def test_the_same_inputs_give_the_same_verdict():
    first = _evaluate(findings=[_finding()], audience=PUBLIC_ISSUE)
    second = _evaluate(findings=[_finding()], audience=PUBLIC_ISSUE)
    assert first == second and first.to_dict() == second.to_dict()
    json.dumps(first.to_dict())  # JSON-ready
    later = evaluate(
        MESSAGE,
        audience=PUBLIC_ISSUE,
        disclosure={"people": {"ada": ADA}},
        findings=[_finding()],
        provenance=False,
        now=NOW + timedelta(days=1),
        identities=IDENTITIES,
    )
    assert later.to_dict() == {
        **first.to_dict(),
        "as_of": (NOW + timedelta(days=1)).isoformat(),
    }


# ---- hashes ----


def test_payload_hash_is_pinned_and_covers_what_an_approval_must_bind_to():
    assert payload_hash(MESSAGE) == MESSAGE_HASH
    assert payload_of(MESSAGE) == {
        "recipients": ["ada"],
        "cc": [],
        "bcc": [],
        "ref": "github:example/app#12",
        "title": None,
        "text": "The export fix is in.",
        "attachments": ["notes.pdf"],
    }
    for change in (
        {"text": "The export fix is in!"},
        {"title": "A title"},
        {"cc": ["someone"]},
        {"bcc": ["someone"]},
        {"recipient": "bram"},
        {"ref": "github:example/app#13"},
        {"attachments": ("other.pdf",)},
    ):
        assert payload_hash({**MESSAGE, **change}) != MESSAGE_HASH, change
    assert (
        payload_hash({**MESSAGE, "channel": "email", "case_id": None, "purpose": "ask"})
        == MESSAGE_HASH
    )


def test_payload_hash_reads_the_gates_outbound_record():
    outbound = Outbound(
        ref="github:example/app#12",
        channel="github",
        recipient="ada",
        purpose="reply",
        text="The export fix is in.",
        case_id="app-1",
    )
    assert payload_of(outbound)["attachments"] == []
    assert payload_hash(outbound) == payload_hash({**MESSAGE, "attachments": ()})
    assert payload_of(
        {
            "ref": "x",
            "recipient": "ada",
            "text": "t",
            "attachments": [{"name": "a.pdf"}, {"filename": "b.pdf"}],
        }
    )["attachments"] == ["a.pdf", "b.pdf"]


def test_payload_hash_uses_canonical_json_with_ascii_escaping():
    text = "Héron — en route"
    expected = policy._digest(payload_of({**MESSAGE, "text": text}))
    assert payload_hash({**MESSAGE, "text": text}) == expected
    assert (
        policy.canonical_json({"b": 1, "a": text})
        == '{"a":"H\\u00e9ron \\u2014 en route","b":1}'
    )


def test_audience_hash_matches_correspond_for_the_pinned_record():
    assert audience_hash(PUBLIC_ISSUE) == PUBLIC_ISSUE_HASH
    assert Audience.from_dict(PUBLIC_ISSUE).hash == PUBLIC_ISSUE_HASH
    record = Audience.from_dict(PUBLIC_ISSUE)
    assert audience_hash(record) == PUBLIC_ISSUE_HASH  # an object with to_dict
    assert audience_hash(record.to_dict()) == PUBLIC_ISSUE_HASH


def test_audience_hash_ignores_as_of_evidence_and_listing_order():
    shuffled = {
        **PUBLIC_ISSUE,
        "as_of": "2030-01-01T00:00:00Z",
        "evidence": [],
        "durability": list(reversed(PUBLIC_ISSUE["durability"])),
        "widening": ["visibility_flip", "forks", "forks"],
    }
    assert audience_hash(shuffled) == PUBLIC_ISSUE_HASH
    assert audience_hash({**PUBLIC_ISSUE, "external": None}) != PUBLIC_ISSUE_HASH


def test_audience_hash_matches_correspond_for_every_scope_and_for_unknown():
    for record in (
        Audience(
            ref=ADA_ADDRESS,
            scope="named",
            readers=[],
            complete=False,
            external=True,
            as_of="2026-09-15T12:00:00Z",
        ),
        Audience(
            ref="github:example/app-internal#3",
            scope="org",
            complete=False,
            classes=["b", "a"],
            as_of="2026-09-15T12:00:00Z",
        ),
        Audience(
            ref="macos:notify",
            scope="operator",
            complete=True,
            retractable=True,
            external=False,
            as_of="2026-09-15T12:00:00Z",
        ),
        Audience.unknown("discord:planned", "a channel not built"),
    ):
        assert audience_hash(record.to_dict()) == record.hash, record.scope
    assert audience_hash(None) == Audience.unknown("").hash
    assert audience_hash({"ref": "discord:x"}) == Audience.unknown("discord:x").hash


def test_audience_record_normalises_readers_as_correspond_does():
    record = audience_record(
        {
            **EMAIL_TO_ADA,
            "readers": [
                ADA_ADDRESS,
                {"channel": "email", "native_id": "x", "handle": "x"},
            ],
        }
    )
    assert [r["address"] for r in record["readers"]] == sorted(
        r["address"] for r in record["readers"]
    )
    assert set(record["readers"][0]) == {
        "channel",
        "native_id",
        "handle",
        "display_name",
        "is_bot",
        "is_self",
        "authority",
        "address",
    }
    with pytest.raises(TypeError):
        audience_record(42)
    with pytest.raises(TypeError):
        audience_record({"scope": "named", "classes": "one string"})


def test_a_defaulted_audience_is_public_whatever_it_says():
    record = audience_record(
        {"ref": "x", "scope": "named", "defaulted": True, "retractable": True}
    )
    assert (
        record["scope"] == "public"
        and not record["complete"]
        and not record["retractable"]
    )
    assert "unknown resolves to public" in record["evidence"]
    assert audience_in_words(record).startswith("world-readable (assumed")
    assert audience_in_words(PUBLIC_ISSUE) == (
        "world-readable; emailed to watchers and participants; archived by others; "
        "edits keep a visible history; not retractable"
    )
    assert (
        audience_in_words(PUBLIC_ISSUE) == Audience.from_dict(PUBLIC_ISSUE).in_words()
    )


# ---- the least-cleared reader ----


def test_the_operator_has_no_ceiling_but_its_explicit_recipients():
    assert least_cleared_reader(OPERATOR, {}) == Reader(None, "the operator")
    to_ada = least_cleared_reader(
        OPERATOR, {"people": {"ada": ADA}}, recipients=["ada"]
    )
    assert to_ada == Reader("amber", "ada", "ada")
    to_nobody = least_cleared_reader(OPERATOR, {}, recipients=["nobody"])
    assert to_nobody.clearance == "clear"


def test_public_and_defaulted_audiences_are_clear():
    assert (
        least_cleared_reader(PUBLIC_ISSUE, {"people": {"ada": ADA}}).clearance
        == "clear"
    )
    reader = least_cleared_reader({"ref": "discord:x"}, {"people": {"ada": ADA}})
    assert reader.clearance == "clear" and "could not be determined" in reader.who


def test_a_named_audience_is_the_minimum_over_its_resolved_readers():
    disclosure = {"people": {"ada": ADA, "bram": BRAM}}
    reader = least_cleared_reader(
        EMAIL_TO_ADA,
        {"people": {"ada": ADA}},
        identities=IDENTITIES,
        recipients=["ada"],
    )
    assert reader == Reader("amber", "ada", "ada")
    both = least_cleared_reader(
        EMAIL_TO_ADA, disclosure, identities=IDENTITIES, recipients=["ada"]
    )
    assert both == Reader("clear", "bram", "bram")


def test_an_unresolved_identity_or_recipient_is_clear():
    unresolved = least_cleared_reader(
        EMAIL_TO_ADA, {"people": {"ada": ADA}}, recipients=["ada"]
    )
    assert (
        unresolved.clearance == "clear"
        and unresolved.who == f"{ADA_ADDRESS}, who has no record"
    )
    stranger = least_cleared_reader(
        {**EMAIL_TO_ADA, "readers": []},
        {"people": {"ada": ADA}},
        identities=IDENTITIES,
        recipients=["ada", "nobody"],
    )
    assert stranger == Reader("clear", "nobody, who has no record")
    resolved_but_unknown = least_cleared_reader(
        {**EMAIL_TO_ADA, "readers": []},
        {"people": {"ada": ADA}},
        identities={"x": "cy"},
        recipients=["ada", "x"],
    )
    assert resolved_but_unknown == Reader("clear", "cy", "cy")


def test_a_listed_reader_counts_only_when_identities_resolved_it():
    """The disclosure may have resolved a listed reader itself, but it cannot say which
    of its people the address is, and a gap is not written for every unreadable reader,
    so an address nobody tied to a person is at clear."""
    saw = {
        "people": {"ada": ADA},
        "audience": {
            "ref": ADA_ADDRESS,
            "scope": "named",
            "complete": False,
            "ceiling": "clear",
        },
        "gaps": {},
    }
    assert (
        least_cleared_reader(EMAIL_TO_ADA, saw, recipients=["ada"]).clearance == "clear"
    )
    resolved = least_cleared_reader(
        EMAIL_TO_ADA, saw, identities=IDENTITIES, recipients=["ada"]
    )
    assert resolved == Reader("amber", "ada", "ada")
    assert least_cleared_reader(
        EMAIL_TO_ADA,
        {**saw, "least_clearance": "clear"},
        identities=IDENTITIES,
        recipients=["ada"],
    ) == Reader("amber", "ada", "ada")


def test_the_disclosures_least_clearance_is_a_floor_except_for_a_named_audience_it_saw():
    """acquaint puts a clear class on every incomplete audience; §4.4 gives named
    audiences none, and no email audience is ever complete."""
    disclosure = {"people": {"ada": ADA}, "least_clearance": "clear"}
    assert (
        least_cleared_reader(
            EMAIL_TO_ADA, disclosure, identities=IDENTITIES, recipients=["ada"]
        ).clearance
        == "clear"
    )
    seen = {
        **disclosure,
        "audience": {"ref": ADA_ADDRESS, "scope": "named", "complete": False},
    }
    assert (
        least_cleared_reader(
            EMAIL_TO_ADA, seen, identities=IDENTITIES, recipients=["ada"]
        ).clearance
        == "amber"
    )
    org_seen = {
        **disclosure,
        "audience": {"ref": ORG_REPO["ref"], "scope": "org", "complete": False},
    }
    assert (
        least_cleared_reader(
            {**ORG_REPO, "complete": True}, org_seen, recipients=["ada"]
        ).clearance
        == "clear"
    )
    assert (
        least_cleared_reader(
            EMAIL_TO_ADA,
            {**seen, "least_clearance": "Amber"},
            identities=IDENTITIES,
            recipients=["ada"],
        ).clearance
        == "amber"
    )


def test_an_incomplete_org_audience_takes_the_recorded_clearance_else_clear():
    disclosure = {"people": {"ada": ADA}}
    assert (
        least_cleared_reader(ORG_REPO, disclosure, recipients=["ada"]).clearance
        == "clear"
    )
    recorded = {
        **disclosure,
        "audience": {
            "ref": ORG_REPO["ref"],
            "scope": "org",
            "complete": False,
            "organisation": "org:example",
            "ceiling": "amber",
        },
    }
    reader = least_cleared_reader(ORG_REPO, recorded, recipients=["ada"])
    assert reader.clearance == "amber"  # Ada and the organisation's members tie
    members = least_cleared_reader(ORG_REPO, {**recorded, "people": {}})
    assert members.clearance == "amber" and "org:example" in members.who
    complete = least_cleared_reader(
        {**ORG_REPO, "complete": True}, disclosure, recipients=["ada"]
    )
    assert complete == Reader("amber", "ada", "ada")
    listed = least_cleared_reader(
        {**ORG_REPO, "complete": True, "readers": ["github:x"]},
        disclosure,
        recipients=["ada"],
    )
    assert listed.clearance == "clear" and listed.who == "github:x, who has no record"


# ---- the verdict record ----


def test_the_verdict_carries_the_axes_the_reasons_and_the_hashes():
    verdict = _evaluate(findings=[_finding()], audience=PUBLIC_ISSUE, provenance=True)
    assert isinstance(verdict, Verdict)
    record = verdict.to_dict()
    assert list(record["axes"]) == list(AXES)
    assert record["axes"] == {
        "audience": "public",
        "sensitivity": 3,
        "relationship": "open",
        "irreversible": True,
        "tainted": True,
    }
    assert record["flow"] == "refuse" and record["route"] == "block"
    assert [r["rule"] for r in record["reasons"]] == [
        "taint",
        "no write-down",
        "irreversibility",
    ]
    assert [r["flow"] for r in record["reasons"]] == ["refuse", "revise", "delay"]
    assert record["reasons"][0]["finding"] == _finding().to_dict()
    assert record["least_cleared"]["clearance"] == "clear"
    assert record["payload_hash"] == MESSAGE_HASH
    assert record["audience_hash"] == PUBLIC_ISSUE_HASH
    assert record["as_of"] == NOW.isoformat() and record["mode"] == "enforce"
    assert record["findings"] == [_finding().to_dict()]
    assert verdict.rules == ("taint", "no write-down", "irreversibility")


def test_reasons_never_hold_a_secret():
    secret = _finding(
        "secret",
        entity=None,
        label=None,
        rule="github-pat",
        start=10,
        end=50,
        severity=5,
    )
    verdict = _evaluate(
        {**MESSAGE, "text": "The token " + TOKEN + " is here."}, findings=[secret]
    )
    text = json.dumps(verdict.to_dict())
    assert TOKEN not in text and TOKEN[4:] not in text
    assert verdict.flow == "refuse"
    assert "at characters 10–50" in verdict.reasons[0].text


def test_the_relationship_axis_is_the_most_restrictive_recipient():
    disclosure = {"people": {"ada": ADA, "bram": BRAM}}
    assert _evaluate(disclosure=disclosure).axes["relationship"] == "open"
    with_bram = _evaluate({**MESSAGE, "cc": [BRAM_ADDRESS]}, disclosure=disclosure)
    assert with_bram.axes["relationship"] == "reviewed" and with_bram.flow == "approve"
    with_stranger = _evaluate({**MESSAGE, "cc": ["nobody"]}, disclosure=disclosure)
    assert with_stranger.axes["relationship"] == "stranger"


def test_axes_when_nothing_is_found_and_provenance_is_unknown():
    verdict = _evaluate(audience=OPERATOR, provenance=None)
    assert verdict.axes == {
        "audience": "operator",
        "sensitivity": 0,
        "relationship": "open",
        "irreversible": False,
        "tainted": None,
    }
    assert verdict.flow == "send"


# ---- rows the suite reaches only one way ----


def test_revise_needs_a_case_to_resume_to():
    finding = _finding()
    assert _evaluate(findings=[finding], audience=PUBLIC_ISSUE).flow == "revise"
    assert (
        _evaluate(
            {**MESSAGE, "case_id": None}, findings=[finding], audience=PUBLIC_ISSUE
        ).flow
        == "approve"
    )
    assert (
        _evaluate(
            findings=[finding], audience=PUBLIC_ISSUE, policy={"resumable": False}
        ).flow
        == "approve"
    )
    assert (
        _evaluate(
            {**MESSAGE, "case_id": None},
            findings=[finding],
            audience=PUBLIC_ISSUE,
            policy={"resumable": True},
        ).flow
        == "revise"
    )


def test_a_seal_recorded_in_the_disclosure_counts_without_the_finding_saying_so():
    disclosure = {
        "people": {"ada": ADA, "bram": BRAM},
        "seals": [{"entity": "project:heron", "from": "bram"}],
    }
    verdict = _evaluate(
        {**MESSAGE, "bcc": [BRAM_ADDRESS]}, findings=[_finding()], disclosure=disclosure
    )
    assert (
        verdict.flow == "refuse"
        and verdict.reasons[0].rule == "seals"
        and verdict.reasons[0].reader == "bram"
    )
    unread = _evaluate(findings=[_finding()], disclosure=disclosure)
    assert unread.rules == (
        "seals",
        "no write-down",
    )  # in the disclosure's people, Bram is a reader
    without_bram = _evaluate(
        findings=[_finding()], disclosure={**disclosure, "people": {"ada": ADA}}
    )
    assert without_bram.flow == "send"  # Ada is cleared for amber; nobody sealed reads


def test_a_lapsed_tier_is_read_from_the_clock_as_well_as_the_record():
    finding = _finding()
    later = evaluate(
        MESSAGE,
        audience=EMAIL_TO_ADA,
        disclosure={"people": {"ada": ADA}},
        findings=[],
        provenance=False,
        now=datetime(2026, 11, 5, tzinfo=timezone.utc),
        identities=IDENTITIES,
    )
    assert later.rules == ("tier",) and "past its review date" in later.reasons[0].text
    assert _evaluate(findings=[finding]).flow == "send"
    unreadable = _evaluate(disclosure={"people": {"ada": {**ADA, "review_by": "soon"}}})
    assert unreadable.rules == ("tier",)
    reviewed = _evaluate(
        disclosure={"people": {"ada": {**ADA, "review": ["affiliation ended"]}}}
    )
    assert (
        reviewed.rules == ("tier",)
        and "awaits the operator's review" in reviewed.reasons[0].text
    )


def test_the_taint_waiver_and_the_taint_escalation():
    tainted = Provenance.tainted_by("read an issue by an unknown author")
    assert _evaluate(provenance=tainted).flow == "approve"
    assert _evaluate(provenance=tainted, policy={"tainted_runs": "send"}).flow == "send"
    leaked = _evaluate(provenance=tainted, findings=[_finding()], audience=PUBLIC_ISSUE)
    assert (
        leaked.flow == "refuse"
        and "never released as written" in leaked.reasons[0].text
    )
    assert "read an issue by an unknown author" in leaked.reasons[0].text
    waived = _evaluate(
        provenance=tainted,
        findings=[_finding()],
        audience=PUBLIC_ISSUE,
        policy={"tainted_runs": "send"},
    )
    assert waived.flow == "refuse" and waived.rules[0] == "taint"  # never waived
    assert _evaluate(provenance=None, audience=OPERATOR).flow == "send"


def test_exfiltration_on_a_named_audience_needs_readers_not_known_to_be_internal():
    image = _finding(
        "exfiltration", entity=None, label=None, rule="image-host", severity=4
    )
    link = _finding(
        "exfiltration", entity=None, label=None, rule="link-host", severity=4
    )
    assert (
        _evaluate(findings=[image], audience={**EMAIL_TO_ADA, "external": True}).flow
        == "refuse"
    )
    assert (
        _evaluate(findings=[image], audience={**EMAIL_TO_ADA, "external": None}).flow
        == "refuse"
    )  # unknown resolves to the wider reading
    assert (
        _evaluate(findings=[image], audience={**EMAIL_TO_ADA, "external": False}).flow
        == "send"
    )
    assert (
        _evaluate(findings=[link], audience={**EMAIL_TO_ADA, "external": True}).flow
        == "approve"
    )
    assert _evaluate(findings=[link], audience=PUBLIC_ISSUE).rules == (
        "exfiltration",
        "irreversibility",
    )


def test_personal_findings_split_by_scope():
    personal = _finding(
        "personal", entity=None, label=None, rule="email-address", severity=2
    )
    assert _evaluate(findings=[personal]).flow == "approve"
    assert _evaluate(findings=[personal], audience=PUBLIC_ISSUE).flow == "refuse"
    assert _evaluate(findings=[personal], audience=OPERATOR).flow == "send"


def test_co_ownership_names_the_reader_who_is_not_cleared():
    cy = _finding("third_party", entity="person:cy", label="red", severity=3)
    verdict = _evaluate(findings=[cy])
    assert (
        verdict.flow == "revise"
        and verdict.reasons[0].rule == "co-ownership"
        and verdict.reasons[0].reader == "ada"
    )
    assert "ada is cleared to amber" in verdict.reasons[0].text
    public = _evaluate(findings=[cy], audience=PUBLIC_ISSUE)
    assert public.rules == ("co-ownership", "irreversibility")


def test_reply_mode_and_disclosure_stance():
    assert _evaluate(policy=OutboundPolicy(reply_mode="draft")).rules == ("reply mode",)
    assert _evaluate(policy={"ai_tolerance": "averse"}).rules == ("disclosure stance",)
    assert (
        _evaluate(
            policy={"ai_tolerance": "averse", "disclosure_decision": "disclosed"}
        ).flow
        == "send"
    )
    assert _evaluate(policy={"ai_tolerance": "neutral"}).flow == "send"


# ---- inputs ----


def test_findings_may_come_as_dicts():
    as_dict = _finding().to_dict()
    assert _evaluate(findings=[as_dict], audience=PUBLIC_ISSUE) == _evaluate(
        findings=[_finding()], audience=PUBLIC_ISSUE
    )
    with pytest.raises(TypeError, match="Finding"):
        _evaluate(findings=["not a finding"])


def test_provenance_and_policy_accept_their_dicts():
    assert Provenance.of(None) == Provenance.unknown()
    assert Provenance.of(True).tainted is True
    assert Provenance.of({"tainted": False, "evidence": ["x"]}) == Provenance.clean("x")
    assert Provenance.clean().to_dict() == {"tainted": False, "evidence": []}
    with pytest.raises(TypeError):
        Provenance.of("tainted")
    with pytest.raises(TypeError):
        Provenance.of({"tainted": "yes"})
    assert OutboundPolicy.of(None) == OutboundPolicy()
    assert OutboundPolicy.of({"mode": "shadow"}).mode == "shadow"
    assert OutboundPolicy().to_dict()["tainted_runs"] == "approve"
    with pytest.raises(ValueError, match="unknown policy keys"):
        OutboundPolicy.of({"tainted": True})
    with pytest.raises(ValueError):
        OutboundPolicy(tainted_runs="maybe")
    with pytest.raises(ValueError):
        OutboundPolicy(mode="dry")
    with pytest.raises(TypeError):
        OutboundPolicy.of("shadow")


def test_bad_inputs_are_refused_with_a_reason():
    with pytest.raises(TypeError, match="datetime"):
        evaluate(
            MESSAGE,
            audience=EMAIL_TO_ADA,
            disclosure={},
            findings=(),
            provenance=False,
            now="today",
        )
    with pytest.raises(TypeError, match="text must be a str"):
        _evaluate({**MESSAGE, "text": None})
    with pytest.raises(TypeError, match="collection of strings"):
        _evaluate({**MESSAGE, "cc": "one string"})
    with pytest.raises(TypeError, match="disclosure"):
        _evaluate(disclosure="ada")
    assert _evaluate(disclosure=None, audience=OPERATOR).flow == "send"


def test_the_shadow_mode_is_carried_not_applied():
    verdict = _evaluate(
        findings=[_finding()], audience=PUBLIC_ISSUE, policy={"mode": "shadow"}
    )
    assert verdict.mode == "shadow" and verdict.flow == "revise"


def test_facts_of_exposes_what_the_rules_read():
    facts = facts_of(
        MESSAGE,
        audience=PUBLIC_ISSUE,
        disclosure={"people": {"ada": ADA}},
        findings=[_finding()],
        provenance=False,
        now=NOW,
    )
    assert isinstance(facts, Facts)
    assert facts.scope == "public" and facts.resumable and facts.case_id == "app-1"
    assert facts.recipients == (("ada", "ada"),)
    assert facts.leaks == (_finding(),)
    assert facts.of_kind("secret") == ()


def test_a_fresh_interpreter_can_import_the_policy_without_acquaint():
    code = (
        "import sys; sys.modules['acquaint'] = None\n"
        "import liaise.policy as p\n"
        "print('acquaint' in sys.modules and sys.modules['acquaint'] is not None)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


# ---- what the adversarial review found ----


def test_an_unknown_label_ranks_above_red():
    assert above("Amber", "clear") and above("mystery", "red")
    assert not above(None, "clear")
    verdict = _evaluate(findings=[_finding(label="Amber")], audience=PUBLIC_ISSUE)
    assert verdict.rules[0] == "no write-down"


def test_a_nameless_attachment_still_changes_the_payload_hash():
    unnamed = {**MESSAGE, "attachments": ({"name": None, "sha256": "ab" * 32},)}
    assert payload_hash(unnamed) != payload_hash({**MESSAGE, "attachments": ()})
    assert payload_of(unnamed)["attachments"] == [{"name": None, "sha256": "ab" * 32}]
    digest = {**MESSAGE, "attachments": ({"name": "notes.pdf", "sha256": "ab" * 32},)}
    assert payload_hash(digest) != MESSAGE_HASH  # the bytes changed under the name
    reordered = {**MESSAGE, "cc": ["b", "a"], "attachments": ("y.pdf", "x.pdf")}
    assert payload_hash(reordered) == payload_hash(
        {**MESSAGE, "cc": ["a", "b"], "attachments": ("x.pdf", "y.pdf")}
    )


def test_audience_booleans_must_be_literal():
    for field in ("complete", "retractable", "defaulted"):
        with pytest.raises(TypeError, match=field):
            audience_record({**ORG_REPO, field: "false"})
    with pytest.raises(TypeError, match="external"):
        audience_record({**ORG_REPO, "external": "no"})


def test_a_readers_address_is_derived_never_trusted():
    mallory = {
        "channel": "github",
        "native_id": "9",
        "handle": "mallory",
        "address": "github:ada-lorne",
    }
    record = audience_record({**ORG_REPO, "readers": [mallory]})
    assert record["readers"][0]["address"] == "github:mallory"
    assert (
        audience_hash({**ORG_REPO, "readers": [mallory]})
        == Audience.from_dict({**ORG_REPO, "readers": [mallory]}).hash
    )
    flags = {
        "channel": "github",
        "native_id": "1",
        "handle": "x",
        "is_self": 0,
        "is_bot": 1,
    }
    assert (
        audience_hash({**ORG_REPO, "readers": [flags]})
        == Audience.from_dict({**ORG_REPO, "readers": [flags]}).hash
    )
    reader = least_cleared_reader(
        {**ORG_REPO, "complete": True, "readers": [mallory]},
        {"people": {"ada": ADA}},
        identities={"github:ada-lorne": "ada"},
        recipients=["ada"],
    )
    assert (
        reader.clearance == "clear"
        and reader.who == "github:mallory, who has no record"
    )
    with pytest.raises(TypeError, match="channel identity"):
        audience_record({**ORG_REPO, "readers": [7]})


def test_sealed_from_given_as_a_string_still_seals():
    finding = {**_finding().to_dict(), "sealed_from": "bram"}
    verdict = _evaluate(
        findings=[finding], disclosure={"people": {"ada": ADA, "bram": BRAM}}
    )
    assert verdict.flow == "refuse" and verdict.reasons[0].rule == "seals"


def test_a_one_shot_iterator_of_findings_is_refused():
    with pytest.raises(TypeError, match="one-shot iterator"):
        _evaluate(findings=iter([_finding()]))
    assert _evaluate(findings=(_finding(),), audience=PUBLIC_ISSUE).flow == "revise"


def test_recipient_spellings_resolve_to_the_same_person():
    disclosure = {"people": {"ada": ADA, "bram": BRAM}}
    for spelling in ("bram", " bram ", "Bram", "person:bram", BRAM_ADDRESS):
        verdict = _evaluate({**MESSAGE, "recipient": spelling}, disclosure=disclosure)
        assert verdict.axes["relationship"] == "reviewed", spelling
        assert "stranger" not in verdict.rules, spelling


def test_the_rules_keyword_is_private_and_bound_to_the_table():
    with pytest.raises(TypeError):
        evaluate(
            MESSAGE,
            audience=EMAIL_TO_ADA,
            disclosure={},
            findings=(),
            provenance=False,
            now=NOW,
            rules=(),
        )
    with pytest.raises(ValueError, match="not rules of the table"):
        evaluate(
            MESSAGE,
            audience=EMAIL_TO_ADA,
            disclosure={},
            findings=(),
            provenance=False,
            now=NOW,
            _rules=(Rule("x", lambda f: (), "send"),),
        )


def test_delay_routes_to_draft_until_the_outbox_exists():
    verdict = _evaluate(audience=PUBLIC_ISSUE)
    assert verdict.flow == "delay" and verdict.route == "draft"
    with_outbox = _evaluate(audience=PUBLIC_ISSUE, policy={"outbox": True})
    assert with_outbox.flow == "delay" and with_outbox.route == "send"


def test_the_verdict_records_the_audience_snapshot_and_the_readers_consulted():
    record = _evaluate(
        audience=PUBLIC_ISSUE, disclosure={"people": {"ada": ADA, "bram": BRAM}}
    ).to_dict()
    assert record["audience"] == audience_record(PUBLIC_ISSUE)
    assert record["readers"] == {
        "ada": {
            "tier": "open",
            "clearance": "amber",
            "lapsed": False,
            "review_by": "2026-11-01",
        },
        "bram": {
            "tier": "reviewed",
            "clearance": "clear",
            "lapsed": None,
            "review_by": None,
        },
    }
    json.dumps(record)


def test_a_naive_now_is_read_as_utc():
    verdict = evaluate(
        MESSAGE,
        audience=EMAIL_TO_ADA,
        disclosure={"people": {"ada": ADA}},
        findings=(),
        provenance=False,
        now=datetime(2026, 9, 15, 12),
        identities=IDENTITIES,
    )
    assert verdict.as_of == NOW.isoformat()


def test_verdicts_do_not_depend_on_the_disclosures_key_order():
    cy = _finding("third_party", entity="person:cy", label="red", severity=3)
    one = _evaluate(findings=[cy], disclosure={"people": {"ada": ADA, "bram": BRAM}})
    other = _evaluate(findings=[cy], disclosure={"people": {"bram": BRAM, "ada": ADA}})
    assert one == other and one.to_dict() == other.to_dict()
