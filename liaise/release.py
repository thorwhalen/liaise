"""Releasing a message: through the gate, then through correspond, as one step.

Every message liaise sends goes through :func:`gate_and_send`. That covers what the tick
sends for a run's outcomes, the tick's own notices, a message an agent sends outside any
case (``liaise message send``), and a draft the operator releases. It asks correspond who
can read the destination right then (:func:`audience_of`, never cached), runs
:func:`liaise.gate.run_gate` with that audience on the context, and only a message the gate
passed reaches ``correspond.send``, as the filters left it. It records nothing in the
ledger: what a :class:`SendAttempt` means for a case, a message or a notification is for its
caller to keep. Once a message has gone out, each recipient's acquaint record is told which
labelled records it identified (:func:`liaise.outbound.record_disclosure`).

**Releasing a held message.** :func:`release_draft` is the one way a message held for the
operator goes out, whether it waits on a case (``liaise case send-draft``) or outside any
(``liaise message send-draft``). It checks what must stop a release, computes the audience
afresh, and runs :func:`gate_and_send` with the operator's :class:`~liaise.model.Approval`
on the context. That approval is bound to the payload hash and the audience hash of the
decision the operator was shown, so a text, a title or a readership that changed since
voids it, and the operator sees the new verdict instead of a send (liaise ADR 0002). It
hands back the ledger entry and the draft to keep, for the caller to record.

**Never twice.** Every real send carries an idempotency key (correspond's
``idempotency_key``), kept in liaise's own store of sends (:func:`default_send_store`,
under ``state_dir``). A channel that posts and then reports an error leaves its key
claimed, so the release of the draft it became, which reuses the key, either finds the
message already out (read back, or a replay of the stored result) and posts nothing, or
fails ``unconfirmed``. An ``unconfirmed`` draft stays with the operator, whose release
with ``new_attempt`` (``send-draft --new-attempt``, after checking the conversation) moves
it to the next key (:func:`next_attempt_key`). The keys: a tick's send
``<case>/<payload hash>/<entry serial>`` (:func:`tick_send_key`), an outbox release
``<case>/<held id>`` (:func:`outbox_key`), a message outside a case its id, and a draft the
key it carries (``send_key``), or one derived from it (:func:`draft_send_key`).

One path for every sender is what makes the gate a gate. A filter added to it applies to
all of them at once, and none of them has a way to send around it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from hashlib import sha256
import os
from pathlib import Path
import re
import warnings
from typing import Any, Optional, Union

import correspond
from correspond.channels.github import REF_RE
from correspond.idempotency import FAILED, MAX_KEY_LENGTH
from correspond.model import Audience, ConversationRef, Draft
from correspond.stores import SendStore

from liaise.gate import (
    DFLT_OUTBOUND_FILTERS,
    GateContext,
    GateDecision,
    Outbound,
    OutboundFilter,
    approval_for,
    run_gate,
)
from liaise.holds import blocking_hold, scopes_for
from liaise.ledger import Ledger
from liaise.model import Approval, Case, LedgerEntry
from liaise.outbound import case_provenance, record_disclosure
from liaise.outcomes import HELD_REASON_PREFIX, make_draft
from liaise.policy import Provenance, canonical_json
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
#: The provenance of a message outside a case: its sender's reading is nobody's to vouch for.
CASELESS_PROVENANCE = "a message outside a case: nobody can say what its sender read"
#: Why a passed message with attachments is not sent: correspond's send takes none yet.
NO_ATTACHMENTS = (
    "correspond.send takes no attachments, so a message with some is not sent"
)
#: The failure kind of a message liaise refused to hand to its channel as it stands.
VALIDATION_KIND = "validation"
#: correspond's failure kind for a send whose key an earlier attempt claimed, which may
#: have gone out: nothing was sent, and the operator checks the conversation.
UNCONFIRMED_KIND = "unconfirmed"
#: The directory of liaise's idempotency records under ``state_dir``.
DFLT_SENDS_SUBDIR = "sends"
#: How many hex digits of a digest a key keeps: enough to tell messages apart.
KEY_DIGEST_DIGITS = 16
#: What ends a key that is not its message's first attempt: ``~2``, ``~3``, ...
ATTEMPT_SUFFIX_RE = re.compile(r"~(\d+)$")
#: Room a key keeps for attempt suffixes (``~NN``) under correspond's length limit.
ATTEMPT_ROOM = 8
#: How many characters of a too-long key's head :func:`bounded_key` keeps, for reading.
KEY_HEAD_CHARS = 60
#: The first attempt suffix :func:`next_attempt_key` adds.
FIRST_RETRY = 2
#: Why a send whose earlier attempt may have gone out is kept for the operator.
UNCONFIRMED_REASON = (
    "send unconfirmed: an earlier attempt at this message may have gone out ({failure}). "
    "Check {ref}; if it is not there, release it with send-draft --new-attempt"
)


def error_text(error: BaseException) -> str:
    """How liaise names an exception it recovered from: its class, then its message.

    >>> error_text(ValueError("no such channel"))
    'ValueError: no such channel'
    """
    return f"{type(error).__name__}: {error}"


def default_send_store(state_dir: Union[str, os.PathLike]) -> SendStore:
    """liaise's idempotency records: correspond's ``SendStore`` in ``<state_dir>/sends``.

    Nothing is created until a real send claims a key, so a dry run leaves no directory.
    """
    return SendStore(Path(state_dir).expanduser() / DFLT_SENDS_SUBDIR)


def _digest(*parts: Any) -> str:
    return sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()[
        :KEY_DIGEST_DIGITS
    ]


def bounded_key(key: str) -> str:
    """``key``, or, when too long for correspond (a long subject slug), its head and a digest of it.

    >>> bounded_key("c/h1")
    'c/h1'
    >>> len(bounded_key("s" * 200 + "-1/h1")) <= 128
    True
    """
    if len(key) <= MAX_KEY_LENGTH - ATTEMPT_ROOM:
        return key
    return f"{key[:KEY_HEAD_CHARS]}/{sha256(key.encode('utf-8')).hexdigest()[:32]}"


def tick_send_key(
    case_id: str,
    ref: Optional[str],
    text: str,
    title: Optional[str],
    serial: Union[int, str],
) -> str:
    """The key of a message a tick sends: ``<case>/<payload digest>/<entry serial>``.

    ``serial`` is how many entries the case had when the message was judged, so two sends
    of the same text on one case differ, while a tick that runs again over the same
    entries (after a crash) reuses the key and never posts twice.

    >>> key = tick_send_key("app3", "github:example/app#3", "Fixed.", None, 7)
    >>> key.startswith("app3/") and key.endswith("/7")
    True
    """
    return bounded_key(f"{case_id}/{_digest(ref, text, title)}/{serial}")


def outbox_key(case_id: str, held: str) -> str:
    """The key of an outbox item's release: ``<case>/<held id>``, the same on every try.

    >>> print(outbox_key("example", "h3f9a0c12"))
    example/h3f9a0c12
    """
    return bounded_key(f"{case_id}/{held}")


def draft_send_key(owner: str, draft: Mapping[str, Any]) -> str:
    """The key ``draft`` is released under: the one it carries (``send_key``), else one from its content.

    ``owner`` is the case or message id it belongs to. A draft kept before keys existed
    gets ``<owner>/d<digest of its time, destination, text and title>``, and its release
    stores that key on whatever draft stays, so a second release reuses it.
    """
    if draft.get("send_key"):
        return bounded_key(str(draft["send_key"]))
    digest = _digest(
        draft.get("at"), draft.get("ref"), draft.get("text"), draft.get("title")
    )
    return bounded_key(f"{owner}/d{digest}")


def next_attempt_key(key: str) -> str:
    """``key`` for the message's next attempt: ``~2`` added, or its attempt number raised.

    >>> print(next_attempt_key("example/h3f9a0c12"), next_attempt_key("example/h3f9a0c12~2"))
    example/h3f9a0c12~2 example/h3f9a0c12~3
    """
    found = ATTEMPT_SUFFIX_RE.search(key)
    if found is None:
        return f"{key}~{FIRST_RETRY}"
    return f"{key[: found.start()]}~{int(found.group(1)) + 1}"


def usable_key(key: str, sends: Optional[Mapping[str, Any]]) -> str:
    """``key``, or a later attempt's when the platform refused every try with it so far.

    A key whose record says ``failed`` posted nothing, so a new attempt is safe, and a
    fresh key lets the message change (an operator's edit) without being refused as a key
    used for another message. A key with no record, or one that sent or may have, is kept.
    """
    if sends is None:
        return key
    while True:
        try:
            record = sends.get(key)
        except (KeyError, OSError, ValueError):
            return key
        if not record or record.get("state") != FAILED:
            return key
        key = next_attempt_key(key)


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
    gate diverted has none of the three. ``disclosure_failure`` says why a sent message's
    disclosure could not be written to its recipients' acquaint records; None otherwise.
    ``key`` is the idempotency key the send carried, if any.
    """

    decision: GateDecision
    result: Optional[Any] = None
    failure: Optional[str] = None
    failure_kind: Optional[str] = None
    disclosure_failure: Optional[str] = None
    key: Optional[str] = None

    @property
    def outbound(self) -> Optional[Outbound]:
        """The message as the gate's filters left it, or None when the gate diverted it."""
        return self.decision.send

    @property
    def sent(self) -> bool:
        """Whether the channel took the message; in a dry run, whether it would have."""
        return self.decision.send is not None and self.failure is None

    @property
    def unconfirmed(self) -> bool:
        """Whether nothing was sent because an earlier attempt with the key may have gone out.

        That is correspond's ``unconfirmed``, and its refusal of a key an earlier attempt
        used for a different text (a message edited since): either way the operator checks
        the conversation before a new attempt.
        """
        if self.failure_kind == UNCONFIRMED_KIND:
            return True
        plan = getattr(self.result, "plan", None) or {}
        return (
            self.key is not None
            and self.failure_kind == VALIDATION_KIND
            and plan.get("idempotency_key") == self.key
        )

    @property
    def replayed(self) -> bool:
        """Whether the message went out on an earlier attempt, and this one posted nothing."""
        plan = getattr(self.result, "plan", None) or {}
        return self.sent and "idempotency" in plan


def audience_of(
    outbound: Outbound, *, registry: Optional[Mapping[str, Any]] = None
) -> Audience:
    """Who can read ``outbound``'s destination, asked of its channel now through correspond.

    The draft counts where its channel makes it count (an email's copies). It never
    raises: correspond resolves what it cannot compute to public, and so does anything that
    fails before it can ask. Nothing is cached, so a release asks again at send time.
    """
    try:
        draft = Draft(
            text=outbound.text, title=outbound.title, cc=outbound.cc, bcc=outbound.bcc
        )
        return correspond.audience(outbound.ref, draft, registry=registry)
    except Exception as error:  # nobody could ask: unknown, so public
        return Audience.unknown(
            str(outbound.ref), f"computing the audience failed: {error_text(error)}"
        )


def gate_and_send(
    outbound: Outbound,
    ctx: GateContext,
    *,
    registry: Optional[Mapping[str, Any]] = None,
    dry_run: bool = False,
    outbound_filters: Iterable[OutboundFilter] = DFLT_OUTBOUND_FILTERS,
    idempotency_key: Optional[str] = None,
    sends: Optional[MutableMapping[str, Any]] = None,
) -> SendAttempt:
    """Put ``outbound`` through the gate and, only when it passes, send it through correspond.

    When the context carries no audience, it is computed now (:func:`audience_of`). The
    gate is :func:`liaise.gate.run_gate` with ``outbound_filters``, and a message it holds
    back is not sent. A passed message goes to ``correspond.send`` as the filters left it,
    its title and copies included, on ``registry`` (correspond's own when None); one with
    attachments is not, since correspond sends none. ``dry_run`` asks correspond for its
    plan and sends nothing. A channel that refuses the message, or raises, becomes a
    ``failure`` on the attempt rather than an exception, so the caller still has the
    message to keep. Once a real send succeeds, the recipients' acquaint records are told
    what it identified, and a failure there is ``disclosure_failure``, never a raise.

    ``idempotency_key`` and ``sends`` go to ``correspond.send``: the same key never posts
    twice, and an attempt that may have gone out fails ``unconfirmed``
    (:attr:`SendAttempt.unconfirmed`). The key is used only with a ``sends`` store
    (:func:`default_send_store` for the tick and the CLI): without one the message is sent
    unkeyed, as before, and liaise never writes correspond's own data root.
    """
    if ctx.audience is None:
        ctx = replace(ctx, audience=audience_of(outbound, registry=registry))
    decision = run_gate(outbound, ctx, outbound_filters=outbound_filters)
    passed = decision.send
    if passed is None:
        return SendAttempt(decision)
    key = idempotency_key if sends is not None else None
    if passed.attachments:
        return SendAttempt(
            decision, failure=NO_ATTACHMENTS, failure_kind=VALIDATION_KIND, key=key
        )
    try:
        result = correspond.send(
            passed.ref,
            passed.text,
            title=passed.title,
            cc=passed.cc,
            bcc=passed.bcc,
            dry_run=dry_run,
            registry=registry,
            idempotency_key=key,
            sends=sends,
        )
    except Exception as error:  # an unknown channel, an adapter that raised
        return SendAttempt(
            decision,
            failure=error_text(error),
            failure_kind=type(error).__name__,
            key=key,
        )
    if result.ok:
        failure = None
        if not dry_run:
            try:
                failure = record_disclosure(
                    passed, decision.verdict, decision.consulted
                )
            except Exception as error:  # the message went out: report, never raise
                failure = error_text(error)
        return SendAttempt(decision, result=result, disclosure_failure=failure, key=key)
    return SendAttempt(
        decision,
        result=result,
        failure=result.error or DFLT_REFUSAL,
        failure_kind=result.error_kind,
        key=key,
    )


def failure_reason(attempt: SendAttempt, ref: Optional[str]) -> str:
    """Why a message the gate passed, and its channel did not take, is kept for the operator.

    An :attr:`~SendAttempt.unconfirmed` one says to check ``ref`` before a new attempt.
    """
    if attempt.unconfirmed:
        return UNCONFIRMED_REASON.format(failure=attempt.failure, ref=ref)
    return f"send failed: {attempt.failure}"


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
            f' Take it off with {reject} --reason "sent, not recorded".'
            if reject
            else ""
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
    ``approval`` is the approval the gate was given, bound to what the operator was shown.
    """

    attempt: SendAttempt
    filters: int
    edited: bool
    entry: Optional[LedgerEntry] = None
    kept: Optional[dict[str, Any]] = None
    approval: Optional[Approval] = None


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
    approval: Optional[Approval] = None,
    approve_shown: bool = False,
    justification: str = "",
    fingerprint_key: Union[bytes, Callable[[], bytes], None] = None,
    sends: Optional[MutableMapping[str, Any]] = None,
    new_attempt: bool = False,
) -> DraftOutcome:
    """Release ``draft`` (a :func:`liaise.outcomes.make_draft` item) as ``by``, through the gate.

    The message is the draft's text and title, or ``text`` and ``title`` when the operator
    edited them. It goes to the draft's ``ref``, for its ``recipient``, carrying out its
    ``outcome``, on the case ``case`` or outside any when that is None. The audience is
    asked of its channel now. The provenance is the case's (see
    :func:`liaise.outbound.case_provenance`), or unknown outside a case.

    ``approval`` is the operator's :class:`~liaise.model.Approval` of the decision they
    were shown, bound to its hashes and to what that verdict flagged
    (:func:`liaise.gate.approval_for`), as ``liaise case send-draft`` passes it after asking
    at a terminal. It must be ``by``'s. The message passes through :func:`gate_and_send`
    with it on the context: what it binds to and names is settled, and every other concern
    holds, a ``refuse`` always.

    **Without an approval, nothing is settled**: a draft the gate holds back stays held,
    even for a caller who says who releases it. ``approve_shown`` is how a caller that
    shows the operator a decision and asks them releases it: the gate judges the message as
    it stands, and the approval is ``by``'s of exactly that decision, with
    ``justification``. Nothing else in this package sets it; ``liaise case send-draft``
    does, on the dry run it shows, and then sends what the operator answered to.
    ``fingerprint_key`` is as :class:`~liaise.gate.GateContext` has it.

    It asks no one and records nothing. Its caller records the outcome's ``entry`` (with
    ``detail`` added to it) and, when nothing went out, its ``kept`` draft. The kept draft
    holds the operator's text without the mention the gate adds, the new reason and what
    the gate decided. ``send=False`` asks the channel only for its plan. A dry run judges
    and plans as a send would. ``label`` names the message in errors, and ``reject`` is the
    command that takes it off.

    Raises ``ValueError``, sending nothing, for any of these:

    - a draft with no destination, or no text;
    - a ``deliver`` message a hold kept, whose delivery never ran;
    - a hold on the subject, the recipient, the repository, the checkout or, for a
      ``deliver`` message, the delivery, that keeps effects waiting;
    - an ``approval`` given by someone other than ``by``.
    """
    ref, recipient, purpose = (
        draft.get(key) for key in ("ref", "recipient", "outcome")
    )
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

    if approval is not None and approval.by != by:
        raise ValueError(
            f"{label} carries an approval by {approval.by}, and is released by {by}: an "
            f"approval releases a message only for the one who gave it"
        )
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
    context = GateContext(
        subject=subject,
        case=case,
        now=now,
        audience=audience_of(outbound, registry=registry),
        provenance=(
            case_provenance(case, subject, ledger)
            if case is not None
            else Provenance.unknown(CASELESS_PROVENANCE)
        ),
        fingerprint_key=fingerprint_key,
    )
    if sends is None and send and not dry_run:
        warnings.warn(
            f"{label} is released without a store of sends, so it is sent unkeyed: an "
            f"earlier attempt that went out would be posted again. Pass sends="
            f"default_send_store(state_dir).",
            stacklevel=2,
        )
    if approval is None and approve_shown:
        shown = run_gate(outbound, context, outbound_filters=filters)
        approval = approval_for(shown, by=by, at=now, justification=justification)
    key = draft_send_key(case.id if case is not None else label, draft)
    key = usable_key(next_attempt_key(key) if new_attempt else key, sends)
    attempt = gate_and_send(
        outbound,
        replace(context, approval=approval),
        registry=registry,
        dry_run=dry_run or not send,
        outbound_filters=filters,
        idempotency_key=key,
        sends=sends,
    )
    decision = attempt.decision
    edited = body != (draft.get("text") or "") or headline != draft.get("title")
    if attempt.sent and not send and not dry_run:  # a plan: nothing to record
        return DraftOutcome(attempt, len(filters), edited, approval=approval)
    recorded = {
        "purpose": purpose,
        "ref": ref,
        "notes": list(decision.notes),
        **detail,
        "held_for": draft.get("reason"),
        "edited": edited,
        **decision.record(),
    }
    recorded["send_key"] = key
    if attempt.sent:
        url = getattr(attempt.result, "url", None)
        verdict = {"decision": "send", "url": url}
        if attempt.replayed:
            verdict["replayed"] = True
        if attempt.disclosure_failure:
            verdict["disclosure_failure"] = attempt.disclosure_failure
        entry = LedgerEntry(
            at=now,
            kind=DRAFT_ENTRY_KIND,
            actor=by,
            text=attempt.outbound.text,
            detail={**recorded, **verdict},
        )
        return DraftOutcome(
            attempt, len(filters), edited, entry=entry, approval=approval
        )
    if decision.send is None:
        reason = decision.diverted
        verdict = {"decision": "divert", "reason": reason}
    else:
        reason = failure_reason(attempt, ref)
        verdict = {"decision": "send", "error": attempt.failure}
        if attempt.unconfirmed:
            verdict["unconfirmed"] = True
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
        gate=decision.summary() if decision.send is None else None,
        send_key=key,
    )
    return DraftOutcome(
        attempt, len(filters), edited, entry=entry, kept=kept, approval=approval
    )
