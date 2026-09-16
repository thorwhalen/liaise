"""The outbound gate: the checks every message passes before liaise sends it, and the verdict they reach.

A processor run reports outcomes, :mod:`liaise.outcomes` plans them into actions, and each
:class:`~liaise.outcomes.Send` among those is an :class:`Outbound` that the tick hands to
:func:`run_gate` before anything reaches a channel. A message outside a case, and a draft
the operator releases, pass the same gate. It runs :data:`DFLT_OUTBOUND_FILTERS`, in this
order:

1. :func:`outside_a_case`: a message outside any case waits for the operator: its sender
   chose where it goes and to whom (liaise #28).
2. :func:`outbound_policy`: the policy of liaise discussion 32. Who can read the
   destination (the audience on the context), what each reader may be told (the
   disclosure), what the message holds (the detectors, over its text, title and attachment
   names) and what the run that wrote it read (the provenance on the context), through the
   rule table of :mod:`liaise.policy`. Draft reply mode is a row of that table. It
   replaces 0.1's leak scan.
3. :func:`writing_card`: a note with the recipient's acquaint writing card.
4. :func:`deslop`: nothing acquaint's style lint finds machine-sounding.
5. :func:`notify_recipient`: on GitHub, the message starts with ``@<login>``, since
   GitHub notifies only the people a comment mentions.

**Every filter runs** (liaise ADR 0002, which amends ADR 0001's "the first divert ends the
gate"). A filter is ``(outbound, ctx) -> Pass | Divert``. A :class:`Divert` says how far
the message must be held back: its ``flow``, one of :data:`liaise.policy.FLOWS`
(``approve`` when it does not say). Each divert is one or more :class:`Concern` records,
the policy's one per rule that fired. The decision's flow is the most restrictive concern
still standing, and its reasons are all of them, most restrictive first. A message goes out
only when that flow is ``send``: ``delay`` waits for the operator until the delay outbox
exists (liaise #38). A filter that raises, or answers anything but a ``Pass`` or a
``Divert``, contributes an ``approve`` concern with the error as its reason. The order stays
fixed and is not a seam: the mention, the one rewrite, comes last, so every filter judges
the text as it was written, and the rewrite reaches a send only when nothing held it back.

**Approvals** (discussion §5.7). The operator's :class:`~liaise.model.Approval` on
:attr:`GateContext.approval` is bound to the message, the audience and the verdict it was
given for: the hashes of the message the filters judged and of the audience on the context
(:func:`liaise.policy.payload_hash`, :func:`liaise.policy.audience_hash`), and the name of
what that verdict flagged (:func:`liaise.outbound.verdict_id`). While all three still hold,
it settles each concern whose rule it names and whose flow is at most ``approve``. A
``refuse`` is never settled, nor is a concern with no rule (deslop, a missing handle, a
filter that failed). An approval that no longer binds settles nothing, and is itself the
first concern the operator reads, naming what changed — a widened audience, an edited text,
or a disclosure that now flags something else under the same rule. :func:`approval_for`
makes the approval for a decision the operator was shown.

The gate only decides. :func:`liaise.release.gate_and_send` computes the audience, runs the
gate, and sends :attr:`GateDecision.send`; its callers keep a diverted message as a draft
(see :func:`liaise.outcomes.make_draft`) and record :meth:`GateDecision.record`. acquaint is
optional (``liaise[people]``): without it the disclosure has every reader at
``need-to-know`` and the subject's ``leak_terms`` as its vocabulary, and filters 3 and 4
add a note and let the message through.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Optional, Union

from liaise.access import Resolver, resolve_person
from liaise.detect import DFLT_DETECTORS, Detector, Finding
from liaise.model import Approval, Case
from liaise.outbound import (
    DisclosureSource,
    Judgement,
    acquaint_disclosure,
    audience_snapshot,
    judge,
    verdict_id,
)
from liaise.policy import payload_of
from liaise.policy import (
    APPROVE,
    DELAY,
    FLOWS,
    SEND,
    Provenance,
    Verdict,
    audience_hash,
    audience_in_words,
    flow_rank,
    most_restrictive,
    payload_hash,
)
from liaise.subjects import Subject

#: The rule :func:`outside_a_case` holds a message for, which an approval names to release it.
OUTSIDE_A_CASE = "a message outside a case"
CASELESS_REASON = f"{OUTSIDE_A_CASE} waits for the operator"
#: The channel whose messages must @mention their recipient to reach them.
MENTION_CHANNEL = "github"
#: What a decision held back as ``delay`` says, until the outbox (liaise #38) exists.
DELAY_HELD = (
    "a delay is held for the operator until the delay outbox exists (liaise #38)"
)
#: The name the gate files its own concerns under: a void approval, a message it cannot hash.
GATE_CONCERN = "gate"
#: A GitHub login: letters, digits and hyphens, at most 39 characters.
_GITHUB_LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")


@dataclass(frozen=True, kw_only=True)
class Outbound:
    """A message liaise would send: ``text`` for ``recipient`` (a person id) at ``ref``.

    ``ref`` is the encoded conversation or address it goes to (``github:example/app#12``,
    or ``github:example/app`` to open an issue there), and ``channel`` is that ref's
    channel. ``purpose`` is the outcome kind it carries out (``ask``, ``reply``,
    ``propose``, ``deliver``). ``title`` is the title of the issue it opens, when it opens
    one. ``case_id`` is the case the message belongs to, or None for a message an agent
    sends outside any case (``liaise message send``). ``cc`` and ``bcc`` are further
    recipients (addresses), on channels that have them; ``attachments`` are the names of
    attached files, and ``project`` the project the message is about, when one is named.
    The policy judges every one of these, and the payload hash covers all but ``project``.
    """

    ref: str
    channel: str
    recipient: str
    purpose: str
    text: str
    title: Optional[str] = None
    case_id: Optional[str] = None
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    attachments: tuple[str, ...] = ()
    project: Optional[str] = None


@dataclass(frozen=True, kw_only=True)
class GateContext:
    """What the filters may consult about one message.

    ``subject`` is the subject whose policy applies, ``now`` the time of the decision, and
    ``case`` the case the message belongs to (None outside any). ``audience`` is
    correspond's record of who can read the destination, computed right before the gate
    runs (None: unknown, so public). ``provenance`` is what the run that wrote the message
    read (None: unknown, so tainted). ``mode`` overrides the subject's ``policy.mode``.
    ``approval`` is the operator's release of this message, None for every message sent
    without one. ``fingerprint_key`` is the key findings are fingerprinted with (None: the
    one in the configured state directory).
    """

    subject: Subject
    now: datetime
    case: Optional[Case] = None
    approval: Optional[Approval] = None
    audience: Optional[Any] = None
    provenance: Optional[Provenance] = None
    mode: Optional[str] = None
    fingerprint_key: Union[bytes, Callable[[], bytes], None] = None


@dataclass(frozen=True)
class Pass:
    """A filter's verdict to go on, with ``outbound`` as the filter left it.

    ``judgement`` is the policy's, when the filter is the policy.
    """

    outbound: Outbound
    notes: tuple[str, ...] = ()
    judgement: Optional[Judgement] = None


@dataclass(frozen=True)
class Divert:
    """A filter's verdict to hold the message back: ``reason``, and how far (``flow``).

    ``flow`` is one of :data:`liaise.policy.FLOWS` other than ``send``; a divert that does
    not say is ``approve``, a flagged draft for the operator. ``findings`` are what it
    found. ``rule`` names what an approval may settle it by; a divert without one is
    settled only by changing the message. ``judgement`` is the policy's, whose rules become
    the concerns.
    """

    reason: str
    notes: tuple[str, ...] = ()
    flow: str = APPROVE
    findings: tuple[Finding, ...] = ()
    rule: Optional[str] = None
    judgement: Optional[Judgement] = None


#: ``(outbound, ctx) -> Pass | Divert``: one check of the gate.
OutboundFilter = Callable[[Outbound, GateContext], Union[Pass, Divert]]


def binds(
    approval: Optional[Approval],
    hashes: tuple[Optional[str], Optional[str]],
    verdict: Optional[Verdict],
) -> bool:
    """Whether ``approval`` was given for this message, this audience and this verdict.

    The one rule the gate settles by, so what a decision records as bound is what its
    concerns were judged by: both hashes as the filters computed them, and the name of
    what the verdict flags (:func:`liaise.outbound.verdict_id`), which is ``None`` when no
    policy judged the message.
    """
    if approval is None or None in hashes:
        return False
    shown = None if verdict is None else verdict_id(verdict)
    return approval.binds(*hashes) and approval.verdict_id == shown


@dataclass(frozen=True, kw_only=True)
class Concern:
    """One reason the gate holds a message back: the filter, the rule, how far, and why.

    ``text`` is what the operator reads, and never holds a matched value. ``rule`` is None
    for a concern no approval settles.
    """

    filter: str
    flow: str
    text: str
    rule: Optional[str] = None
    findings: tuple[Finding, ...] = ()

    @property
    def settleable(self) -> bool:
        """Whether an approval naming its rule settles it: a rule, and a flow at most ``approve``."""
        return self.rule is not None and flow_rank(self.flow) <= flow_rank(APPROVE)

    def to_dict(self) -> dict:
        """JSON-ready."""
        return {
            "filter": self.filter,
            "flow": self.flow,
            "rule": self.rule,
            "text": self.text,
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class GateDecision:
    """What :func:`run_gate` decided: ``send`` a message, or why it is ``diverted``.

    Exactly one of ``send`` (the message as the filters left it) and ``diverted`` (every
    standing concern's text, most restrictive first) is set. ``flow`` is the decision's,
    ``concerns`` what still holds the message back and ``settled`` what the approval
    released it past. ``notes`` holds every filter's notes, in order. ``diverted_by`` names
    the filter of the most restrictive concern, as an operator notification may say it: the
    reason can quote what a filter raised. ``verdict`` and ``consulted`` are the policy's
    verdict and what it consulted. ``approval`` is the one on the context, and
    ``payload_hash`` and ``audience_hash`` what it had to match.
    """

    send: Optional[Outbound]
    diverted: Optional[str]
    notes: tuple[str, ...] = ()
    diverted_by: Optional[str] = None
    flow: str = SEND
    concerns: tuple[Concern, ...] = ()
    settled: tuple[Concern, ...] = ()
    verdict: Optional[Verdict] = None
    consulted: Mapping[str, Any] = field(default_factory=dict)
    approval: Optional[Approval] = None
    payload_hash: Optional[str] = None
    audience_hash: Optional[str] = None

    @property
    def bound(self) -> bool:
        """Whether the approval binds to this message, this audience and this verdict."""
        return binds(
            self.approval, (self.payload_hash, self.audience_hash), self.verdict
        )

    @property
    def overridable(self) -> tuple[str, ...]:
        """The rules of the standing concerns an approval could settle, each once."""
        return tuple(
            dict.fromkeys(c.rule for c in self.concerns if c.settleable and c.rule)
        )

    @property
    def audience_words(self) -> Optional[str]:
        """The audience the policy judged, in words; None when the policy did not run."""
        if self.verdict is None:
            return None
        return audience_in_words(self.verdict.audience)

    def summary(self) -> dict:
        """What a held draft keeps of the decision: the flow, the audience in words, the reasons."""
        return {
            "flow": self.flow,
            "audience": self.audience_words,
            "reasons": [concern.text for concern in self.concerns],
        }

    def record(self) -> dict:
        """What a ledger ``gate`` entry records of the decision (discussion §5.7).

        The flow and every concern with its findings (kinds, positions and fingerprints,
        never the value), what the approval settled, the policy's verdict (its audience
        snapshot, the readers' tiers and clearances, the mode), the labels, seals and
        provenance consulted, and the approval with whether it bound.
        """
        return {
            "flow": self.flow,
            "concerns": [concern.to_dict() for concern in self.concerns],
            "settled": [concern.to_dict() for concern in self.settled],
            "verdict": None if self.verdict is None else self.verdict.to_dict(),
            "consulted": dict(self.consulted),
            "approval": None if self.approval is None else self.approval.to_dict(),
            "approval_bound": None if self.approval is None else self.bound,
            "payload_hash": self.payload_hash,
            "audience_hash": self.audience_hash,
        }


def approval_for(
    decision: GateDecision, *, by: str, at: datetime, justification: str = ""
) -> Approval:
    """The approval of ``by``, at ``at``, of the message and audience ``decision`` judged.

    It overrides every concern of the decision an approval can settle, so the operator must
    have been shown ``decision``: its hashes bind the approval to exactly that message and
    audience.
    """
    return Approval(
        by=by,
        at=at,
        payload_hash=decision.payload_hash,
        audience_hash=decision.audience_hash,
        verdict_id=None if decision.verdict is None else verdict_id(decision.verdict),
        justification=justification.strip(),
        rules_overridden=decision.overridable,
    )


def _acquaint_failure(error: Exception) -> str:
    if isinstance(error, ImportError):
        return f"acquaint could not be imported ({error}); install liaise[people]"
    return f"{type(error).__name__}: {error}"


# ---- the filters, in their default order ----


def outside_a_case(outbound: Outbound, ctx: GateContext) -> Union[Pass, Divert]:
    """Hold a message outside any case (``ctx.case`` None) for the operator, whatever its reply mode.

    Its sender chose where it goes and to whom, so a sender who picks a person in
    ``direct`` mode must not reach an audience that way (discussion §6.2). An approval
    naming :data:`OUTSIDE_A_CASE`, bound to the message, releases it.
    """
    if ctx.case is not None:
        return Pass(outbound)
    return Divert(CASELESS_REASON, rule=OUTSIDE_A_CASE)


def outbound_policy(
    outbound: Outbound,
    ctx: GateContext,
    *,
    disclosure: DisclosureSource = acquaint_disclosure,
    detectors: Iterable[Detector] = DFLT_DETECTORS,
    resolver: Resolver = resolve_person,
) -> Union[Pass, Divert]:
    """Hold back what the outbound policy (discussion §5.4) does not let go now.

    The audience is the context's (unknown, so public, when it has none), the disclosure
    comes through ``disclosure`` (acquaint's, or every reader at ``need-to-know`` without
    it), and the provenance is the context's (unknown, so tainted, when it has none). See
    :func:`liaise.outbound.judge`. A ``send`` verdict passes, noting the audience; any
    other diverts at its flow, one concern per rule that fired. It never redacts: what it
    found is for the operator to fix, and its reasons say where, never what.
    """
    judgement = judge(
        outbound,
        subject=ctx.subject,
        now=ctx.now,
        audience=ctx.audience,
        provenance=ctx.provenance,
        mode=ctx.mode,
        key=ctx.fingerprint_key,
        disclosure=disclosure,
        detectors=detectors,
        resolver=resolver,
    )
    verdict = judgement.verdict
    if verdict.flow == SEND:
        return Pass(outbound, notes=judgement.notes, judgement=judgement)
    return Divert(
        verdict.reasons[0].text,
        notes=judgement.notes,
        flow=verdict.flow,
        findings=verdict.findings,
        judgement=judgement,
    )


def writing_card(outbound: Outbound, ctx: GateContext) -> Pass:
    """Note the recipient's acquaint writing card, for the ledger and the next run.

    Calls ``acquaint.brief(recipient, purpose=purpose)`` and notes its summary. It never
    diverts and never raises: without acquaint, or when acquaint fails (an unknown
    person raises ``AcquaintError``), the note says why the card is unavailable.
    """
    try:
        import acquaint

        card = acquaint.brief(outbound.recipient, purpose=outbound.purpose)
        summary = card.get("summary") or f"brief for {outbound.recipient}"
    except Exception as error:  # acquaint missing, or failing for this person
        note = f"writing card unavailable: {_acquaint_failure(error)}"
        return Pass(outbound, notes=(note,))
    return Pass(outbound, notes=(f"writing card: {summary}",))


def deslop(outbound: Outbound, ctx: GateContext) -> Union[Pass, Divert]:
    """Divert a message acquaint's style lint finds machine-sounding for its recipient.

    Calls ``acquaint.style_lint(text, recipient=recipient)``. When that is not ``ok``,
    the message is diverted, with each enforced finding as a note. Without acquaint, or
    when acquaint fails, the note says why and the message goes on. It never raises.
    """
    try:
        import acquaint

        lint = acquaint.style_lint(outbound.text, recipient=outbound.recipient)
        ok = bool(lint["ok"])
        findings = tuple(
            f"deslop: {finding['tier']} {finding['rule']}: {finding['message']}"
            + (f" (…{finding['excerpt']}…)" if finding.get("excerpt") else "")
            for finding in lint.get("findings", ())
            if finding.get("enforced")
        )
    except Exception as error:  # acquaint missing, or failing for this person
        note = f"deslop unavailable: {_acquaint_failure(error)}"
        return Pass(outbound, notes=(note,))
    if ok:
        return Pass(outbound)
    return Divert(f"deslop: {len(findings)} enforced finding(s)", notes=findings)


def notify_recipient(outbound: Outbound, ctx: GateContext) -> Union[Pass, Divert]:
    """On GitHub, make the message start with an ``@mention`` of its recipient.

    GitHub notifies only the people a comment mentions, and an issue an app files
    subscribes its partner to nothing. The login is the first valid one among the
    recipient's ``github:`` addresses, best first (see
    :meth:`~liaise.subjects.Subject.notify_addresses_for`). A missing mention is
    prefixed, the one rewrite the gate makes. A recipient with no GitHub address is
    diverted. Other channels pass unchanged.
    """
    if outbound.channel != MENTION_CHANNEL:
        return Pass(outbound)
    addresses = ctx.subject.notify_addresses_for(
        outbound.recipient, channels=MENTION_CHANNEL
    )
    logins = (address.partition(":")[2] for address in addresses)
    login = next((name for name in logins if _GITHUB_LOGIN_RE.fullmatch(name)), None)
    if login is None:
        return Divert(f"no handle to notify {outbound.recipient}")
    text = outbound.text.lstrip()
    if re.match(rf"@{re.escape(login)}(?![\w-])", text, flags=re.IGNORECASE):
        return Pass(outbound)
    mentioned = replace(outbound, text=f"@{login} {text}")
    return Pass(mentioned, notes=(f"added the mention @{login}",))


#: The gate's filters, in the order they run. The order is part of the design, not a
#: setting: the mention, the one rewrite, comes after every filter that judges the text.
DFLT_OUTBOUND_FILTERS: tuple[OutboundFilter, ...] = (
    outside_a_case,
    outbound_policy,
    writing_card,
    deslop,
    notify_recipient,
)


# ---- the gate ----


def filter_name(outbound_filter: Any) -> str:
    """How the gate names a filter: its name, the name of what a partial wraps, else its type.

    Never its repr, which can hold what a filter was bound to, such as a local path; the
    name reaches the operator's notification. A filter configured at a seam is a
    ``functools.partial``, whose own name is its arguments: its function's name is what
    tells the operator which check held their message back.
    """
    for candidate in (outbound_filter, getattr(outbound_filter, "func", None)):
        name = getattr(candidate, "__name__", None)
        if isinstance(name, str) and name:
            return name
    return type(outbound_filter).__name__


def _redirected(payload: Optional[Mapping[str, Any]], outbound: Outbound) -> bool:
    """Whether a filter's rewrite changed the message in anything but its text.

    A filter may reword a message (the mention); it may never redirect it. Where a message
    goes, who it is for and what it carries were judged by every filter before it, and are
    what the payload hash binds an approval to.
    """
    if payload is None:
        return False
    try:
        rewritten = payload_of(outbound)
    except Exception:  # a rewrite nobody can hash is not one to send
        return True
    keep = lambda fields: {k: v for k, v in fields.items() if k != "text"}  # noqa: E731
    return keep(rewritten) != keep(payload)


def _concerns_of(name: str, divert: Divert) -> list[Concern]:
    """The concerns ``divert``, from the filter ``name``, holds the message back for."""
    flow, reason = divert.flow, divert.reason
    if flow not in FLOWS or flow == SEND:
        reason = f"{reason} ({name} diverted with the flow {flow!r}, read as {APPROVE})"
        flow = APPROVE
    own = Concern(
        filter=name,
        flow=flow,
        text=reason,
        rule=divert.rule,
        findings=tuple(divert.findings),
    )
    verdict = divert.judgement.verdict if divert.judgement is not None else None
    if verdict is None:
        return [own]
    concerns = [
        Concern(
            filter=name,
            flow=r.flow,
            text=r.text,
            rule=r.rule,
            findings=() if r.finding is None else (r.finding,),
        )
        for r in verdict.reasons
        if r.flow != SEND
    ]
    strongest = most_restrictive(c.flow for c in concerns)
    if not concerns or flow_rank(flow) > flow_rank(strongest):
        concerns.append(
            replace(own, rule=None)
        )  # the divert asks for more than its rules
    return concerns


def _void(approval: Approval, hashes: tuple[Optional[str], Optional[str]]) -> Concern:
    """The concern an approval that does not bind to this message raises."""
    if approval.payload_hash is None or approval.audience_hash is None:
        what = "it is bound to no message"
    else:
        pairs = (
            ("the message", approval.payload_hash, hashes[0]),
            ("its audience", approval.audience_hash, hashes[1]),
        )
        changed = [label for label, given, now in pairs if given != now]
        # The hashes can both hold while the verdict names something else: the disclosure
        # changed under it, so what the operator released it past is not what it flags now.
        what = f"{' and '.join(changed or ['what the gate flags in it'])} changed since it was given"
    return Concern(
        filter=GATE_CONCERN,
        flow=APPROVE,
        text=(
            f"the approval by {approval.by} at {approval.at.isoformat()} is void: "
            f"{what}; the operator must judge the message again"
        ),
    )


def run_gate(
    outbound: Outbound,
    ctx: GateContext,
    *,
    outbound_filters: Iterable[OutboundFilter] = DFLT_OUTBOUND_FILTERS,
) -> GateDecision:
    """Run ``outbound`` through every one of ``outbound_filters``, in order, and decide.

    Each :class:`Pass` hands its message, possibly rewritten, to the next filter; each
    :class:`Divert` adds its concerns. An approval on the context settles what it binds to
    and names (see the module docstring). The decision's flow is the most restrictive
    concern left, and only ``send`` sends. The gate fails closed: a filter that raises, or
    returns anything but a ``Pass`` (of an :class:`Outbound`) or a ``Divert``, adds an
    ``approve`` concern naming it.
    """
    entry_payload: Optional[Mapping[str, Any]] = None
    try:
        judged_audience = audience_snapshot(ctx.audience, outbound.ref)
        entry_payload = payload_of(outbound)
        hashes: tuple[Optional[str], Optional[str]] = (
            payload_hash(outbound),
            audience_hash(judged_audience),
        )
        problem = None
    except Exception as error:  # a message or an audience nobody can hash binds nothing
        hashes, problem = (None, None), f"{type(error).__name__}: {error}"
    approval = ctx.approval if isinstance(ctx.approval, Approval) else None
    notes: list[str] = []
    concerns: list[Concern] = []
    if problem is not None:
        concerns.append(
            Concern(
                filter=GATE_CONCERN,
                flow=APPROVE,
                text=f"the message could not be hashed ({problem})",
            )
        )
    judgement: Optional[Judgement] = None
    for outbound_filter in outbound_filters:
        name = filter_name(outbound_filter)
        try:
            result = outbound_filter(outbound, ctx)
        except Exception as error:
            result = Divert(f"{name} failed: {type(error).__name__}: {error}")
        if not isinstance(result, (Pass, Divert)) or (
            isinstance(result, Pass) and not isinstance(result.outbound, Outbound)
        ):
            given = type(result.outbound if isinstance(result, Pass) else result)
            result = Divert(f"{name} returned {given.__name__}, not a Pass or a Divert")
        notes.extend(result.notes)
        if result.judgement is not None:
            judgement = result.judgement
        if isinstance(result, Pass):
            if _redirected(entry_payload, result.outbound):
                concerns.append(
                    Concern(
                        filter=name,
                        flow=APPROVE,
                        text=(
                            f"{name} changed where the message goes, or what it carries, "
                            f"not only what it says: the rewrite is dropped, since every "
                            f"filter judged the message as it stands"
                        ),
                    )
                )
            else:
                outbound = result.outbound
        else:
            concerns.extend(_concerns_of(name, result))
    bound = binds(approval, hashes, None if judgement is None else judgement.verdict)
    if approval is not None and not bound:
        concerns.insert(
            0, _void(approval, hashes)
        )  # first among its equals: it explains the rest
    settled, standing = [], []
    for concern in concerns:
        releases = bound and concern.settleable
        (
            settled
            if releases and concern.rule in approval.rules_overridden
            else standing
        ).append(concern)
    standing.sort(key=lambda concern: -flow_rank(concern.flow))  # stable: filter order
    if settled:
        rules = ", ".join(dict.fromkeys(concern.rule for concern in settled))
        why = f" ({approval.justification})" if approval.justification else ""
        at = approval.at.isoformat()
        notes.append(f"released by {approval.by} at {at}, past: {rules}{why}")
    flow = most_restrictive(concern.flow for concern in standing)
    common = dict(
        notes=tuple(notes),
        flow=flow,
        concerns=tuple(standing),
        settled=tuple(settled),
        verdict=None if judgement is None else judgement.verdict,
        consulted={} if judgement is None else dict(judgement.consulted),
        approval=approval,
        payload_hash=hashes[0],
        audience_hash=hashes[1],
    )
    if flow == SEND:
        return GateDecision(send=outbound, diverted=None, **common)
    texts = [concern.text for concern in standing]
    if flow == DELAY:
        texts.append(DELAY_HELD)
    return GateDecision(
        send=None,
        diverted="; ".join(texts),
        diverted_by=standing[0].filter,
        **common,
    )
