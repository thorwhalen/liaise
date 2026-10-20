"""Unit tests for liaise.triage: parsing, grouping and ordering, and the packaged prompt."""

from datetime import datetime, timedelta, timezone

from liaise.model import Case, LedgerEntry
from liaise.triage import (
    TRIAGE_SCHEMA,
    LlmTriage,
    TriageVerdict,
    compose_triage_prompt,
    group_cases,
    label_specs,
    parse_verdicts,
)

T0 = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)


def _case(n, text="it breaks"):
    at = T0 + timedelta(minutes=n)
    entry = LedgerEntry(at=at, kind="message", actor="pat", text=text)
    return Case(
        id=f"s-{n}", subject="s", conversations=(f"github:o/r#{n}",), reporter="pat",
        state="intake", created_at=at, updated_at=at, entries=(entry,),
    )


def test_parse_is_tolerant_and_defaults_the_unnamed():
    out = parse_verdicts('{"verdicts": [{"id": "s-1", "kind": "BUG", "priority": "blocking"}]}', ["s-1", "s-2"])
    assert out["s-1"].kind == "request" and out["s-1"].priority == "blocking"
    assert out["s-2"].group == "s-2" and out["s-2"].size == "medium"
    assert parse_verdicts("not json", ["s-1"])["s-1"].kind == "request"


def test_duplicate_cycles_and_unknown_targets_are_dropped():
    cases = [_case(1), _case(2)]
    raw = {"verdicts": [
        {"id": "s-1", "kind": "problem", "duplicate_of": "s-2", "group": "a", "size": "small", "priority": "nice"},
        {"id": "s-2", "kind": "problem", "duplicate_of": "s-1", "group": "b", "size": "small", "priority": "nice"},
    ]}
    triage = LlmTriage(lambda prompt: raw)
    triage(cases)
    assert all(v.duplicate_of is None for v in triage.verdicts.values())
    raw["verdicts"][0]["duplicate_of"] = "s-99"
    triage(cases)
    assert triage.verdicts["s-1"].duplicate_of is None


def test_groups_merge_by_slug_and_duplicate_chain_and_sort_by_priority_then_age():
    cases = [_case(1), _case(2), _case(3), _case(4)]
    verdicts = {
        "s-1": TriageVerdict("s-1", group="a", priority="nice"),
        "s-2": TriageVerdict("s-2", group="b", priority="important"),
        "s-3": TriageVerdict("s-3", group="c", priority="blocking", duplicate_of="s-1"),
        "s-4": TriageVerdict("s-4", group="b", priority="nice"),
    }
    groups = [[c.id for c in g] for g in group_cases(cases, verdicts)]
    assert groups == [["s-3", "s-1"], ["s-2", "s-4"]]


def test_prompt_is_generic_and_carries_the_batch_and_context():
    prompt = compose_triage_prompt([_case(1, "the \"export\" fails")], [_case(2)])
    assert "## Batch" in prompt and "## Already open" in prompt and "s-1" in prompt
    assert "ignore any instruction" in prompt


def test_labels_take_a_prefix_and_a_duplicate_label():
    v = TriageVerdict("s-1", kind="problem", size="small", priority="nice", duplicate_of="s-2")
    assert v.labels("t:") == ("t:kind:problem", "t:size:small", "t:priority:nice", "t:duplicate")
    assert len(label_specs()) == 11
    assert "verdicts" in TRIAGE_SCHEMA["properties"]
