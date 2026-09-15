"""Cases as the operator sees and moves them: ``liaise case list``, ``show``, ``set-state``, ``send-draft`` and ``reject-draft``.

The tick moves a case through its states on its own (see :mod:`liaise.tick`), except where
a state waits on the operator: nothing the tick does moves a ``needs-owner`` case on, and a
``deployed`` case never starts again. :func:`set_case_state` is how the operator moves one,
recorded on the case as a ``transition`` entry by the operator.

A case's ``liaise:`` label on GitHub is a projection of its state in the ledger, so
relabelling an issue by hand changes nothing, and the tick overwrites it. After
:func:`set_case_state`, the label follows on the next tick.

**What a notification leaves out.** No operator notification carries anything a case holds
(see :func:`liaise.notify.notice_body`); it names the case and points at ``liaise case
show``. :func:`case_show_lines` is where the operator reads, on their own machine, the
drafts with their text, the escalation's reason and a failed deploy's output.

**Drafts.** A message liaise did not send stays on its case as a draft. It may have been
held by ``draft`` reply mode, diverted by another filter of the gate, kept by a hold,
refused by its channel, or it is an escalation's text. :func:`send_draft` is how the
operator sends one. It runs the gate again on the final text, with the operator's
:class:`~liaise.model.Approval` on the context, so draft reply mode lets it through while
every other filter still judges it, an edited text included. :func:`reject_draft` records
that the operator declined one, and why.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Optional

from correspond.model import ConversationRef

from liaise.gate import DFLT_OUTBOUND_FILTERS, GateContext, Outbound, OutboundFilter
from liaise.holds import DFLT_SET_BY, blocking_hold, scopes_for
from liaise.ledger import Ledger
from liaise.model import CASE_STATES, Approval, Case, LedgerEntry, require_one_of
from liaise.outcomes import make_draft
from liaise.processor import RUNNING
from liaise.projection import github_issue
from liaise.release import SendAttempt, error_text, gate_and_send
from liaise.subjects import Subject
from liaise.tick import HELD_REASON_PREFIX, RUN_DEPLOY_FAILED

#: Who a state set with :func:`set_case_state` is recorded as set by.
OPERATOR_ACTOR = DFLT_SET_BY
#: The reason recorded for a state the operator set without giving one.
DFLT_OPERATOR_REASON = "set by the operator"
#: States only the tick sets: ``working`` says a run is in flight, which only a start makes so.
TICK_ONLY_STATES = ("working",)
#: How the case commands print an empty or unset value.
NONE_SHOWN = "(none)"
#: How many of a case's latest entries ``liaise case show`` lists.
DFLT_SHOW_ENTRIES = 12
#: How many characters of an entry's text ``liaise case show`` puts on the entry's line.
SHOW_TEXT_CHARS = 200
#: The outcome kinds whose reason ``liaise case show`` gives as the last escalation's.
ESCALATION_KINDS = ("escalate", "decline")
#: How ``liaise case show`` indents a draft's text and a deploy's output.
TEXT_INDENT = "    "
#: The state a held message leaves its case waiting on the operator in.
NEEDS_OWNER = "needs-owner"
#: Where a case in :data:`NEEDS_OWNER` goes once the operator sends its last draft, by the
#: outcome that draft carries out: a question, a reply or a proposal now waits on the
#: reporter, as it does when a run sends one. Anything else leaves the state for the
#: operator to set. An escalation's text may be a refusal, which a reply from the partner
#: must not restart work on. A ``deliver`` message does not make its delivery happen, and
#: the tick's own notices (a nudge, the daily cap) move nothing.
STATE_AFTER_SENT_DRAFT = MappingProxyType(
    dict.fromkeys(("ask", "reply", "propose"), "needs-partner")
)
#: The outcome whose message announces a delivery.
DELIVER_PURPOSE = "deliver"
#: The entry kind a sent or rejected draft is recorded as: a gate decision, as the tick's are.
DRAFT_ENTRY_KIND = "gate"


class DraftSentNotRecorded(RuntimeError):
    """A released draft went out, and the ledger then failed to record that it did."""


def _no_case(case_id: str) -> str:
    return (
        f"no case {case_id!r} in the ledger; liaise case list shows the cases there are"
    )


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _entry_line(entry: LedgerEntry) -> str:
    """One entry on one line: when, what, by whom, its detail, and the start of its text."""
    detail = ", ".join(
        f"{key}={value}"
        for key, value in entry.detail.items()
        if value not in (None, "", [], {})
    )
    line = f"{_stamp(entry.at)} {entry.kind}" + (
        f" by {entry.actor}" if entry.actor else ""
    )
    if detail:
        line += f": {detail}"
    text = " ".join((entry.text or "").split())
    if len(text) > SHOW_TEXT_CHARS:
        text = text[: SHOW_TEXT_CHARS - 1] + "…"
    return f"{line} | {text}" if text else line


def case_lines(
    store: MutableMapping[str, Any], *, state: Optional[str] = None
) -> list[str]:
    """What ``liaise case list`` prints: ``<case id>\\t<state>\\t<conversations>`` per case.

    Every case in the ledger ``store``, or only those in ``state``, by subject and then
    oldest first. Reads only. Raises ``ValueError`` for a state outside
    :data:`~liaise.model.CASE_STATES`.
    """
    if state is not None:
        require_one_of(state, CASE_STATES, what="case state")
    cases = sorted(
        Ledger(store).cases(state=state),
        key=lambda case: (case.subject, case.created_at, case.id),
    )
    if not cases:
        return [f"(no cases in {state})" if state else "(no cases)"]
    return [
        f"{case.id}\t{case.state}\t{', '.join(case.conversations)}" for case in cases
    ]


def case_show_lines(
    store: MutableMapping[str, Any],
    case_id: str,
    *,
    entries: int = DFLT_SHOW_ENTRIES,
) -> list[str]:
    """What ``liaise case show`` prints: the case ``case_id``, with all a notification leaves out.

    Its state and conversations; the reason of its last ``escalate`` or ``decline``; its
    last failed deploy, with the tail of the command's output; each draft waiting for the
    operator, with its whole text; and its ``entries`` latest ledger entries, oldest first,
    a line each with its detail and the start of its text. Reads only. Raises
    ``ValueError`` for a case the ledger ``store`` does not hold.
    """
    case = Ledger(store).get_case(case_id)
    if case is None:
        raise ValueError(_no_case(case_id))

    def indented(text: Optional[str]) -> list[str]:
        return [f"{TEXT_INDENT}{line}" for line in (text or NONE_SHOWN).splitlines()]

    def last(kind: str, matches: Callable[[Mapping[str, Any]], bool]):
        found = (e for e in reversed(case.entries) if e.kind == kind)
        return next((entry for entry in found if matches(entry.detail)), None)

    lines = [
        f"case: {case.id}",
        f"  subject: {case.subject}",
        f"  state: {case.state}",
        f"  reporter: {case.reporter}",
        f"  conversations: {', '.join(case.conversations) or NONE_SHOWN}",
        f"  opened: {_stamp(case.created_at)}",
        f"  updated: {_stamp(case.updated_at)}",
        f"  session: {case.session_id or NONE_SHOWN}",
    ]
    if case.defer_until is not None:
        lines.append(f"  deferred until: {_stamp(case.defer_until)}")
    escalation = last("outcome", lambda detail: detail.get("kind") in ESCALATION_KINDS)
    reason = escalation.detail.get("reason") if escalation else None
    lines.append(f"last escalation reason: {reason or NONE_SHOWN}")
    deploy = last("run", lambda detail: detail.get("event") == RUN_DEPLOY_FAILED)
    if deploy is None:
        lines.append(f"last failed deploy: {NONE_SHOWN}")
    else:
        causes = (deploy.detail.get("cause"), deploy.detail.get("error"))
        cause = ", ".join(str(part) for part in causes if part) or NONE_SHOWN
        lines.append(f"last failed deploy: {_stamp(deploy.at)} ({cause}); its output:")
        lines += indented(deploy.text)
    lines.append(f"drafts waiting for the operator: {len(case.drafts)}")
    for index, draft in enumerate(case.drafts):
        to = draft.get("ref") or f"{draft.get('recipient')} (no channel)"
        lines.append(
            f"  [{index}] {draft.get('at')} {draft.get('outcome')} to {to}: "
            f"{draft.get('reason')}"
        )
        lines += indented(draft.get("text"))
    shown = case.entries[-entries:] if entries > 0 else ()
    lines.append(f"latest entries: {len(shown)} of {len(case.entries)}")
    lines += [f"  {_entry_line(entry)}" for entry in shown]
    return lines


def set_case_state(
    ledger: Ledger,
    case_id: str,
    state: str,
    *,
    reason: str = "",
    now: Optional[datetime] = None,
) -> Case:
    """Move the case ``case_id`` to ``state`` as the operator; return the case as it is now.

    The move is a ``transition`` entry whose actor is ``operator``, with ``reason`` (or
    :data:`DFLT_OPERATOR_REASON`), stamped ``now`` (the current UTC time when None). A case
    already in ``state`` is returned as it is, and nothing is recorded. Its GitHub labels
    follow on the next tick.

    Raises ``ValueError``, writing nothing, for a state outside
    :data:`~liaise.model.CASE_STATES` or in :data:`TICK_ONLY_STATES`, for a case the ledger
    does not hold, and for a case with a run in flight, whose state the tick sets when it
    collects that run.
    """
    require_one_of(state, CASE_STATES, what="case state")
    if state in TICK_ONLY_STATES:
        raise ValueError(
            f"a case cannot be set to {state}: {state} means a run is in flight, and only "
            f"the tick starts one. Set it to intake, and the tick starts a run once the "
            f"case is ready."
        )
    case = ledger.get_case(case_id)
    if case is None:
        raise ValueError(_no_case(case_id))
    in_flight = _runs_in_flight(ledger, case_id)
    if in_flight:
        raise ValueError(
            f"case {case_id} has run {in_flight[0]} in flight, and the tick sets the "
            f"case's state when it collects that run. Stop the run with a cancel hold "
            f"(liaise hold subject:{case.subject} --mode cancel), or let it finish."
        )
    if case.state == state:
        return case
    return ledger.transition(
        case_id,
        state,
        at=now if now is not None else datetime.now(timezone.utc),
        actor=OPERATOR_ACTOR,
        reason=reason or DFLT_OPERATOR_REASON,
    )


def _runs_in_flight(ledger: Ledger, case_id: str) -> list[str]:
    """The ids of the case's runs the ledger records as running, in order."""
    return sorted(
        run.run_id for run in ledger.runs(status=RUNNING) if run.case_id == case_id
    )


# ---- drafts ----


@dataclass(frozen=True)
class DraftRelease:
    """What :func:`send_draft` did with one of a case's drafts.

    ``index`` and ``draft`` are the draft as the case held it. ``attempt`` is the gate's
    decision and the send (see :class:`~liaise.release.SendAttempt`), and ``filters`` is
    how many filters the gate ran it through. ``edited`` says whether the operator's text
    replaced the draft's. ``case`` is the case as the release left it, or would leave it in
    a dry run, and ``moved`` is its ``(from, to)`` states when the send moved it on.
    """

    index: int
    draft: Mapping[str, Any]
    attempt: SendAttempt
    filters: int
    edited: bool
    case: Case
    moved: Optional[tuple[str, str]] = None


@dataclass(frozen=True)
class DraftRejection:
    """What :func:`reject_draft` did: the draft it took off the case, and the case after it."""

    index: int
    draft: Mapping[str, Any]
    case: Case


def pick_draft(
    case: Case, index: Optional[int] = None
) -> tuple[int, Mapping[str, Any]]:
    """``(index, draft)``: ``case``'s draft at ``index``, or its only draft when ``index`` is None.

    Raises ``ValueError``, saying which drafts there are, for a case with none, for an index
    it holds no draft at, and for no index on a case holding several.
    """
    drafts = case.drafts
    listed = f"liaise case show {case.id} lists them"
    if not drafts:
        raise ValueError(f"case {case.id} has no draft waiting for the operator")
    last = len(drafts) - 1
    if index is None and last > 0:
        raise ValueError(
            f"case {case.id} has {len(drafts)} drafts, [0] to [{last}]: name the one "
            f"to use ({listed})"
        )
    index = 0 if index is None else index
    if not 0 <= index <= last:
        held = "[0]" if last == 0 else f"[0] to [{last}]"
        raise ValueError(
            f"case {case.id} has no draft [{index}]; its drafts are {held} ({listed})"
        )
    return index, drafts[index]


def find_draft(
    ledger: Ledger, case_id: str, *, index: Optional[int] = None
) -> tuple[int, Mapping[str, Any]]:
    """``(index, draft)`` of the case ``case_id``, as :func:`pick_draft` picks it.

    Raises ``ValueError`` for a case the ledger does not hold, and as :func:`pick_draft` does.
    """
    case = ledger.get_case(case_id)
    if case is None:
        raise ValueError(_no_case(case_id))
    return pick_draft(case, index)


def _github_repo_of(refs: Iterable[Optional[str]]) -> Optional[str]:
    """``owner/repo`` of the first GitHub issue among ``refs``, or None."""
    issues = (github_issue(ref) for ref in refs if ref)
    return next((issue[0] for issue in issues if issue is not None), None)


def send_draft(
    ledger: Ledger,
    subjects: Mapping[str, Subject],
    case_id: str,
    *,
    index: Optional[int] = None,
    text: Optional[str] = None,
    seen: Optional[Mapping[str, Any]] = None,
    by: str = OPERATOR_ACTOR,
    now: Optional[datetime] = None,
    registry: Optional[Mapping[str, Any]] = None,
    send: bool = True,
    dry_run: bool = False,
    outbound_filters: Iterable[OutboundFilter] = DFLT_OUTBOUND_FILTERS,
) -> DraftRelease:
    """Send the case ``case_id``'s draft at ``index`` as ``by``, through the gate again.

    The message is the draft's text, or ``text`` when the operator edited it. It goes to
    the draft's ``ref``, for its ``recipient``, carrying out its ``outcome``. It passes
    through :func:`liaise.release.gate_and_send`, with an :class:`~liaise.model.Approval`
    by ``by`` at ``now`` (the current UTC time when None) on the gate's context. Draft
    reply mode lets it through, and every other filter judges it as it would a message
    the tick sends, the mention included.

    It asks no one. Its caller shows the operator the message and the gate's verdict
    first, from a dry run, and passes the draft they saw as ``seen``, as
    ``liaise case send-draft`` does.

    - **Sent:** the draft leaves the case. A ``gate`` entry by ``by`` records the text as
      it went out, its url, the approval and why the draft was held. Once no draft is
      left, a case in ``needs-owner`` moves as :data:`STATE_AFTER_SENT_DRAFT` says.
    - **Diverted, or refused by its channel:** nothing is sent. The draft stays at its
      index, now holding the text the operator gave (without the mention the gate adds)
      and the new reason, and a ``gate`` entry records the attempt.

    ``seen`` is the draft as the operator read it: a draft that has changed since is not
    sent. ``send=False`` asks the channel for its plan and sends nothing. A divert or a
    refusal is then recorded as above, and a message the gate would pass changes nothing.
    A dry run judges and plans as a send would, and writes nothing.

    Raises ``ValueError``, sending and writing nothing, for any of these:

    - a case the ledger does not hold, one with a run in flight, or one whose subject is
      not in ``subjects``;
    - a draft :func:`pick_draft` cannot pick, or one that changed since ``seen``;
    - a draft with no destination, or no text to send;
    - a ``deliver`` message a hold kept, whose delivery never ran;
    - a hold that keeps the case's messages, or for a ``deliver`` message its delivery,
      waiting.

    Raises :class:`DraftSentNotRecorded` when the message went out and the ledger then
    failed to record it.
    """
    case = ledger.get_case(case_id)
    if case is None:
        raise ValueError(_no_case(case_id))
    in_flight = _runs_in_flight(ledger, case_id)
    if in_flight:
        raise ValueError(
            f"case {case_id} has run {in_flight[0]} in flight, whose outcomes may answer "
            f"the same thing: send the draft once a tick has collected that run"
        )
    subject = subjects.get(case.subject)
    if subject is None:
        raise ValueError(
            f"case {case_id} belongs to the subject {case.subject!r}, which is not "
            f"configured, so there is no policy to judge its draft by"
        )
    index, draft = pick_draft(case, index)
    label = f"draft [{index}] of {case_id}"
    if seen is not None and dict(draft) != dict(seen):
        raise ValueError(
            f"{label} changed while you had it open, so nothing was sent; read it again "
            f"with liaise case show {case_id}"
        )
    ref, recipient, purpose = draft.get("ref"), draft.get("recipient"), draft.get("outcome")
    if not ref:
        raise ValueError(
            f"{label} has no destination ({draft.get('reason')}): send it yourself, "
            f"then take it off the case with liaise case reject-draft {case_id} {index}"
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
            f"change first, then take this draft off the case with liaise case "
            f"reject-draft {case_id} {index}"
        )
    body = (draft.get("text") or "") if text is None else text
    if not body.strip():
        hint = "; write it with --edit" if text is None else ""
        raise ValueError(f"{label} has no text to send{hint}")
    scopes = scopes_for(
        subject=subject.slug,
        person=recipient or None,
        repo=_github_repo_of((ref, *case.conversations)),
        checkout=subject.workspace.path or None,
        effect=subject.delivery.kind if delivers else None,
        processor=False,
    )
    hold = blocking_hold(ledger, scopes, for_="effect")
    if hold is not None:
        waiting = "delivery" if delivers else "messages"
        raise ValueError(
            f"{label} was not sent: the hold on {hold.scope} ({hold.mode}) keeps this "
            f"case's {waiting} waiting; liaise unhold {hold.scope} first"
        )

    at = now if now is not None else datetime.now(timezone.utc)
    approval = Approval(by=by, at=at)
    filters = tuple(outbound_filters)
    outbound = Outbound(
        case_id=case.id,
        ref=ref,
        channel=channel,
        recipient=recipient,
        purpose=purpose,
        text=body,
    )
    context = GateContext(subject=subject, case=case, now=at, approval=approval)
    attempt = gate_and_send(
        outbound,
        context,
        registry=registry,
        dry_run=dry_run or not send,
        outbound_filters=filters,
    )
    decision = attempt.decision
    edited = text is not None and text != draft.get("text")
    release = functools.partial(
        DraftRelease,
        index=index,
        draft=draft,
        attempt=attempt,
        filters=len(filters),
        edited=edited,
    )
    if attempt.sent and not send and not dry_run:
        return release(case=case)  # only a plan: nothing went out, so nothing changes
    detail = {
        "purpose": purpose,
        "ref": ref,
        "notes": list(decision.notes),
        "draft": index,
        "held_for": draft.get("reason"),
        "edited": edited,
        "approval": approval.to_dict(),
    }
    others = (*case.drafts[:index], *case.drafts[index + 1 :])
    url = getattr(attempt.result, "url", None)
    moved = None
    if attempt.sent:
        entry = LedgerEntry(
            at=at,
            kind=DRAFT_ENTRY_KIND,
            actor=by,
            text=attempt.outbound.text,
            detail={**detail, "decision": "send", "url": url},
        )
        after = replace(case, drafts=others).with_entry(entry)
        target = STATE_AFTER_SENT_DRAFT.get(purpose)
        if case.state == NEEDS_OWNER and target is not None and not others:
            after = after.with_state(target, at=at, actor=by, reason=f"sent {label}")
            moved = (case.state, target)
    else:
        if decision.send is None:
            reason = decision.diverted
            outcome = {"decision": "divert", "reason": reason}
        else:
            reason = f"send failed: {attempt.failure}"
            outcome = {"decision": "send", "error": attempt.failure}
        entry = LedgerEntry(
            at=at,
            kind=DRAFT_ENTRY_KIND,
            actor=by,
            text=decision.send.text if decision.send is not None else body,
            detail={**detail, **outcome},
        )
        held = make_draft(
            at=at,
            outcome=purpose,
            recipient=recipient,
            ref=ref,
            text=body,  # the gate adds the mention again, for the handle of that day
            reason=reason,
            notes=decision.notes,
        )
        drafts = (*case.drafts[:index], held, *case.drafts[index + 1 :])
        after = replace(case, drafts=drafts).with_entry(entry)
    if not dry_run:
        try:
            ledger.save_case(after)
        except Exception as error:
            if not attempt.sent:
                raise
            where = f" as {url}" if url else ""
            raise DraftSentNotRecorded(
                f"{label} was sent{where}, but the ledger could not record it "
                f"({error_text(error)}): do not send it again. Take it off the case with "
                f'liaise case reject-draft {case_id} {index} --reason "sent, not recorded"'
            ) from error
    return release(case=after, moved=moved)


def reject_draft(
    ledger: Ledger,
    case_id: str,
    *,
    reason: str,
    index: Optional[int] = None,
    by: str = OPERATOR_ACTOR,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> DraftRejection:
    """Decline the case ``case_id``'s draft at ``index`` as ``by``, recording ``reason``.

    The draft leaves the case, and a ``gate`` entry by ``by``, stamped ``now``, keeps its
    text, where it would have gone, why it was held and ``reason``. Nothing is sent, and the
    case's state stays as it is: move it on with :func:`set_case_state`. A dry run writes
    nothing.

    Raises ``ValueError``, writing nothing, for a blank ``reason``, a case the ledger does
    not hold, and a draft :func:`pick_draft` cannot pick.
    """
    if not reason.strip():
        raise ValueError(
            "a rejected draft needs a reason, so the case says why it was not sent "
            "(--reason)"
        )
    index, draft = find_draft(ledger, case_id, index=index)
    case = ledger.get_case(case_id)
    entry = LedgerEntry(
        at=now if now is not None else datetime.now(timezone.utc),
        kind=DRAFT_ENTRY_KIND,
        actor=by,
        text=draft.get("text"),
        detail={
            "decision": "reject",
            "reason": reason.strip(),
            "purpose": draft.get("outcome"),
            "ref": draft.get("ref"),
            "draft": index,
            "held_for": draft.get("reason"),
        },
    )
    others = (*case.drafts[:index], *case.drafts[index + 1 :])
    after = replace(case, drafts=others).with_entry(entry)
    if not dry_run:
        ledger.save_case(after)
    return DraftRejection(index=index, draft=draft, case=after)
