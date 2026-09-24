"""Tests for ``liaise gate report`` (liaise #39): counts from the ledger's gate entries, never content.

The ledgers here are built from invented entries, shaped as the gate records them, plus one
real tick on fake channels. Nothing leaves the process.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from liaise.gate import OUTBOX_ACTOR
from liaise.ledger import Ledger
from liaise.model import LedgerEntry
from liaise.report import (
    MAX_FALSE_DIVERT_RATE,
    MIN_SHADOW_MESSAGES,
    enforce_recommended,
    gate_report,
    report_lines,
)

SUBJECT = "example-studio"
T0 = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
SECRET_TEXT = "The staging password is hunter2-example."
FINGERPRINT = "fp-0123456789abcdef"


def _concern(rule, flow="approve"):
    return {
        "filter": "outbound_policy",
        "flow": flow,
        "rule": rule,
        "text": f"{rule} fired",
        "findings": [{"kind": "secret", "fingerprint": FINGERPRINT, "start": 4, "end": 12}],
    }


def _judged(minutes, *, text, rules=(), decision=None, mode="enforce", error=None, legacy=None, severity=None):
    """A first judgement; ``legacy`` is 0.1's counterfactual flow, when the gate recorded one."""
    flow = "approve" if rules else "send"
    decision = decision or ("divert" if rules else "send")
    detail = {
        "decision": decision,
        "flow": flow,
        "concerns": [_concern(rule) for rule in rules],
        "settled": [],
        "verdict": {"flow": flow, "mode": mode},
        "approval": None,
        "approval_bound": None,
    }
    if error:
        detail["error"] = error
    if legacy is not None:
        detail["counterfactual"] = {"version": "0.1", "flow": legacy, "reasons": []}
    if severity is not None:
        for concern in detail["concerns"]:
            concern["findings"][0]["severity"] = severity
    return LedgerEntry(at=T0 + timedelta(minutes=minutes), kind="gate", actor="liaise", text=text, detail=detail)


def _released(minutes, *, text, rules, edited=False, by="operator", bound=True):
    detail = {
        "decision": "send",
        "flow": "send",
        "concerns": [],
        "settled": [_concern(rule) for rule in rules],
        "verdict": {"flow": "approve", "mode": "enforce"},
        "approval": {"by": by, "rules_overridden": list(rules)},
        "approval_bound": bound,
        "edited": edited,
    }
    return LedgerEntry(at=T0 + timedelta(minutes=minutes), kind="gate", actor=by, text=text, detail=detail)


def _rejected(minutes, *, text):
    return LedgerEntry(
        at=T0 + timedelta(minutes=minutes),
        kind="gate",
        actor="operator",
        text=text,
        detail={"decision": "reject", "reason": "not this"},
    )


def _ledger(*cases):
    ledger = Ledger({})
    for number, entries in enumerate(cases, start=1):
        case = ledger.new_case(SUBJECT, f"github:example/app#{number}", reporter="pat", at=T0)
        for entry in entries:
            ledger.append(case.id, entry)
    return ledger


@pytest.fixture
def ledger():
    return _ledger(
        [  # sent as judged, then a held secret the operator confirmed by rejecting it
            _judged(0, text="All good."),
            _judged(1, text=SECRET_TEXT, rules=("secret",)),
            _rejected(2, text=SECRET_TEXT),
        ],
        [  # a link held, released unedited: a false positive, so a false divert
            _judged(3, text="See the docs link.", rules=("exfiltration",)),
            _released(4, text="See the docs link.", rules=("exfiltration",)),
        ],
        [  # held for two rules, released after an edit: overrides, not a false divert
            _judged(5, text="Draft one.", rules=("exfiltration", "reply mode")),
            _released(6, text="Draft one, edited.", rules=("exfiltration", "reply mode"), edited=True),
        ],
        [  # the outbox's own release is not an operator override
            _judged(7, text="Public reply.", rules=("irreversibility",), decision="hold"),
            _released(8, text="Public reply.", rules=("irreversibility",), by=OUTBOX_ACTOR),
        ],
    )


def test_the_report_counts_judgements_releases_and_rejections(ledger):
    report = gate_report(ledger, subject=SUBJECT)

    assert report["counts"] == {
        "judged": 5,
        "sent as judged": 2,  # one as judged, one delay the outbox let through
        "held": 3,
        "released": 2,
        "false diverts": 1,
        "rejected": 1,
        "shadow": 0,
        "shadow compared": 0,
    }
    assert report["override_rate"] == pytest.approx(2 / 5)
    assert report["false_divert_rate"] == pytest.approx(1 / 3)  # 1 false divert of 3 that should send


def test_per_rule_precision_is_confirmed_over_confirmed_plus_released(ledger):
    rules = gate_report(ledger)["rules"]

    assert rules["secret"] == {"fired": 1, "released": 0, "confirmed": 1, "precision": 1.0}
    # the edited release is not a false positive: the operator fixed what the rule found
    assert rules["exfiltration"] == {"fired": 2, "released": 1, "confirmed": 0, "precision": 0.0}
    assert rules["reply mode"]["released"] == 0
    assert rules["irreversibility"] == {"fired": 1, "released": 0, "confirmed": 0, "precision": None}


def test_since_and_subject_narrow_what_is_counted(ledger):
    assert gate_report(ledger, since=T0 + timedelta(minutes=5))["counts"]["judged"] == 2
    assert gate_report(ledger, subject="another-subject")["counts"]["judged"] == 0
    assert gate_report(ledger.store, since=(T0 + timedelta(minutes=5)).isoformat())["counts"]["released"] == 1


def test_since_takes_a_date_and_refuses_what_is_not_a_time(ledger):
    assert gate_report(ledger, since="2026-09-21")["counts"]["judged"] == 0
    with pytest.raises(ValueError, match="not a time"):
        gate_report(ledger, since="last week")


def test_releasing_a_draft_no_rule_held_back_is_not_an_override():
    failed_then_released = _released(1, text="x", rules=())  # a failed send's draft, sent
    failed_then_released.detail["held_for"] = "send failed: GitHub answered 502"
    ledger = _ledger([_judged(0, text="x", error="GitHub answered 502"), failed_then_released])

    report = gate_report(ledger)

    assert report["counts"]["released"] == 0 and report["counts"]["false diverts"] == 0
    assert report["counts"]["judged"] == 1 and report["false_divert_rate"] is None


def test_a_draft_released_through_the_gate_again_is_not_a_second_judgement():
    again = _judged(1, text="x")
    again.detail["held_for"] = "the operator released it"
    report = gate_report(_ledger([_judged(0, text="x", rules=("secret",)), again]))

    assert report["counts"]["judged"] == 1


def test_shadow_counts_the_recorded_mode_and_the_subject_only_without_one():
    enforce_era = _judged(0, text="a")
    no_verdict = _judged(1, text="b")
    no_verdict.detail["verdict"] = None
    ledger = _ledger([enforce_era, no_verdict])

    assert gate_report(ledger, shadow_subjects=[SUBJECT])["counts"]["shadow"] == 1


def test_a_rejected_lapsed_delay_confirms_no_rule():
    held = _judged(0, text="Public reply.", rules=("irreversibility",), decision="hold")
    held.detail["flow"] = "delay"
    ledger = _ledger([held, _rejected(1, text="Public reply.")])
    assert gate_report(ledger)["rules"]["irreversibility"]["confirmed"] == 0


def test_rule_names_print_only_plain_characters():
    ledger = _ledger([_judged(0, text="x", rules=("odd\nname<script>",))])

    assert list(gate_report(ledger)["rules"]) == ["odd?name?script?"]


def test_an_approval_that_did_not_bind_or_a_failed_send_is_not_a_release():
    ledger = _ledger(
        [
            _judged(0, text="x", rules=("secret",)),
            _released(1, text="x", rules=("secret",), bound=False),
            _judged(2, text="y", error="GitHub answered 502"),
        ]
    )
    counts = gate_report(ledger)["counts"]

    assert counts["released"] == 0 and counts["sent as judged"] == 0 and counts["judged"] == 2


def test_the_report_never_prints_text_values_or_fingerprints(ledger):
    printed = "\n".join(report_lines(gate_report(ledger, subject=SUBJECT)))

    assert SECRET_TEXT not in printed and "hunter2" not in printed and FINGERPRINT not in printed
    assert "See the docs" not in printed and "Draft one" not in printed
    assert "per rule" in printed and "exfiltration" in printed
    assert printed.splitlines()[-1].startswith("enforce recommended: no (")


def test_shadow_entries_without_a_counterfactual_are_counted_but_not_compared():
    entries = [_judged(i, text=f"m{i}", mode="shadow") for i in range(MIN_SHADOW_MESSAGES)]
    report = gate_report(_ledger(entries))

    assert report["counts"]["shadow"] == MIN_SHADOW_MESSAGES and report["counts"]["shadow compared"] == 0
    assert report["shadow_agreement"] is None and report["missed_high_severity"] is None
    assert not report["enforce"]["recommended"]
    assert "fewer than" in report["enforce"]["reason"]
    assert "shadow agreement: n/a" in "\n".join(report_lines(report))
    assert gate_report(_ledger(entries[:1]), shadow_subjects=[SUBJECT])["counts"]["shadow"] == 1


def test_shadow_agreement_is_the_share_in_the_same_flow_class_as_0_1():
    entries = [
        _judged(0, text="a", mode="shadow", legacy="send"),  # both send: agree
        _judged(1, text="b", rules=("secrets",), mode="shadow", legacy="approve"),  # both held: agree
        _judged(2, text="c", rules=("irreversibility",), mode="shadow", legacy="send", severity=2),  # disagree
        _judged(3, text="d", rules=("audience",), mode="shadow", legacy="send", severity=4),  # a missed finding
        _judged(4, text="e", mode="enforce", legacy="approve"),  # not shadow: not compared
        _judged(5, text="f", mode="shadow"),  # before the counterfactual: shadow, not compared
    ]
    report = gate_report(_ledger(entries))

    assert report["counts"]["shadow"] == 5 and report["counts"]["shadow compared"] == 4
    assert report["shadow_agreement"] == 0.5 and report["missed_high_severity"] == 1
    assert report["shadow_note"] is None
    printed = "\n".join(report_lines(report))
    assert "shadow agreement: 0.500 (same flow class as 0.1, of 4 compared)" in printed
    assert "missed findings (0.1 would have sent, severity 4 or above): 1" in printed


def test_the_rollout_line_uses_the_compared_shadow_messages():
    clean = [_judged(i, text=f"m{i}", mode="shadow", legacy="send") for i in range(MIN_SHADOW_MESSAGES)]
    assert gate_report(_ledger(clean))["enforce"]["recommended"] is True
    missed = clean + [_judged(99, text="x", rules=("secrets",), mode="shadow", legacy="send", severity=5)]
    enforce = gate_report(_ledger(missed))["enforce"]
    assert enforce["recommended"] is False and "1 missed finding" in enforce["reason"]
    few = gate_report(_ledger(clean[:-1] + [_judged(98, text="y", mode="shadow")]))["enforce"]
    assert few["reason"].startswith(f"only {MIN_SHADOW_MESSAGES - 1} shadow messages compared")


@pytest.mark.parametrize(
    "shadow, missed, rate, recommended, why",
    [
        (MIN_SHADOW_MESSAGES, 0, MAX_FALSE_DIVERT_RATE, True, "shadow messages"),
        (MIN_SHADOW_MESSAGES - 1, 0, 0.0, False, "fewer than"),
        (MIN_SHADOW_MESSAGES, 1, 0.0, False, "missed finding"),
        (MIN_SHADOW_MESSAGES, 0, 0.11, False, "false-divert rate 0.11"),
        (MIN_SHADOW_MESSAGES, 0, None, False, "unknown"),
        (MIN_SHADOW_MESSAGES, None, 0.0, False, "not observable"),
    ],
)
def test_the_rollout_rule_of_decision_11(shadow, missed, rate, recommended, why):
    verdict = enforce_recommended(shadow_messages=shadow, missed_high_severity=missed, false_divert_rate=rate)

    assert verdict["recommended"] is recommended and why in verdict["reason"]


def test_the_gate_report_command_prints_the_report(tmp_path, ledger):
    from liaise import cli

    root = tmp_path / "config"
    root.mkdir()
    (root / "config.toml").write_text(f'owner_login = "owner"\nstate_dir = "{(tmp_path / "state").as_posix()}"\n')

    out = cli.gate_report(root=str(root), store=ledger.store)

    assert out.startswith("gate report for every subject") and "judged: 5" in out
    with pytest.raises(Exception, match="no subject 'typo'"):
        cli.gate_report(subject="typo", root=str(root), store=ledger.store)
    assert "enforce recommended: no" in out and SECRET_TEXT not in out


def test_a_real_tick_and_outbox_release_are_counted_without_the_outbox_as_an_override(world):
    from dataclasses import replace

    from liaise.subjects import RECOMMENDED_DELAY_MINUTES
    from liaise.tests.test_outbox import DUE, HELD_AT
    from liaise.tests.test_tick import REPO

    world.subject = replace(world.subject, policy=replace(world.subject.policy, delay_minutes=RECOMMENDED_DELAY_MINUTES))
    world.github.set_visibility(REPO, "public")
    world.issue()
    world.tick()
    world.tick(HELD_AT)
    world.tick(DUE)
    assert world.github.sent  # the held reply went out through the outbox

    report = gate_report(world.store)

    assert report["counts"]["released"] == 0 and report["counts"]["judged"] >= 1
    assert report["rules"]["irreversibility"]["released"] == 0


def test_a_shadow_subject_still_holds_what_the_policy_holds_and_records_what_0_1_would_have_done(world):
    """Acceptance (#39, as decided on #51): shadow mode enforces, and measures against 0.1.

    A canary term is something 0.1 never looked for: it would have sent this reply. The
    policy refuses it, shadow mode or not, and the gate entry records both.
    """
    from dataclasses import replace

    from liaise.tests.test_tick import CASE_1, LATER
    from liaise.model import Outcome, RunResult
    from liaise.processor import EchoProcessor

    canary = "violet-anchor-example"
    policy = replace(world.subject.policy, mode="shadow", canary_terms=(canary,))
    world.subject = replace(world.subject, policy=policy)
    reply = Outcome(kind="reply", text=f"The export is fixed; see {canary} for the notes.")
    world.processor = EchoProcessor(results={CASE_1: RunResult(run_id="", outcomes=(reply,))})
    world.issue()
    world.tick()
    world.tick(LATER)

    assert world.github.sent == []  # nothing sends that the policy holds back
    (gate,) = [e for e in world.case().entries if e.kind == "gate"]
    assert gate.detail["verdict"]["mode"] == "shadow" and gate.detail["flow"] == "refuse"
    assert gate.detail["counterfactual"] == {"version": "0.1", "flow": "send", "reasons": []}
    report = gate_report(world.store, shadow_subjects=[world.subject.slug])
    assert report["counts"]["shadow compared"] == 1
    assert report["shadow_agreement"] == 0.0 and report["missed_high_severity"] == 1


from liaise.tests.test_tick import fixed_run_suffix, no_real_acquaint, world  # noqa: E402,F401


# ---- hook overrides (liaise #37) ----


def test_hook_overrides_are_counted_apart_and_filtered_by_subject_and_time():
    from liaise import report
    from liaise.ledger import Ledger

    ledger = Ledger({})
    write = {"ref": "github:example/app#12", "subject": "example-studio", "flow": "approve", "rules": ["taint"]}
    ledger.add_override("t1", {"ran_at": "2026-09-20T10:00:00+00:00", "writes": [write], "unread": 0})
    ledger.add_override("t2", {"ran_at": "2026-09-21T10:00:00+00:00", "writes": [write, {**write, "rules": ["taint", "irreversibility"]}], "unread": 1})
    ledger.add_override("t3", {"ran_at": "not a time", "writes": [{**write, "subject": "other"}], "unread": 0})
    found = report.gate_report(ledger)
    assert found["hook"] == {"overrides": 3, "unread": 1, "rules": {"irreversibility": 1, "taint": 3}}
    assert found["counts"]["released"] == 0 and found["override_rate"] is None
    scoped = report.gate_report(ledger, subject="example-studio", since="2026-09-21")
    assert scoped["hook"] == {"overrides": 1, "unread": 0, "rules": {"irreversibility": 1, "taint": 1}}
    lines = report.report_lines(found)
    assert any(line.startswith("hook overrides") and "taint 3" in line for line in lines)


def test_hook_overrides_of_odd_shape_never_crash_the_report():
    from datetime import datetime

    from liaise import report
    from liaise.ledger import Ledger

    store = {}
    ledger = Ledger(store)
    for key, record in {
        "a": ["x"], "b": "junk", "c": {"writes": 5}, "d": {"writes": [{"rules": 3}]},
        "e": {"unread": "n/a"}, "f": {"unread": [1]}, "g": {"unread": -5},
        "h": {"writes": [{"rules": "taint", "subject": "s"}]},
        "i": {"writes": [{"subject": "a", "rules": ["taint"]}, {"subject": "b", "rules": ["irrev"]}], "unread": 2},
    }.items():
        store["override__" + key] = record
    found = report.gate_report(ledger)["hook"]
    assert found["unread"] == 2 and "a" not in found["rules"]
    assert report.gate_report(ledger, subject="a")["hook"] == {"overrides": 1, "unread": 0, "rules": {"taint": 1}}
    assert report.hook_overrides(ledger, since="2026-09-20")["overrides"] == 0
    assert report.hook_overrides(ledger, since=datetime(2026, 9, 20))["overrides"] == 0
