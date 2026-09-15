"""The scenario runner: load ``scenarios.yaml``, build each case's inputs, mutate, evaluate.

A :class:`Prepared` case holds everything :func:`liaise.policy.evaluate` takes, built
from a scenario record and the fixtures: the message, the audience of its channel, the
disclosure for its readers, the findings the real detectors make over the text, the
title and the attachment names, the provenance, the policy and the clock. The mutation
generator (:func:`mutations`) derives the deterministic variants the design names
(discussion §7): alias and homoglyph substitutions, three channel swaps, an added Cc and
an added Bcc, and the sensitive span moved into quoted history, a link title and an
attachment name, each with the flows that pass.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from liaise.detect import Finding, detect
from liaise.policy import (
    APPROVE,
    FLOWS,
    REFUSE,
    RULES,
    SEND,
    OutboundPolicy,
    Provenance,
    Rule,
    Verdict,
    evaluate,
    flow_rank,
)
from liaise.tests.outbound.fixtures import (
    ALIASES,
    ALLOWLIST,
    AT,
    CANARY,
    ENTITIES,
    IDENTITIES,
    KEY,
    OPERATOR_ADDRESS,
    OPERATOR_TERMS,
    TODAY,
    TOKEN,
    address_of,
    audience_for,
    deep_merge,
    disclosure_for,
)

SCENARIOS_FILE = Path(__file__).with_name("scenarios.yaml")
DEFAULT_NOW = TODAY + "T12:00:00Z"
PLACEHOLDERS = {
    "{at}": AT,
    "{email}": OPERATOR_ADDRESS,
    "{token}": TOKEN,
    "{canary}": CANARY,
}
PROVENANCES = {
    "clean": Provenance.clean("every message read came from a trusted role"),
    "tainted": Provenance.tainted_by(
        "read an issue by an unknown author (grade none, role none)"
    ),
    "unknown": Provenance.unknown("the hook path: nobody can say what the run read"),
}
#: The channel swaps of discussion §7: a DM (a Telegram private chat), a public issue, a
#: shared channel (a private organisation repository). A swap onto the scenario's own
#: channel is not generated: it would re-run the scenario unchanged.
SWAP_CHANNELS = {"dm": "telegram_dm", "public": "public_issue", "shared": "org_repo"}
#: A homoglyph substitution: Latin letters to the Cyrillic shapes that imitate them.
HOMOGLYPHS = str.maketrans(
    {
        "a": "\u0430",
        "c": "\u0441",
        "e": "\u0435",
        "o": "\u043e",
        "p": "\u0440",
        "x": "\u0445",
        "y": "\u0443",
        "A": "\u0410",
        "B": "\u0412",
        "C": "\u0421",
        "E": "\u0415",
        "H": "\u041d",
        "K": "\u041a",
        "M": "\u041c",
        "O": "\u041e",
        "P": "\u0420",
        "T": "\u0422",
        "X": "\u0425",
        "Y": "\u0423",
    }
)
#: The other homoglyph substitution: ASCII letters to their full-width compatibility forms.
FULLWIDTH = str.maketrans(
    {
        c: chr(ord(c) - 0x20 + 0xFF00)
        for c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    }
)
STRANGER_CC = "email:pm" + AT + "partner.example"
BRAM_BCC = "email:bram" + AT + "example.org"


def _fill(value: Any) -> Any:
    if isinstance(value, str):
        for placeholder, replacement in PLACEHOLDERS.items():
            value = value.replace(placeholder, replacement)
        return value
    if isinstance(value, list):
        return [_fill(v) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v) for k, v in value.items()}
    return value


def load() -> dict[str, list[dict]]:
    """The scenario file, placeholders filled: ``{"scenarios": [...], "extras": [...]}``."""
    with SCENARIOS_FILE.open(encoding="utf-8") as file:
        data = yaml.safe_load(file)
    return _fill(data)


SUITE = load()
SCENARIOS: list[dict] = SUITE["scenarios"]
EXTRAS: list[dict] = SUITE["extras"]
BY_ID = {s["id"]: s for s in (*SCENARIOS, *EXTRAS)}


@dataclass(frozen=True)
class Message:
    """What the gate would hand the policy: the outbound message's fields."""

    ref: str
    channel: str
    recipient: str
    text: str
    title: Optional[str] = None
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    attachments: tuple[str, ...] = ()
    case_id: Optional[str] = None
    purpose: str = "reply"


@dataclass(frozen=True)
class Prepared:
    """One case, ready to evaluate: the inputs and the flows that pass."""

    id: str
    message: Message
    audience: dict
    disclosure: dict
    identities: Mapping[str, Optional[str]]
    provenance: Provenance
    policy: OutboundPolicy
    now: datetime
    expected: frozenset[str]
    scenario: Mapping[str, Any] = field(repr=False)

    def findings(self) -> tuple[Finding, ...]:
        """What the detectors find in the text, the title and the attachment names."""
        parts = [self.message.text, self.message.title or "", *self.message.attachments]
        found: list[Finding] = []
        for part in parts:
            if part:
                found += detect(
                    part,
                    disclosure=self.disclosure,
                    allowlist=ALLOWLIST,
                    canary_terms=(CANARY,),
                    personal_terms=OPERATOR_TERMS,
                    key=KEY,
                )
        return tuple(found)

    def evaluate(self, *, rules: Sequence[Rule] = RULES) -> Verdict:
        """The verdict, through ``rules``."""
        return evaluate(
            self.message,
            audience=self.audience,
            disclosure=self.disclosure,
            findings=self.findings(),
            provenance=self.provenance,
            policy=self.policy,
            now=self.now,
            identities=self.identities,
            _rules=rules,
        )


def _now(value: Optional[str]) -> datetime:
    stamp = datetime.fromisoformat((value or DEFAULT_NOW).replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def prepare(
    scenario: Mapping[str, Any],
    *,
    channel: Optional[str] = None,
    text: Optional[str] = None,
    cc: Optional[Sequence[str]] = None,
    bcc: Optional[Sequence[str]] = None,
    title: Optional[str] = None,
    attachments: Sequence[str] = (),
    expected: Optional[Sequence[str]] = None,
    label: str = "",
) -> Prepared:
    """The :class:`Prepared` case of ``scenario``, with any of its inputs replaced."""
    channel = channel or scenario["channel"]
    recipient = scenario["recipient"]
    cc = tuple(scenario.get("cc") or () if cc is None else cc)
    bcc = tuple(scenario.get("bcc") or () if bcc is None else bcc)
    audience = audience_for(channel, recipient=recipient, cc=cc, bcc=bcc)
    ref = audience["ref"]
    message = Message(
        ref=ref,
        channel=ref.partition(":")[0],
        recipient=recipient,
        text=scenario["text"] if text is None else text,
        title=scenario.get("title") if title is None else title,
        cc=cc,
        bcc=bcc,
        attachments=tuple(scenario.get("attachments") or ()) + tuple(attachments),
        case_id=f"app-{scenario['id'].lower()}" if scenario.get("case") else None,
    )
    changes = scenario.get("disclosure") or {}
    disclosure = disclosure_for(
        [recipient, *cc, *bcc],
        audience=audience,
        today=str(_now(scenario.get("now")).date()),
        overrides=changes.get("people"),
        gaps=changes.get("gaps"),
    )
    identities = {
        **IDENTITIES,
        **{given: IDENTITIES.get(given) for given in (*cc, *bcc)},
        **{
            r["channel"] + ":" + r["handle"]: IDENTITIES.get(
                r["channel"] + ":" + r["handle"]
            )
            for r in audience["readers"]
        },
    }
    return Prepared(
        id=f"{scenario['id']}{label}",
        message=message,
        audience=audience,
        disclosure=disclosure,
        identities=identities,
        provenance=PROVENANCES[scenario.get("provenance", "unknown")],
        policy=OutboundPolicy(**(scenario.get("policy") or {})),
        now=_now(scenario.get("now")),
        expected=frozenset(scenario["expected"] if expected is None else expected),
        scenario=scenario,
    )


def at_least(flow: str) -> frozenset[str]:
    """Every flow at least as restrictive as ``flow``."""
    return frozenset(f for f in FLOWS if flow_rank(f) >= flow_rank(flow))


def _min_expected(scenario: Mapping) -> str:
    return min(scenario["expected"], key=flow_rank)


def _sealed_from_bram(scenario: Mapping) -> bool:
    terms = ENTITIES["project:heron"]["terms"]
    text = scenario["text"].casefold()
    return any(term.casefold() in text for term in terms)


def _quoted(text: str) -> str:
    quoted = "\n> ".join(text.strip().splitlines())
    return f"Yes, Thursday works. See you then.\n\n> {quoted}"


def mutations(scenario: Mapping[str, Any]) -> Iterator[Prepared]:
    """The deterministic mutations of ``scenario`` (discussion §7), each with its expectation.

    Substitutions apply to a sensitive span that is a term with aliases; the moves apply
    to any sensitive span; quoting applies to every scenario; the swaps and the added
    copies apply to every scenario.
    """
    text = scenario["text"]
    span = scenario.get("sensitive")
    same = scenario["expected"]
    if span and span in ALIASES:
        substitutes = [(f"alias-{alias}", alias) for alias in ALIASES[span]] + [
            ("homoglyph-cyrillic", span.translate(HOMOGLYPHS)),
            ("homoglyph-fullwidth", span.translate(FULLWIDTH)),
        ]
        for name, substitute in substitutes:
            mutated = text.replace(span, substitute)
            assert mutated != text, (scenario["id"], name)
            yield prepare(scenario, text=mutated, expected=same, label=f":{name}")
    for name, channel in SWAP_CHANNELS.items():
        if channel == scenario["channel"]:
            continue
        yield prepare(
            scenario,
            channel=channel,
            expected=scenario["swaps"][name],
            label=f":swap-{name}",
        )
    floor = max((APPROVE, _min_expected(scenario)), key=flow_rank)
    yield prepare(
        scenario,
        cc=[*(scenario.get("cc") or ()), STRANGER_CC],
        expected=at_least(floor),
        label=":cc-stranger",
    )
    bcc_expected = {REFUSE} if _sealed_from_bram(scenario) else at_least(floor)
    yield prepare(
        scenario,
        bcc=[*(scenario.get("bcc") or ()), BRAM_BCC],
        expected=bcc_expected,
        label=":bcc-bram",
    )
    yield prepare(scenario, text=_quoted(text), expected=same, label=":quoted")
    if span:
        titled = text.replace(span, f'[the notes](https://example.org/notes "{span}")')
        yield prepare(scenario, text=titled, expected=same, label=":link-title")
        moved = text.replace(span, "the attached")
        yield prepare(
            scenario,
            text=moved,
            attachments=[f"{span} notes.pdf"],
            expected=same,
            label=":attachment",
        )


def all_cases(scenarios: Sequence[Mapping] = SCENARIOS) -> list[Prepared]:
    """Every scenario and every mutation of it."""
    cases = []
    for scenario in scenarios:
        cases.append(prepare(scenario))
        cases.extend(mutations(scenario))
    return cases


def weakened(rule: Rule) -> Rule:
    """``rule`` asking for one flow less than it does, hit by hit."""

    def predicate(facts):
        for hit in rule.predicate(facts):
            flow = hit.flow or rule.flow
            index = flow_rank(flow)
            yield replace(hit, flow=FLOWS[max(index - 1, 0)])

    return Rule(
        rule.name,
        predicate,
        rule.flow if rule.flow == SEND else FLOWS[flow_rank(rule.flow) - 1],
    )


def without(name: str, rules: Sequence[Rule] = RULES) -> tuple[Rule, ...]:
    """``rules`` without the rule called ``name``."""
    return tuple(rule for rule in rules if rule.name != name)


def weakening(name: str, rules: Sequence[Rule] = RULES) -> tuple[Rule, ...]:
    """``rules`` with the rule called ``name`` weakened."""
    return tuple(weakened(rule) if rule.name == name else rule for rule in rules)


def metrics(scenarios: Sequence[Mapping] = SCENARIOS) -> dict[str, Any]:
    """The suite's numbers (research §9.2): miss rate, false-divert rate, utility under attack.

    A miss is a verdict less restrictive than every expected flow, or an expected finding
    that is absent; the miss rate weighs them by severity over the scenarios whose
    correct verdict is anything but ``send``. A false divert is a verdict outside the
    expected flows on a scenario whose correct verdict may be ``send``. A scenario
    delivers when its route is not ``block``.
    """
    weight = missed_weight = 0
    misses: list[str] = []
    send_scenarios = diverted = 0
    delivered: dict[str, bool] = {}
    verdicts: dict[str, str] = {}
    for scenario in scenarios:
        case = prepare(scenario)
        verdict = case.evaluate()
        verdicts[scenario["id"]] = verdict.flow
        found = {f.entity for f in verdict.findings}
        too_loose = flow_rank(verdict.flow) < min(map(flow_rank, case.expected))
        missed = too_loose or (
            scenario.get("expects_finding") and scenario["expects_finding"] not in found
        )
        if SEND not in case.expected:
            weight += scenario["severity"]
            if missed:
                missed_weight += scenario["severity"]
                misses.append(scenario["id"])
        else:
            send_scenarios += 1
            diverted += verdict.flow not in case.expected
        if scenario.get("deliver"):
            delivered[scenario["id"]] = verdict.route != "block"
    return {
        "verdicts": verdicts,
        "miss_rate": missed_weight / weight if weight else 0.0,
        "misses": misses,
        "false_divert_rate": diverted / send_scenarios if send_scenarios else 0.0,
        "send_scenarios": send_scenarios,
        "utility_under_attack": delivered,
    }


def scenario_copy(scenario: Mapping[str, Any], **changes: Any) -> dict:
    """``scenario`` with top-level ``changes`` (a deep copy)."""
    return deep_merge(copy.deepcopy(dict(scenario)), changes)


__all__ = [
    "BY_ID",
    "EXTRAS",
    "SCENARIOS",
    "Message",
    "Prepared",
    "address_of",
    "all_cases",
    "at_least",
    "load",
    "metrics",
    "mutations",
    "prepare",
    "scenario_copy",
    "weakened",
    "weakening",
    "without",
]
