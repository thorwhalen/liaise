"""Tests for liaise.model: the vocabularies, JSON round trips, and the pure case helpers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from correspond.model import Grade

from liaise.model import (
    CASE_STATES,
    ENTRY_KINDS,
    HOLD_MODES,
    OUTCOME_KINDS,
    PERMISSIONS,
    Case,
    Health,
    Hold,
    LedgerEntry,
    Outcome,
    RunRecord,
    RunResult,
)

T0 =datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
REF = "github:example/app#12"


def _message(**overrides) -> LedgerEntry:
    fields = dict(
        at=T0,
        kind="message",
        actor="pat",
        grade=Grade.PLATFORM,
        permission="report",
        delivery_id="d1",
        text="The export drops the last row.",
        detail={"labels": ["partner:pat"]},
    )
    fields.update(overrides)
    return LedgerEntry(**fields)


def _case(**overrides) -> Case:
    fields = dict(
        id="pat-1",
        subject="pat",
        conversations=(REF,),
        reporter="pat",
        state="intake",
        created_at=T0,
        updated_at=T0,
    )
    fields.update(overrides)
    return Case(**fields)


# ---- vocabularies ----


def test_case_states_are_the_label_vocabulary_with_one_home():
    assert CASE_STATES == (
        "intake",
        "paused",
        "working",
        "needs-partner",
        "needs-owner",
        "deployed",
        "budget",
    )


def test_closed_vocabularies():
    assert OUTCOME_KINDS == (
        "ask",
        "reply",
        "escalate",
        "propose",
        "deliver",
        "decline",
        "defer",
        "note",
    )
    assert PERMISSIONS == ("report", "request_work", "approve_candidate")
    assert HOLD_MODES == ("block", "drain", "cancel")
    assert ENTRY_KINDS == (
        "message",
        "transition",
        "outcome",
        "gate",
        "run",
        "hold",
        "projection",
        "note",
    )


# ---- to_dict / from_dict ----

RECORDS = [
    _message(),
    LedgerEntry(at=T0, kind="transition"),
    _case(),
    _case(
        session_id="s1",
        entries=(_message(), LedgerEntry(at=T0, kind="transition")),
        drafts=({"text": "Try it now.", "reason": "draft mode"},),
        defer_until=T0 + timedelta(minutes=30),
    ),
    Outcome(kind="note"),
    Outcome(
        kind="ask",
        text="Which browser?",
        questions=("Chrome or Safari? (default: Chrome)",),
        reason="needs detail",
    ),
    Hold(scope="global", mode="drain"),
    Hold(
        scope="repo:example/app",
        mode="block",
        reason="maintenance",
        set_by="operator",
        set_at=T0,
    ),
    Health(ok=True),
    Health(ok=False, defer_until=T0 + timedelta(hours=1), error="rate limited"),
    RunRecord(
        run_id="r2",
        case_id="pat-1",
        subject="pat",
        mode="resume",
        status="finished",
        started_at=T0,
    ),
    RunRecord(
        run_id="r1",
        case_id="pat-1",
        subject="pat",
        mode="fresh",
        status="running",
        started_at=T0,
        pid=4242,
        heartbeat_at=T0 + timedelta(minutes=1),
        ended_at=T0 + timedelta(minutes=5),
        session_id="s1",
        stream_path="r1.jsonl",
    ),
    RunResult(run_id="r2"),
    RunResult(
        run_id="r1",
        outcomes=(Outcome(kind="reply", text="Fixed."), Outcome(kind="note")),
        usage={"input_tokens": 10, "output_tokens": 5},
        cost_usd=0.25,
        rate_limit={"status": "allowed"},
        session_id="s1",
        summary="Fixed the export.",
    ),
]


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: type(r).__name__)
def test_round_trips_through_json(record):
    data = json.loads(json.dumps(record.to_dict()))
    assert type(record).from_dict(data) == record


def test_to_dict_writes_iso_datetimes_lists_and_plain_grades():
    data = _case(entries=(_message(),)).to_dict()
    assert data["created_at"] == "2026-01-01T12:00:00+00:00"
    assert data["conversations"] == [REF]
    assert isinstance(data["entries"], list)
    assert type(data["entries"][0]["grade"]) is str
    assert data["entries"][0]["grade"] == "platform"


def test_from_dict_rebuilds_tuples_records_and_datetimes():
    case = Case.from_dict(_case(entries=(_message(),)).to_dict())
    assert isinstance(case.conversations, tuple)
    assert isinstance(case.entries, tuple)
    assert isinstance(case.entries[0], LedgerEntry)
    assert case.entries[0].at == T0
    result = RunResult.from_dict(RunResult(run_id="r1", outcomes=(Outcome(kind="note"),)).to_dict())
    assert isinstance(result.outcomes[0], Outcome)


def test_case_defer_until_defaults_to_none_and_round_trips_as_iso():
    assert _case().defer_until is None
    later = T0 + timedelta(minutes=10)
    data = _case(defer_until=later).to_dict()
    assert data["defer_until"] == "2026-01-01T12:10:00+00:00"
    assert Case.from_dict(data).defer_until == later


def test_case_written_before_defer_until_existed_still_reads():
    data = _case().to_dict()
    del data["defer_until"]
    assert Case.from_dict(data).defer_until is None


def test_from_dict_does_not_share_mutable_values_with_its_input():
    data = _message().to_dict()
    entry = LedgerEntry.from_dict(data)
    data["detail"]["labels"].append("bug")
    assert entry.detail == {"labels": ["partner:pat"]}


def test_from_dict_names_the_missing_required_fields():
    with pytest.raises(ValueError, match="without id, subject, reporter"):
        Case.from_dict({"conversations": []})


def test_from_dict_ignores_keys_it_has_no_field_for():
    data = {**Health(ok=True).to_dict(), "added_by_a_later_version": 1}
    assert Health.from_dict(data) == Health(ok=True)


# ---- validation ----


def test_unknown_case_state_is_refused():
    with pytest.raises(ValueError, match="case state 'needs_owner'.*needs-owner"):
        _case(state="needs_owner")


def test_unknown_entry_kind_is_refused():
    with pytest.raises(ValueError, match="ledger entry kind 'comment'"):
        LedgerEntry(at=T0, kind="comment")


def test_unknown_hold_mode_is_refused():
    with pytest.raises(ValueError, match="block, drain, cancel"):
        Hold(scope="global", mode="pause")


# ---- pure case helpers ----


def test_with_entry_appends_and_moves_updated_at_forward():
    case = _case()
    later = _message(at=T0 + timedelta(minutes=5))
    updated = case.with_entry(later)
    assert updated.entries == (later,)
    assert updated.updated_at == T0 + timedelta(minutes=5)
    assert case.entries == ()  # pure: the original is unchanged


def test_with_entry_never_moves_updated_at_back():
    case = _case(updated_at=T0 + timedelta(hours=1))
    assert case.with_entry(_message(at=T0)).updated_at == T0 + timedelta(hours=1)


def test_with_state_changes_the_state_and_records_the_transition():
    at = T0 + timedelta(minutes=3)
    case = _case().with_state("working", at=at, actor="liaise", reason="quiet window")
    assert case.state == "working"
    (entry,) = case.entries
    assert (entry.kind, entry.at, entry.actor) == ("transition", at, "liaise")
    assert entry.detail == {"from": "intake", "to": "working", "reason": "quiet window"}
    assert case.updated_at == at


def test_with_state_refuses_an_unknown_state():
    with pytest.raises(ValueError, match="case state 'done'"):
        _case().with_state("done", at=T0)
