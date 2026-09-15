"""Releasing a message: through the gate, then through correspond, as one step.

Every message liaise sends goes through :func:`gate_and_send`. That covers what the tick
sends for a run's outcomes, the tick's own notices, a message an agent sends outside any
case (``liaise message send``), and a draft the operator releases. It runs
:func:`liaise.gate.run_gate`, and only a message the gate passed reaches
``correspond.send``, as the filters left it. It records nothing: what a
:class:`SendAttempt` means for a case, a message or a notification is for its caller to
keep.

**Releasing a held message.** :func:`release_draft` is the one way a message held for the
operator goes out, whether it waits on a case (``liaise case send-draft``) or outside any
(``liaise message send-draft``). It checks what must stop a release, runs
:func:`gate_and_send` with the operator's :class:`~liaise.model.Approval` on the context,
and hands back the ledger entry and the draft to keep, for the caller to record.

One path for every sender is what makes the gate a gate. A filter added to it applies to
all of them at once, and none of them has a way to send around it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Optional

import correspond
from correspond.channels.github import REF_RE
from correspond.model import ConversationRef

from liaise.gate import (
    DFLT_OUTBOUND_FILTERS,
    GateContext,
    GateDecision,
    Outbound,
    OutboundFilter,
    run_gate,
)
from liaise.holds import blocking_hold, scopes_for
from liaise.ledger import Ledger
from liaise.model import Approval, Case, LedgerEntry
from liaise.outcomes import HELD_REASON_PREFIX, make_draft
from liaise.subjects import Subject

#: Why a send failed when the channel said no without saying why.
DFLT_REFUSAL = "the channel refused it"
#: The outcome whose message announces a delivery.
DELIVER_PURPOSE = "deliver"
#: The entry kind a released or rejected draft is recorded as: a gate decision, as the
#: tick's are.
DRAFT_ENTRY_KIND = "gate"
#: The channel whose references name repositories.
GITHUB_CHANNEL = "github"


def error_text(error: BaseException) -> str:
    """How liaise names an exception it recovered from: its class, then its message.

    >>> error_text(ValueError("no such channel"))
    'ValueError: no such channel'
    """
    return f"{type(error).__name__}: {error}"


def github_repo(ref: Optional[str]) -> Optional[str]:
    """``owner/repo``, lower-cased, of the repository a GitHub reference names: itself or one of its issues.

    >>> github_repo("github:Example/App#12"), github_repo("github:example/app")
    ('example/app', 'example/app')
    >>> github_repo("webinbox:example-site") is None
    True
    """
    channel, _, native_id = (ref or "").strip().partition(":")
    match = REF_RE.match(native_id) if channel.lower() == GITHUB_CHANNEL else None
    return f"{match['owner']}/{match['repo']}".lower() if match else None


def sendable_ref(ref: str) -> str:
    """``ref`` as a GitHub issue or repository reference, stripped and lower-cased.

    >>> sendable_ref(" github:Example/App#12 ")
    'github:example/app#12'

    Raises ``ValueError`` for anything else: another channel, a malformed reference, or an
    issue number that is not a positive whole number.
    """
    channel, _, native_id = ref.strip().partition(":")
    match = REF_RE.fullmatch(native_id) if channel.lower() == GITHUB_CHANNEL else None
    number = match["number"] if match else None
    if match is None or (number is not None and int(number) < 1):
        raise ValueError(
            f"{ref!r} is not a GitHub issue (github:owner/repo#N) or repository "
            f"(github:owner/repo), the conversations a message outside a case goes to"
        )
    repo = f"{GITHUB_CHANNEL}:{match['owner']}/{match['repo']}".lower()
    return f"{repo}#{int(number)}" if number is not None else repo


@dataclass(frozen=True)
class SendAttempt:
    """What :func:`gate_and_send` did with one message.

    ``decision`` is the gate's. Once the gate has passed the message, ``result`` is
    correspond's ``SendResult``, or None when sending raised. ``failure`` says why the
    channel did not take the message, and is None once it did. ``failure_kind`` names that
    failure: correspond's ``error_kind``, or the class of what was raised. A message the
    gate diverted has none of the three.
    """

    decision: GateDecision
    result: Optional[Any] = None
    failure: Optional[str] = None
    failure_kind: Optional[str] = None

    @property
    def outbound(self) -> Optional[Outbound]:
        """The message as the gate's filters left it, or None when the gate diverted it."""
        return self.decision.send

    @property
    def sent(self) -> bool:
        """Whether the channel took the message; in a dry run, whether it would have."""
        return self.decision.send is not None and self.failure is None


def gate_and_send(
    outbound: Outbound,
    ctx: GateContext,
    *,
    registry: Optional[Mapping[str, Any]] = None,
    dry_run: bool = False,
    outbound_filters: Iterable[OutboundFilter] = DFLT_OUTBOUND_FILTERS,
) -> SendAttempt:
    """Put ``outbound`` through the gate and, only when it passes, send it through correspond.

    The gate is :func:`liaise.gate.run_gate` with ``outbound_filters``, and a message it
    diverts is not sent. A passed message goes to ``correspond.send`` as the filters left
    it, its title included, on ``registry`` (correspond's own when None). ``dry_run`` asks
    correspond for its plan and sends nothing. A channel that refuses the message, or
    raises, becomes a ``failure`` on the attempt rather than an exception, so the caller
    still has the message to keep.
    """
    decision = run_gate(outbound, ctx, outbound_filters=outbound_filters)
    passed = decision.send
    if passed is None:
        return SendAttempt(decision)
    try:
        result = correspond.send(
            passed.ref,
            passed.text,
            title=passed.title,
            dry_run=dry_run,
            registry=registry,
        )
    except Exception as error:  # an unknown channel, an adapter that raised
        return SendAttempt(
            decision, failure=error_text(error), failure_kind=type(error).__name__
        )
    if result.ok:
        return SendAttempt(decision, result=result)
    return SendAttempt(
        decision,
        result=result,
        failure=result.error or DFLT_REFUSAL,
        failure_kind=result.error_kind,
    )


# ---- releasing a held message ----


class DraftSentNotRecorded(RuntimeError):
    """A released message went out, and the ledger then failed to record that it did."""

    @classmethod
    def after(
        cls,
        label: str,
        attempt: SendAttempt,
        error: BaseException,
        *,
        reject: Optional[str],
    ) -> DraftSentNotRecorded:
        """The error for ``label``, which ``attempt`` sent and the ledger failed to record.

        ``reject`` is the command that takes the held message off, so it is not sent twice,
        or None when there is no record to take it off.
        """
        url = getattr(attempt.result, "url", None)
        where = f" as {url}" if url else ""
        take_off = (
            f' Take it off with {reject} --reason "sent, not recorded".' if reject else ""
        )
        return cls(
            f"{label} was sent{where}, but the ledger could not record it "
            f"({error_text(error)}): do not send it again.{take_off}"
        )


@dataclass(frozen=True)
class DraftOutcome:
    """What :func:`release_draft` did with one held message, for its caller to record.

    ``attempt`` is the gate's decision and the send, ``filters`` how many filters the gate
    ran it through, and ``edited`` whether the operator's text replaced the draft's.
    ``entry`` is the ``gate`` entry the release is recorded as. ``kept`` is the draft that
    stays for the operator when nothing went out, and None once the message is sent. Both
    are None for a plan (``send=False``) that the gate passed: nothing happened to record.
    """

    attempt: SendAttempt
    filters: int
    edited: bool
    entry: Optional[LedgerEntry] = None
    kept: Optional[dict[str, Any]] = None


def release_draft(
    draft: Mapping[str, Any],
    *,
    subject: Subject,
    ledger: Ledger,
    label: str,
    reject: str,
    by: str,
    now: datetime,
    case: Optional[Case] = None,
    text: Optional[str] = None,
    title: Optional[str] = None,
    detail: Mapping[str, Any] = MappingProxyType({}),
    registry: Optional[Mapping[str, Any]] = None,
    send: bool = True,
    dry_run: bool = False,
    outbound_filters: Iterable[OutboundFilter] = DFLT_OUTBOUND_FILTERS,
) -> DraftOutcome:
    """Release ``draft`` (a :func:`liaise.outcomes.make_draft` item) as ``by``, through the gate.

    The message is the draft's text and title, or ``text`` and ``title`` when the operator
    edited them. It goes to the draft's ``ref``, for its ``recipient``, carrying out its
    ``outcome``, on the case ``case`` or outside any when that is None. It passes through
    :func:`gate_and_send` with an :class:`~liaise.model.Approval` by ``by`` at ``now`` on
    the context: draft reply mode lets it through, and every other filter judges it as it
    judges any message, the mention included.

    It asks no one and records nothing. Its caller shows the operator the verdict first,
    from a dry run, and records the outcome's ``entry`` (with ``detail`` added to it) and,
    when nothing went out, its ``kept`` draft. The kept draft holds the operator's text
    without the mention the gate adds, and the new reason. ``send=False`` asks the channel
    only for its plan. A dry run judges and plans as a send would. ``label`` names the
    message in errors, and ``reject`` is the command that takes it off.

    Raises ``ValueError``, sending nothing, for any of these:

    - a draft with no destination, or no text;
    - a ``deliver`` message a hold kept, whose delivery never ran;
    - a hold on the subject, the recipient, the repository, the checkout or, for a
      ``deliver`` message, the delivery, that keeps effects waiting.
    """
    ref, recipient, purpose = (draft.get(key) for key in ("ref", "recipient", "outcome"))
    if not ref:
        raise ValueError(
            f"{label} has no destination ({draft.get('reason')}): send it yourself, then "
            f"take it off with {reject}"
        )
    try:
        channel = ConversationRef.parse(ref).channel
    except Exception as error:  # correspond's InvalidRef, or anything a bad ref raises
        raise ValueError(f"{label} goes to {ref!r}: {error_text(error)}") from error
    delivers = purpose == DELIVER_PURPOSE
    if delivers and str(draft.get("reason") or "").startswith(HELD_REASON_PREFIX):
        raise ValueError(
            f"{label} tells {recipient} a change is live, but a hold kept that delivery "
            f"from running ({draft.get('reason')}), so nothing was sent: deliver the "
            f"change first, then take this draft off with {reject}"
        )
    body = (draft.get("text") or "") if text is None else text
    if not body.strip():
        hint = "; write it with --edit" if text is None else ""
        raise ValueError(f"{label} has no text to send{hint}")
    refs = (ref, *(case.conversations if case is not None else ()))
    scopes = scopes_for(
        subject=subject.slug,
        person=recipient or None,
        repo=next(filter(None, map(github_repo, refs)), None),
        checkout=subject.workspace.path or None,
        effect=subject.delivery.kind if delivers else None,
        processor=False,
    )
    hold = blocking_hold(ledger, scopes, for_="effect")
    if hold is not None:
        waiting = "this delivery" if delivers else "these messages"
        raise ValueError(
            f"{label} was not sent: the hold on {hold.scope} ({hold.mode}) keeps "
            f"{waiting} waiting; liaise unhold {hold.scope} first"
        )

    approval = Approval(by=by, at=now)
    filters = tuple(outbound_filters)
    headline = draft.get("title") if title is None else (title.strip() or None)
    outbound = Outbound(
        ref=ref,
        channel=channel,
        recipient=recipient,
        purpose=purpose,
        text=body,
        title=headline,
        case_id=case.id if case is not None else None,
    )
    context = GateContext(subject=subject, case=case, now=now, approval=approval)
    attempt = gate_and_send(
        outbound,
        context,
        registry=registry,
        dry_run=dry_run or not send,
        outbound_filters=filters,
    )
    decision = attempt.decision
    edited = body != (draft.get("text") or "") or headline != draft.get("title")
    if attempt.sent and not send and not dry_run:
        return DraftOutcome(attempt, len(filters), edited)  # a plan: nothing to record
    recorded = {
        "purpose": purpose,
        "ref": ref,
        "notes": list(decision.notes),
        **detail,
        "held_for": draft.get("reason"),
        "edited": edited,
        "approval": approval.to_dict(),
    }
    if attempt.sent:
        url = getattr(attempt.result, "url", None)
        verdict = {"decision": "send", "url": url}
        entry = LedgerEntry(
            at=now,
            kind=DRAFT_ENTRY_KIND,
            actor=by,
            text=attempt.outbound.text,
            detail={**recorded, **verdict},
        )
        return DraftOutcome(attempt, len(filters), edited, entry=entry)
    if decision.send is None:
        reason = decision.diverted
        verdict = {"decision": "divert", "reason": reason}
    else:
        reason = f"send failed: {attempt.failure}"
        verdict = {"decision": "send", "error": attempt.failure}
    entry = LedgerEntry(
        at=now,
        kind=DRAFT_ENTRY_KIND,
        actor=by,
        text=decision.send.text if decision.send is not None else body,
        detail={**recorded, **verdict},
    )
    kept = make_draft(
        at=now,
        outcome=purpose,
        recipient=recipient,
        ref=ref,
        text=body,  # the gate adds the mention again, for the handle of that day
        reason=reason,
        notes=decision.notes,
        title=headline,
    )
    return DraftOutcome(attempt, len(filters), edited, entry=entry, kept=kept)
