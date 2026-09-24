"""A mutation check per rule of the policy table (liaise discussion 32 §7).

For each row, the table is run with that row removed, and with that row weakened (asking
for one flow less than it does), and at least one scenario or extra must then get a less
restrictive verdict than the full table gives it. The row *unknown audience* sets no flow;
its check is that its reason appears for a defaulted audience and disappears with the row.
"""

from __future__ import annotations

import pytest

from liaise.policy import RULE_NAMES, RULES, SEND, flow_rank
from liaise.tests.outbound.suite import (
    BY_ID,
    EXTRAS,
    SCENARIOS,
    all_cases,
    prepare,
    weakening,
    without,
)

FLOWED_RULES = [name for name in RULE_NAMES if name != "unknown audience"]
#: The scenario each row's removal must loosen, so the check names what it relies on.
WITNESSES = {
    "secrets": ("S8", "X-canary"),
    "seals": ("S6", "S12"),
    "exfiltration": ("S21",),
    "personal, public": ("X-personal-public",),
    "no write-down": ("S2",),
    "co-ownership": ("S3", "S17"),
    "personal, private": ("X-personal-private",),
    "tier": ("S5", "S15"),
    "stranger": ("S16",),
    "disclosure stance": ("X-disclosure-stance",),
    "taint": ("S9", "X-taint"),
    "reply mode": ("X-reply-mode",),
    "irreversibility": ("X-irreversibility",),
}


def _loosened(name: str, cases) -> list[tuple[str, str, str]]:
    """``(case id, full flow, loosened flow)`` for the cases the mutated table loosens."""
    loosened = []
    for case in cases:
        full = case.evaluate().flow
        mutated = case.evaluate(rules=without(name)).flow
        if flow_rank(mutated) < flow_rank(full):
            loosened.append((case.id, full, mutated))
    return loosened


@pytest.mark.parametrize("name", FLOWED_RULES)
def test_removing_the_rule_loosens_a_scenario(name):
    cases = [prepare(s) for s in (*SCENARIOS, *EXTRAS)]
    loosened = _loosened(name, cases)
    assert loosened, (
        f"no scenario gets a less restrictive verdict without the rule {name!r}"
    )
    witnesses = {case_id for case_id, _, _ in loosened}
    assert set(WITNESSES[name]) <= witnesses, (name, sorted(witnesses))


@pytest.mark.parametrize("name", FLOWED_RULES)
def test_weakening_the_rule_loosens_a_scenario(name):
    loosened = []
    for scenario in (*SCENARIOS, *EXTRAS):
        case = prepare(scenario)
        full = case.evaluate().flow
        mutated = case.evaluate(rules=weakening(name)).flow
        if flow_rank(mutated) < flow_rank(full):
            loosened.append((case.id, full, mutated))
    assert loosened, (
        f"no scenario gets a less restrictive verdict with the rule {name!r} weakened"
    )


@pytest.mark.parametrize("name", FLOWED_RULES)
def test_removing_the_rule_never_tightens(name):
    for case in all_cases():
        full = case.evaluate().flow
        mutated = case.evaluate(rules=without(name)).flow
        assert flow_rank(mutated) <= flow_rank(full), case.id


def test_the_unknown_audience_row_explains_a_defaulted_audience():
    case = prepare(BY_ID["S13"])
    with_row = case.evaluate()
    assert "unknown audience" in with_row.rules
    assert any("assumed public" in reason.text for reason in with_row.reasons)
    without_row = case.evaluate(rules=without("unknown audience"))
    assert "unknown audience" not in without_row.rules
    assert without_row.flow == with_row.flow, "the row sets no flow of its own"
    assert with_row.least_cleared.clearance == "clear"
    named = prepare(BY_ID["S1"]).evaluate()
    assert "unknown audience" not in named.rules


def test_every_row_has_a_check():
    assert set(WITNESSES) | {"unknown audience"} == set(RULE_NAMES)
    assert len(RULES) == 14


def test_the_taint_escalation_is_its_own_mutation():
    """S9 is refused by taint alone: weakened or removed, the stranger row's approve is left."""
    s9 = prepare(BY_ID["S9"])
    assert s9.evaluate().flow == "refuse"
    weakened = s9.evaluate(rules=weakening("taint"))
    assert flow_rank(weakened.flow) < flow_rank("refuse")
    assert s9.evaluate(rules=without("taint")).flow == "approve"
    x_taint = prepare(BY_ID["X-taint"])
    assert x_taint.evaluate(rules=without("taint")).flow == SEND


def test_the_exfiltration_row_treats_a_plain_link_as_approve():
    s21, s22 = prepare(BY_ID["S21"]), prepare(BY_ID["S22"])
    assert s21.evaluate().flow == "refuse"
    assert s22.evaluate().flow == "approve"
    assert s22.evaluate(rules=without("exfiltration")).rules == (
        "taint",
        "irreversibility",
    )


def test_without_the_link_term_detector_a_term_glued_into_a_link_path_is_approve_again():
    """Mutation check of liaise #46: the detector alone moves X-link-path off ``approve``."""
    from liaise.detect import DFLT_DETECTORS, detect_link_terms

    case = prepare(BY_ID["X-link-path"])
    less = [d for d in DFLT_DETECTORS if d is not detect_link_terms]

    assert case.evaluate().flow == "refuse"
    assert case.evaluate(detectors=less).flow == "approve"
    assert prepare(BY_ID["S22"]).evaluate().flow == "approve"  # a plain link still delivers
