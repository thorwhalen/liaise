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


def _judged(minutes, *, text, rules=(), decision=None, mode="enforce", error=None):
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


def test_shadow_entries_are_counted_and_agreement_is_reported_as_not_observable():
    entries = [_judged(i, text=f"m{i}", mode="shadow") for i in range(MIN_SHADOW_MESSAGES)]
    report = gate_report(_ledger(entries))

    assert report["counts"]["shadow"] == MIN_SHADOW_MESSAGES
    assert report["shadow_agreement"] is None and report["missed_high_severity"] is None
    assert not report["enforce"]["recommended"]
    assert "not observable" in report["enforce"]["reason"]
    assert gate_report(_ledger(entries[:1]), shadow_subjects=[SUBJECT])["counts"]["shadow"] == 1


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


from liaise.tests.test_tick import fixed_run_suffix, no_real_acquaint, world  # noqa: E402,F401
