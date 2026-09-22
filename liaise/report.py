"""``liaise gate report``: what the outbound gate did, in counts, and whether to enforce (liaise #39).

The report reads the ``gate`` entries of every case and every message outside a case, and
counts; it never prints a message's text, a finding's value or a fingerprint, so it can be
pasted anywhere. What it counts (discussion 32 §5.7 and §7):

- **judged**: messages the gate judged on their own, with no approval on the context
  (a first judgement: the tick's send, divert or hold, and ``liaise message send``);
- **released**: an operator's approval that bound and let a held message go out (the
  outbox's own approvals, by :data:`~liaise.gate.OUTBOX_ACTOR`, are not the operator's and
  are left out); a release that did not edit the message is a **false divert**: the operator
  judged it fine as written;
- **rejected**: a draft the operator declined, joined to the judgement that held it (the
  latest earlier judged entry of the same case or message with the same text);
- **per rule**: how often it fired, how often its findings were released as false positives
  (an operator approval settled it), and how often they were confirmed (a draft it held was
  rejected), with the precision ``confirmed / (confirmed + released)``;
- **rates**: the override rate (releases over judged messages) and the false-divert rate
  (false diverts over the messages that should have gone as written: those sent as judged
  plus the false diverts);
- **shadow**: the judged messages of subjects in ``policy.mode = "shadow"``. Shadow mode
  enforces like ``enforce`` until its sending semantics are decided (liaise #39), so no
  would-be verdict differs from the decision yet, and shadow agreement and missed findings
  are not observable: the report says so rather than printing a number that means nothing.

**The rollout rule** (discussion 32, decision 11): enforce when shadow mode has seen at
least :data:`MIN_SHADOW_MESSAGES` messages, with no missed finding of severity
:data:`MISSED_SEVERITY` or above and a false-divert rate of at most
:data:`MAX_FALSE_DIVERT_RATE`. :func:`enforce_recommended` applies it and says why not.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from datetime import datetime, timezone
from typing import Any, Optional, Union

from liaise.gate import OUTBOX_ACTOR
from liaise.ledger import Ledger
from liaise.model import LedgerEntry
from liaise.policy import SEND, SHADOW

#: Decision 11: the fewest messages shadow mode must have seen before enforcing.
MIN_SHADOW_MESSAGES = 30
#: Decision 11: a missed finding at this severity or above blocks enforcing.
MISSED_SEVERITY = 4
#: Decision 11: the highest false-divert rate at which enforcing is recommended (one in ten).
MAX_FALSE_DIVERT_RATE = 0.1
#: The entry kind the gate's decisions are recorded as.
GATE_KIND = "gate"
#: Why shadow agreement and missed findings cannot be counted yet.
SHADOW_PENDING = (
    "shadow mode enforces like enforce until its sending semantics are decided "
    "(liaise #39), so it records no would-be verdict that differs from the decision"
)


def _moment(value: Union[str, datetime, None]) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(
                f"--since {value!r} is not a time; use ISO 8601, like 2026-09-20 or "
                "2026-09-20T09:00:00Z"
            ) from None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _rules(concerns: Iterable[Mapping[str, Any]]) -> set[str]:
    return {
        c["rule"] for c in concerns or () if isinstance(c, Mapping) and c.get("rule")
    }


def _is_judgement(detail: Mapping[str, Any]) -> bool:
    """A first judgement: the gate ran with no approval on the context."""
    return "flow" in detail and detail.get("approval") is None


def _is_operator_release(detail: Mapping[str, Any]) -> bool:
    approval = detail.get("approval")
    return (
        isinstance(approval, Mapping)
        and approval.get("by") != OUTBOX_ACTOR
        and detail.get("approval_bound") is True
        and detail.get("decision") == "send"
        and not detail.get("error")
    )


def _records(
    ledger: Ledger, subject: Optional[str]
) -> Iterator[tuple[str, tuple[LedgerEntry, ...]]]:
    """``(subject, entries)`` of every case and message outside a case, of ``subject`` or all."""
    for case in ledger.cases(subject=subject):
        yield case.subject, case.entries
    for message in ledger.messages(subject=subject):
        yield message.subject, message.entries


def _empty_rule() -> dict[str, int]:
    return {"fired": 0, "released": 0, "confirmed": 0}


def gate_report(
    ledger: Union[Ledger, MutableMapping[str, Any]],
    *,
    subject: Optional[str] = None,
    since: Union[str, datetime, None] = None,
    shadow_subjects: Iterable[str] = (),
) -> dict[str, Any]:
    """The counts of the gate's decisions in ``ledger`` (a :class:`Ledger` or its store).

    ``subject`` keeps one subject's, ``since`` the entries at or after it.
    ``shadow_subjects`` are the subjects whose policy is in shadow mode now; an entry whose
    verdict recorded ``mode = "shadow"`` counts as shadow whatever the subject says today.
    Counts only: nothing a message says is in it.
    """
    ledger = ledger if isinstance(ledger, Ledger) else Ledger(ledger)
    start = _moment(since)
    shadow_now = set(shadow_subjects)
    rules: dict[str, dict[str, int]] = {}
    counts = Counter()
    for owner, entries in _records(ledger, subject):
        judged_texts: list[
            tuple[str, set[str], str]
        ] = []  # (text, rules, flow), in order
        for entry in entries:
            detail = entry.detail or {}
            if entry.kind != GATE_KIND:
                continue
            counted = start is None or entry.at >= start
            if _is_judgement(detail):
                fired = _rules(detail.get("concerns"))
                judged_texts.append((entry.text or "", fired, detail.get("flow")))
                if not counted:
                    continue
                counts["judged"] += 1
                sent = detail.get("decision") == "send" and not detail.get("error")
                counts["sent as judged"] += sent
                counts["held"] += not sent and detail.get("flow") != SEND
                verdict = detail.get("verdict") or {}
                if verdict.get("mode") == SHADOW or owner in shadow_now:
                    counts["shadow"] += 1
                for rule in fired:
                    rules.setdefault(rule, _empty_rule())["fired"] += 1
            elif counted and _is_operator_release(detail):
                counts["released"] += 1
                counts["false diverts"] += not detail.get("edited")
                for rule in _rules(detail.get("settled")):
                    rules.setdefault(rule, _empty_rule())["released"] += 1
            elif counted and detail.get("decision") == "reject":
                counts["rejected"] += 1
                held = [
                    fired
                    for text, fired, flow in judged_texts
                    if text == (entry.text or "") and flow != SEND
                ]
                for rule in held[-1] if held else ():
                    rules.setdefault(rule, _empty_rule())["confirmed"] += 1
    for entry in rules.values():
        judged = entry["released"] + entry["confirmed"]
        entry["precision"] = (
            None if judged == 0 else round(entry["confirmed"] / judged, 3)
        )
    judged = counts["judged"]
    should_send = counts["sent as judged"] + counts["false diverts"]
    false_divert_rate = (
        None if should_send == 0 else counts["false diverts"] / should_send
    )
    report = {
        "subject": subject,
        "since": None if start is None else start.isoformat(),
        "counts": {
            key: counts[key]
            for key in (
                "judged",
                "sent as judged",
                "held",
                "released",
                "false diverts",
                "rejected",
                "shadow",
            )
        },
        "override_rate": None if judged == 0 else counts["released"] / judged,
        "false_divert_rate": false_divert_rate,
        "rules": dict(sorted(rules.items())),
        "shadow_agreement": None,
        "missed_high_severity": None,
        "shadow_note": SHADOW_PENDING,
    }
    report["enforce"] = enforce_recommended(
        shadow_messages=counts["shadow"],
        missed_high_severity=None,
        false_divert_rate=false_divert_rate,
    )
    return report


def enforce_recommended(
    *,
    shadow_messages: int,
    missed_high_severity: Optional[int],
    false_divert_rate: Optional[float],
) -> dict[str, Any]:
    """Decision 11's rollout rule: ``{"recommended": bool, "reason": str}``.

    ``missed_high_severity`` is how many findings of severity :data:`MISSED_SEVERITY` or
    above shadow mode let through (None: not observable, which never recommends).

    >>> enforce_recommended(shadow_messages=40, missed_high_severity=0, false_divert_rate=0.05)
    {'recommended': True, 'reason': '40 shadow messages, no missed finding of severity 4 or above, false-divert rate 0.05 (at most 0.1)'}
    >>> enforce_recommended(shadow_messages=12, missed_high_severity=0, false_divert_rate=0.0)['reason']
    'only 12 shadow messages, fewer than 30'
    """
    if shadow_messages < MIN_SHADOW_MESSAGES:
        return {
            "recommended": False,
            "reason": f"only {shadow_messages} shadow messages, fewer than {MIN_SHADOW_MESSAGES}",
        }
    if missed_high_severity is None:
        return {
            "recommended": False,
            "reason": f"missed findings are not observable yet: {SHADOW_PENDING}",
        }
    if missed_high_severity > 0:
        return {
            "recommended": False,
            "reason": (
                f"{missed_high_severity} missed finding(s) of severity {MISSED_SEVERITY} "
                "or above"
            ),
        }
    if false_divert_rate is None or false_divert_rate > MAX_FALSE_DIVERT_RATE:
        rate = "unknown" if false_divert_rate is None else f"{false_divert_rate:.2f}"
        return {
            "recommended": False,
            "reason": f"false-divert rate {rate}, above {MAX_FALSE_DIVERT_RATE}",
        }
    return {
        "recommended": True,
        "reason": (
            f"{shadow_messages} shadow messages, no missed finding of severity "
            f"{MISSED_SEVERITY} or above, false-divert rate {false_divert_rate:.2f} "
            f"(at most {MAX_FALSE_DIVERT_RATE})"
        ),
    }


def _rate(value: Optional[float]) -> str:
    return "n/a (nothing to count)" if value is None else f"{value:.3f}"


def report_lines(report: Mapping[str, Any]) -> list[str]:
    """``report`` (:func:`gate_report`) for a terminal: counts, a per-rule table, the rollout line."""
    counts = report["counts"]
    scope = report["subject"] or "every subject"
    since = f" since {report['since']}" if report["since"] else ""
    lines = [f"gate report for {scope}{since}"]
    lines += [f"  {name}: {value}" for name, value in counts.items()]
    lines.append(f"  override rate: {_rate(report['override_rate'])}")
    lines.append(f"  false-divert rate: {_rate(report['false_divert_rate'])}")
    lines.append(f"  shadow agreement: n/a ({report['shadow_note']})")
    rules = report["rules"]
    lines.append(
        f"per rule (fired, released as false positive, confirmed, precision): {len(rules)}"
    )
    width = max((len(rule) for rule in rules), default=0)
    for rule, entry in rules.items():
        precision = "n/a" if entry["precision"] is None else f"{entry['precision']:.3f}"
        lines.append(
            f"  {rule:<{width}}  {entry['fired']:>4}  {entry['released']:>4}  "
            f"{entry['confirmed']:>4}  {precision}"
        )
    enforce = report["enforce"]
    verdict = "yes" if enforce["recommended"] else "no"
    lines.append(f"enforce recommended: {verdict} ({enforce['reason']})")
    return lines
