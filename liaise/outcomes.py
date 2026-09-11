"""Outcomes: what a processor run reports, checked, then planned into actions.

A run reports its result as structured output in the shape of :data:`OUTCOME_SCHEMA`,
which the processor passes to ``claude --json-schema``: a ``summary``, and one or more
outcomes, each a ``kind`` from the closed vocabulary :data:`~liaise.model.OUTCOME_KINDS`
with its ``text``, ``questions`` and ``reason``.

- :func:`parse_outcomes` checks that output and returns its
  :class:`~liaise.model.Outcome` records, or raises ``ValueError`` listing every problem.
- :func:`normalize` makes each ``decline`` an ``escalate``.
- :func:`plan_outcomes` turns a case's outcomes into actions, in outcome order:

  - ``ask``: :class:`Send` the text and the numbered questions, then
    :class:`Transition` to ``needs-partner``;
  - ``reply``: :class:`Send`;
  - ``escalate``, and ``decline``: :class:`StoreDraft`, :class:`NotifyOperator` at high
    priority, then :class:`Transition` to ``needs-owner``;
  - ``propose``: :class:`Send`, then :class:`Transition` to ``needs-partner``;
  - ``deliver``: :class:`Deliver` as the subject's ``delivery`` says, :class:`Send` the
    "try it" text, then :class:`Transition` to ``deployed``;
  - ``defer``: :class:`Defer`;
  - ``note``: :class:`DigestNote`.

Planning is pure: it reads the case and the subject and does nothing. The tick executes
the actions in order, and passes each :class:`Send` through :func:`liaise.gate.run_gate`
before sending it.

**Where a Send goes.** To the case's reporter, at the case's first conversation on a
channel liaise can post into (GitHub, in v0.1). A case with none, such as one reported
through a web inbox, is answered at the reporter's notify address when that is on a
channel that can write to a person (email). Failing both, the message becomes a draft
for the operator, with a ``no channel to reach <person>`` notification.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Optional, Union

from correspond.model import ConversationRef

from liaise.gate import Outbound, notify_addresses
from liaise.model import CASE_STATES, OUTCOME_KINDS, Case, Outcome, require_one_of
from liaise.subjects import Subject

#: Channels whose conversations liaise posts into. A web inbox has no writer.
DFLT_SENDING_CHANNELS = ("github",)
#: Channels that can write to a person's address (``email:<address>``), not only into a
#: conversation. GitHub is not one: a ``github:<login>`` handle cannot be messaged.
DFLT_ADDRESS_CHANNELS = ("email",)
#: The ntfy priority of an operator notification that needs the operator to act.
DFLT_OPERATOR_PRIORITY = "high"

#: The field each outcome kind cannot do without. :func:`parse_outcomes` enforces it
#: and :data:`OUTCOME_SCHEMA` states it.
REQUIRED_FIELD_BY_KIND = MappingProxyType(
    {
        "ask": "questions",
        "reply": "text",
        "escalate": "reason",
        "propose": "text",
        "deliver": "text",
        "decline": "reason",
        "defer": "reason",
        "note": "text",
    }
)

#: The JSON Schema of a run's structured result, passed to ``claude --json-schema``.
OUTCOME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcomes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": list(OUTCOME_KINDS),
                        "description": "What to do. Each kind needs one field: "
                        + "; ".join(
                            f"{kind} needs {field}"
                            for kind, field in REQUIRED_FIELD_BY_KIND.items()
                        )
                        + ".",
                    },
                    "text": {
                        "type": "string",
                        "description": "The message to the partner. For escalate "
                        "and decline, a draft the operator may send; for note, the "
                        "line for the operator's digest.",
                    },
                    "questions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "For ask: each question, with the default "
                        "you will take if it goes unanswered.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why, for the operator: what an escalate, "
                        "decline or defer is waiting on.",
                    },
                },
                "required": ["kind"],
                "additionalProperties": False,
            },
        },
        "summary": {
            "type": "string",
            "description": "What this run did, in a sentence or two, for the operator.",
        },
    },
    "required": ["outcomes"],
    "additionalProperties": False,
}

_RESULT_FIELDS = tuple(OUTCOME_SCHEMA["properties"])
_OUTCOME_FIELDS = tuple(OUTCOME_SCHEMA["properties"]["outcomes"]["items"]["properties"])


# ---- actions: what the tick executes, in order ----


@dataclass(frozen=True)
class Send(Outbound):
    """Send ``text`` to ``recipient`` at ``ref``. Every Send is the Outbound the gate checks."""


@dataclass(frozen=True)
class Transition:
    """Move the case to ``state``, recording ``reason``."""

    case_id: str
    state: str
    reason: str

    def __post_init__(self) -> None:
        require_one_of(self.state, CASE_STATES, what="case state")


@dataclass(frozen=True)
class NotifyOperator:
    """Tell the operator, as :func:`liaise.notify.notify` does."""

    title: str
    body: str
    priority: str = "default"


@dataclass(frozen=True)
class StoreDraft:
    """Keep ``draft`` (see :func:`make_draft`) in the case's ``drafts`` for the operator."""

    case_id: str
    draft: Mapping[str, Any]


@dataclass(frozen=True)
class Deliver:
    """Deliver the case's work as the subject's ``delivery`` says."""

    case_id: str
    kind: str
    per: str
    command: str


@dataclass(frozen=True)
class Defer:
    """Put the case aside for ``reason``."""

    case_id: str
    reason: str


@dataclass(frozen=True)
class DigestNote:
    """Add ``text`` to the operator's digest."""

    case_id: str
    text: str


#: What :func:`plan_outcomes` returns a list of.
Action = Union[Send, Transition, NotifyOperator, StoreDraft, Deliver, Defer, DigestNote]


def make_draft(
    *,
    at: datetime,
    outcome: str,
    recipient: str,
    ref: Optional[str],
    text: str,
    reason: str,
    notes: Iterable[str] = (),
) -> dict[str, Any]:
    """One item of a case's ``drafts``: a message held for the operator.

    This is the one shape every draft has, JSON-ready:

    - ``at``: when it was held, as ISO-8601;
    - ``outcome``: the outcome kind it carries out (``ask``, ``escalate``, ...);
    - ``recipient``: the person id it is for;
    - ``ref``: the encoded conversation or address it would go to, or None when nothing
      can reach the recipient;
    - ``text``: the message, as it would be sent;
    - ``reason``: why it was held (an escalation's reason, a gate divert, no channel);
    - ``notes``: the gate's notes on it, in order.

    >>> from datetime import datetime, timezone
    >>> make_draft(at=datetime(2026, 9, 11, tzinfo=timezone.utc), outcome="reply",
    ...     recipient="pat", ref="github:example/app#12", text="Fixed.",
    ...     reason="draft reply mode")  # doctest: +NORMALIZE_WHITESPACE
    {'at': '2026-09-11T00:00:00+00:00', 'outcome': 'reply', 'recipient': 'pat',
     'ref': 'github:example/app#12', 'text': 'Fixed.', 'reason': 'draft reply mode',
     'notes': []}
    """
    return {
        "at": at.isoformat(),
        "outcome": outcome,
        "recipient": recipient,
        "ref": ref,
        "text": text,
        "reason": reason,
        "notes": list(notes),
    }


def parse_outcomes(structured_output: Any) -> tuple[Outcome, ...]:
    """The outcomes of a run's structured output, checked against :data:`OUTCOME_SCHEMA`.

    Beyond the schema, each kind must carry the field :data:`REQUIRED_FIELD_BY_KIND`
    names: an ``ask`` its questions, a ``reply`` its text, and so on. A ``decline`` is
    kept as reported (:func:`plan_outcomes` normalizes it). Raises ``ValueError``
    listing every problem found, not only the first.
    """
    if not isinstance(structured_output, Mapping):
        raise ValueError(
            "invalid structured output: expected an object with 'outcomes', got "
            f"{type(structured_output).__name__}"
        )

    def names(keys: Iterable[Any]) -> str:
        return ", ".join(map(repr, keys))

    def outcome_problems(item: Any, where: str) -> Iterator[str]:
        if not isinstance(item, Mapping):
            yield f"{where} must be an object, got {type(item).__name__}"
            return
        unknown = [key for key in item if key not in _OUTCOME_FIELDS]
        if unknown:
            yield (
                f"{where} has unknown key(s) {names(unknown)}; an outcome has only "
                f"{names(_OUTCOME_FIELDS)}"
            )
        kind = item.get("kind")
        if kind is None:
            yield f"{where}.kind is missing"
        elif kind not in OUTCOME_KINDS:
            yield f"{where}.kind {kind!r} is not one of: {', '.join(OUTCOME_KINDS)}"
        for name in ("text", "reason"):
            if not isinstance(item.get(name, ""), str):
                yield f"{where}.{name} must be a string"
        questions = item.get("questions", [])
        if not isinstance(questions, (list, tuple)) or not all(
            isinstance(question, str) and question.strip() for question in questions
        ):
            yield f"{where}.questions must be a list of non-empty strings"
        if kind in OUTCOME_KINDS:
            required = REQUIRED_FIELD_BY_KIND[kind]
            value = item.get(required)
            if not value or (isinstance(value, str) and not value.strip()):
                yield f"{where}: {kind} needs a non-empty {required}"

    problems: list[str] = []
    unknown = [key for key in structured_output if key not in _RESULT_FIELDS]
    if unknown:
        problems.append(
            f"unknown key(s) {names(unknown)}; the result has only "
            f"{names(_RESULT_FIELDS)}"
        )
    if not isinstance(structured_output.get("summary", ""), str):
        problems.append("summary must be a string")
    raw = structured_output.get("outcomes")
    if raw is None:
        problems.append("outcomes is missing: report at least one outcome")
    elif not isinstance(raw, (list, tuple)):
        problems.append(f"outcomes must be a list, got {type(raw).__name__}")
    elif not raw:
        problems.append("outcomes is empty: report at least one outcome")
    else:
        for index, item in enumerate(raw):
            problems.extend(outcome_problems(item, f"outcomes[{index}]"))
    if problems:
        listed = "\n".join(f"- {problem}" for problem in problems)
        raise ValueError(f"invalid structured output:\n{listed}")
    return tuple(
        Outcome(
            kind=item["kind"],
            text=item.get("text", ""),
            questions=tuple(item.get("questions", ())),
            reason=item.get("reason", ""),
        )
        for item in raw
    )


def normalize(outcomes: Iterable[Outcome]) -> tuple[Outcome, ...]:
    """``outcomes`` with each ``decline`` made an ``escalate``, its reason ``decline: <reason>``.

    A refusal reaches the operator before it reaches the partner. Every other outcome
    is returned as it was.
    """
    return tuple(
        replace(
            outcome,
            kind="escalate",
            reason=f"decline: {outcome.reason}" if outcome.reason else "decline",
        )
        if outcome.kind == "decline"
        else outcome
        for outcome in outcomes
    )


def plan_outcomes(
    case: Case,
    outcomes: Iterable[Outcome],
    subject: Subject,
    *,
    now: datetime,
    sending_channels: Collection[str] = DFLT_SENDING_CHANNELS,
    address_channels: Collection[str] = DFLT_ADDRESS_CHANNELS,
) -> list[Action]:
    """The actions that carry out ``outcomes`` on ``case``, one group per outcome, in order.

    The module docstring lists what each kind plans; ``decline`` is planned as
    ``escalate`` (see :func:`normalize`). Messages are for ``case.reporter``, and go to
    the case's first conversation on one of ``sending_channels``. Failing that, they go
    to the reporter's first notify address (see :func:`liaise.gate.notify_addresses`) on
    one of ``address_channels``. Failing both, each becomes a :class:`StoreDraft` with a
    ``no channel to reach <person>`` :class:`NotifyOperator`. ``now`` stamps the drafts.

    Raises ``ValueError`` for an outcome kind outside the vocabulary.
    """
    person = case.reporter
    conversations = (
        ref
        for ref in case.conversations
        if ConversationRef.parse(ref).channel in sending_channels
    )
    addresses = notify_addresses(subject, person, channels=address_channels)
    ref = next(conversations, None) or next(iter(addresses), None)
    channel = ConversationRef.parse(ref).channel if ref else None

    def text_of(outcome: Outcome) -> str:
        questions = "\n".join(
            f"{number}. {question}"
            for number, question in enumerate(outcome.questions, start=1)
        )
        return "\n\n".join(part for part in (outcome.text, questions) if part)

    def operator_body(why: str, outcome: Outcome) -> str:
        lines = (
            ("why", why),
            ("draft", text_of(outcome)),
            ("case", case.id),
            ("conversations", ", ".join(case.conversations)),
        )
        return "\n".join(f"{label}: {value}" for label, value in lines if value)

    def draft(outcome: Outcome, *, reason: str) -> StoreDraft:
        return StoreDraft(
            case.id,
            make_draft(
                at=now,
                outcome=outcome.kind,
                recipient=person,
                ref=ref,
                text=text_of(outcome),
                reason=reason,
            ),
        )

    def send(outcome: Outcome) -> list[Action]:
        if ref is None:
            headline = f"no channel to reach {person}"
            why = (
                f"no conversation on {case.id} can be written to, and {person} has "
                f"no address on {', '.join(address_channels) or 'any channel'}"
            )
            return [
                draft(outcome, reason=headline),
                NotifyOperator(
                    headline,
                    operator_body(why, outcome),
                    priority=DFLT_OPERATOR_PRIORITY,
                ),
            ]
        return [
            Send(
                case_id=case.id,
                ref=ref,
                channel=channel,
                recipient=person,
                purpose=outcome.kind,
                text=text_of(outcome),
            )
        ]

    def transition(state: str, outcome: Outcome) -> Transition:
        return Transition(case.id, state, outcome.reason or outcome.kind)

    actions: list[Action] = []
    for outcome in normalize(outcomes):
        kind = outcome.kind
        if kind in ("ask", "propose"):
            actions += [*send(outcome), transition("needs-partner", outcome)]
        elif kind == "reply":
            actions += send(outcome)
        elif kind == "escalate":
            actions += [
                draft(outcome, reason=outcome.reason),
                NotifyOperator(
                    f"{case.id} needs you",
                    operator_body(outcome.reason, outcome),
                    priority=DFLT_OPERATOR_PRIORITY,
                ),
                transition("needs-owner", outcome),
            ]
        elif kind == "deliver":
            delivery = subject.delivery
            actions += [
                Deliver(case.id, delivery.kind, delivery.per, delivery.command),
                *send(outcome),
                transition("deployed", outcome),
            ]
        elif kind == "defer":
            actions.append(Defer(case.id, outcome.reason))
        elif kind == "note":
            actions.append(DigestNote(case.id, outcome.text))
        else:
            raise ValueError(
                f"cannot plan outcome kind {kind!r}: not one of "
                f"{', '.join(OUTCOME_KINDS)}"
            )
    return actions
