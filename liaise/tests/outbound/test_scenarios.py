"""The outbound scenario suite (liaise discussion 32 §7; research §9.3): verdicts, mutations, metrics.

Each of the twenty-two scenarios is evaluated end to end: the real detectors over the
text, then :func:`liaise.policy.evaluate` with the fixture audience, disclosure,
provenance and policy. S7 is the known miss: a paraphrase no deterministic detector
finds, marked as a strict expected failure that names its severity. Every deterministic
mutation of every scenario runs the same way. The last test computes the metrics of
research §9.2 and the summary hook prints them.
"""

from __future__ import annotations

import pytest

from liaise.policy import ROUTES, SEND, ROUTE_BLOCK
from liaise.tests.outbound import conftest
from liaise.tests.outbound.suite import EXTRAS, SCENARIOS, all_cases, metrics, mutations, prepare

KNOWN_MISS_SEVERITY = 4


def _mark(case, scenario):
    if scenario.get("known_miss"):
        reason = f"known miss (severity {scenario['severity']}): {scenario['known_miss']}"
        return pytest.param(case, id=case.id, marks=pytest.mark.xfail(reason=reason, strict=True))
    return pytest.param(case, id=case.id)


def _check(case) -> None:
    verdict = case.evaluate()
    scenario = case.scenario
    assert verdict.flow in case.expected, (
        f"{case.id}: {verdict.flow}, expected one of {sorted(case.expected)}; "
        f"reasons: {[reason.text for reason in verdict.reasons]}"
    )
    assert verdict.route == ROUTES[verdict.flow]
    if scenario.get("expects_finding"):
        assert scenario["expects_finding"] in {f.entity for f in verdict.findings}, (
            f"{case.id}: no finding names {scenario['expects_finding']}"
        )
    if scenario.get("no_findings"):
        assert not verdict.findings, [f.to_dict() for f in verdict.findings]
    if scenario.get("deliver"):
        assert verdict.route != ROUTE_BLOCK, f"{case.id} must deliver"


@pytest.mark.parametrize("case", [_mark(prepare(s), s) for s in SCENARIOS])
def test_scenario_gets_its_expected_verdict(case):
    _check(case)


@pytest.mark.parametrize(
    "case", [_mark(case, s) for s in SCENARIOS for case in mutations(s)]
)
def test_mutation_gets_its_expected_verdict(case):
    _check(case)


@pytest.mark.parametrize("case", [pytest.param(prepare(s), id=s["id"]) for s in EXTRAS])
def test_extra_gets_its_expected_verdict(case):
    _check(case)


def test_the_suite_has_the_twenty_two_scenarios():
    assert [s["id"] for s in SCENARIOS] == [f"S{n}" for n in range(1, 23)]
    assert sum(1 for s in SCENARIOS if s.get("known_miss")) == 1
    assert next(s for s in SCENARIOS if s.get("known_miss"))["id"] == "S7"


def test_every_scenario_has_the_mutations_the_design_names():
    for scenario in SCENARIOS:
        names = [case.id.partition(":")[2] for case in mutations(scenario)]
        assert {"swap-dm", "swap-public", "swap-shared", "cc-stranger", "bcc-bram", "quoted"} <= set(names), scenario["id"]
        if scenario.get("sensitive"):
            assert {"link-title", "attachment"} <= set(names), scenario["id"]
            if scenario["sensitive"] in ("Heron", "the bird project", "Cy", "Bram"):
                assert sum(n.startswith(("alias-", "homoglyph-")) for n in names) == 2, scenario["id"]


def test_send_fails_wherever_it_is_not_expected():
    """Where the expected verdict reads 'revise or approve', either passes and send fails."""
    for case in all_cases():
        if SEND not in case.expected and not case.scenario.get("known_miss"):
            assert case.evaluate().flow != SEND, case.id


def test_verdicts_are_deterministic():
    for scenario in SCENARIOS:
        case = prepare(scenario)
        first, second = case.evaluate(), case.evaluate()
        assert first == second
        assert first.to_dict() == second.to_dict()


def test_metrics():
    """Zero misses outside S7, zero false diverts, and S10 and S22 deliver."""
    report = metrics()
    conftest.REPORT.update(report)
    assert report["misses"] == ["S7"], report
    weights = sum(s["severity"] for s in SCENARIOS if SEND not in s["expected"])
    assert report["miss_rate"] == pytest.approx(KNOWN_MISS_SEVERITY / weights)
    assert report["false_divert_rate"] == 0.0
    assert report["send_scenarios"] >= 1
    assert report["utility_under_attack"] == {"S10": True, "S22": True}
