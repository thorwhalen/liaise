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
from typing import Any, Optional, Union

from liaise.detect import link_urls, visible
from liaise.gate import DFLT_OUTBOUND_FILTERS, OutboundFilter
from liaise.holds import DFLT_SET_BY
from liaise.ledger import Ledger
from liaise.model import CASE_STATES, Approval, Case, LedgerEntry, require_one_of
from liaise.policy import audience_in_words
from liaise.processor import RUNNING
from liaise.release import (
    DRAFT_ENTRY_KIND,
    DraftSentNotRecorded,
    SendAttempt,
    release_draft,
)
from liaise.subjects import Subject
from liaise.tick import RUN_DEPLOY_FAILED

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


def _no_case(case_id: str) -> str:
    return (
        f"no case {case_id!r} in the ledger; liaise case list shows the cases there are"
    )


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


#: The keys of a ``gate`` entry's audit record that ``liaise case show`` sums up rather than
#: prints: the verdict, the audience snapshot and the fingerprints stay in the ledger.
_AUDIT_KEYS = frozenset(
    {
        "concerns",
        "settled",
        "verdict",
        "consulted",
        "payload_hash",
        "audience_hash",
        "approval_bound",
    }
)
#: How a held message's audience reads when no verdict names it.
AUDIENCE_UNKNOWN = "not judged"


def _shown_detail(detail: Mapping[str, Any]) -> dict[str, Any]:
    """``detail`` as an entry's line shows it: an audit record summed up by its rules and approval."""
    shown = {key: value for key, value in detail.items() if key not in _AUDIT_KEYS}
    approval = shown.get("approval")
    if isinstance(approval, Mapping):
        past = ", ".join(approval.get("rules_overridden") or ())
        shown["approval"] = f"by {approval.get('by')}" + (
            f" past {past}" if past else ""
        )
    rules = [
        str(concern.get("rule") or concern.get("filter"))
        for concern in detail.get("concerns") or ()
        if isinstance(concern, Mapping)
    ]
    if rules:
        shown["rules"] = ", ".join(dict.fromkeys(rules))
    if detail.get("approval_bound") is False:
        shown["approval"] = f"{shown.get('approval')} (void)"
    return shown


def gate_summary(detail: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """What a ``gate`` entry's ``detail`` says of its decision, as a held draft keeps it.

    None for an entry that records no verdict (one written before liaise ADR 0002, a
    rejection, a nudge).
    """
    if "flow" not in detail:
        return None
    verdict = detail.get("verdict")
    try:
        audience = audience_in_words(verdict["audience"]) if verdict else None
    except (
        KeyError,
        TypeError,
        ValueError,
    ):  # a record nobody can read names no audience
        audience = None
    concerns = detail.get("concerns") or ()
    return {
        "flow": detail["flow"],
        "audience": audience,
        "reasons": [c.get("text") for c in concerns if isinstance(c, Mapping)],
    }


def held_lines(
    text: Optional[str],
    *,
    gate: Optional[Mapping[str, Any]] = None,
    indent: str = TEXT_INDENT,
) -> list[str]:
    """A held message as the operator reads it before releasing it (discussion §5.7).

    What the gate decided and the audience in words, when ``gate`` (a draft's, or
    :func:`gate_summary`'s) says; the text, each invisible or control character written as
    ``<U+XXXX>``; and every link and image destination in full, since a link's title can
    say one place and its destination another.
    """
    lines = []
    if gate:
        audience = gate.get("audience") or AUDIENCE_UNKNOWN
        lines.append(f"{indent}[gate: {gate.get('flow')}; audience: {audience}]")
    lines += [f"{indent}{line}" for line in visible(text or NONE_SHOWN).splitlines()]
    urls = link_urls(text or "")
    if urls:
        lines.append(f"{indent}[links, in full:]")
        lines += [f"{indent}  {visible(url)}" for url in urls]
    return lines


def entry_line(entry: LedgerEntry) -> str:
    """One entry on one line: when, what, by whom, its detail, and the start of its text."""
    detail = ", ".join(
        f"{key}={value}"
        for key, value in _shown_detail(entry.detail).items()
        if value not in (None, "", [], {})
    )
    line = f"{_stamp(entry.at)} {entry.kind}" + (
        f" by {entry.actor}" if entry.actor else ""
    )
    if detail:
        line += f": {detail}"
    text = " ".join(visible(entry.text or "").split())
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
    operator, with the gate's flow, the audience in words, its whole text with invisible
    characters made visible and every link in full (:func:`held_lines`); and its
    ``entries`` latest ledger entries, oldest first, a line each with its detail and the
    start of its text. Reads only. Raises ``ValueError`` for a case the ledger ``store``
    does not hold.
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
        lines += held_lines(draft.get("text"), gate=draft.get("gate"))
    shown = case.entries[-entries:] if entries > 0 else ()
    lines.append(f"latest entries: {len(shown)} of {len(case.entries)}")
    lines += [f"  {entry_line(entry)}" for entry in shown]
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
    ``approval`` is the approval the gate was given: the one to pass back to send exactly
    what was judged.
    """

    index: int
    draft: Mapping[str, Any]
    attempt: SendAttempt
    filters: int
    edited: bool
    case: Case
    moved: Optional[tuple[str, str]] = None
    approval: Optional[Approval] = None


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


def send_draft(
    ledger: Ledger,
    subjects: Mapping[str, Subject],
    case_id: str,
    *,
    index: Optional[int] = None,
    text: Optional[str] = None,
    seen: Optional[Mapping[str, Any]] = None,
    by: str,
    now: Optional[datetime] = None,
    registry: Optional[Mapping[str, Any]] = None,
    send: bool = True,
    dry_run: bool = False,
    outbound_filters: Iterable[OutboundFilter] = DFLT_OUTBOUND_FILTERS,
    approval: Optional[Approval] = None,
    approve_shown: bool = False,
    justification: str = "",
    fingerprint_key: Union[bytes, Callable[[], bytes], None] = None,
) -> DraftRelease:
    """Send the case ``case_id``'s draft at ``index`` as ``by``, through the gate again.

    The message is the draft's text, or ``text`` when the operator edited it. It goes out
    through :func:`liaise.release.release_draft`, with an :class:`~liaise.model.Approval`
    by ``by`` at ``now`` (the current UTC time when None) on the gate's context, bound to
    the message and the audience its channel reports at send time. The approval settles
    what it names and binds to, draft reply mode among them, and every other concern of the
    gate holds, a ``refuse`` always.

    It asks no one, and ``by`` has no default: the caller says who releases the draft. Its
    caller shows the operator the message and the gate's verdict first, from a dry run with
    ``approve_shown``, and passes the draft they saw as ``seen`` and that dry run's
    ``approval``, as ``liaise case send-draft`` does after asking at a terminal. With
    neither, nothing is settled and a draft the gate holds back stays held. A text, an
    audience or a verdict that changed since the approval voids it, and nothing is sent.

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
    - anything :func:`liaise.release.release_draft` refuses: no destination or no text, a
      ``deliver`` message a hold kept, a hold that keeps the case's effects waiting.

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
    reject = f"liaise case reject-draft {case_id} {index}"
    outcome = release_draft(
        draft,
        subject=subject,
        ledger=ledger,
        label=label,
        reject=reject,
        by=by,
        now=now if now is not None else datetime.now(timezone.utc),
        case=case,
        text=text,
        detail={"draft": index},
        registry=registry,
        send=send,
        dry_run=dry_run,
        outbound_filters=outbound_filters,
        approval=approval,
        approve_shown=approve_shown,
        justification=justification,
        fingerprint_key=fingerprint_key,
    )
    release = functools.partial(
        DraftRelease,
        index=index,
        draft=draft,
        attempt=outcome.attempt,
        filters=outcome.filters,
        edited=outcome.edited,
        approval=outcome.approval,
    )
    if outcome.entry is None:
        return release(case=case)  # only a plan: nothing went out, so nothing changes
    others = (*case.drafts[:index], *case.drafts[index + 1 :])
    moved = None
    if outcome.kept is None:
        after = replace(case, drafts=others).with_entry(outcome.entry)
        target = STATE_AFTER_SENT_DRAFT.get(draft.get("outcome"))
        if case.state == NEEDS_OWNER and target is not None and not others:
            at = outcome.entry.at
            after = after.with_state(target, at=at, actor=by, reason=f"sent {label}")
            moved = (case.state, target)
    else:
        drafts = (*case.drafts[:index], outcome.kept, *case.drafts[index + 1 :])
        after = replace(case, drafts=drafts).with_entry(outcome.entry)
    if not dry_run:
        try:
            ledger.save_case(after)
        except Exception as error:
            if outcome.kept is not None:
                raise
            raise DraftSentNotRecorded.after(
                label, outcome.attempt, error, reject=reject
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
