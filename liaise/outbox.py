"""The delay outbox: messages the gate gave ``delay``, held for a cancellable window (liaise #38).

A send that cannot be withdrawn, to an organisation-wide or public place, gets the flow
``delay`` from the outbound policy (discussion 32 §5.4, the ``irreversibility`` row). The
tick does not send it at once, and does not hand it to the operator as a draft either: it
keeps it on the case's :attr:`~liaise.model.Case.outbox` until ``release_at``
(``policy.delay_minutes`` later), tells the operator only that a message is held and for
how long, and sends it on the first tick at or after that time. Until then the operator
takes it off with ``liaise case cancel-send CASE [INDEX]`` (:func:`cancel_send`).

**What a release re-checks.** Each item carries the outbox's own
:class:`~liaise.model.Approval` (:func:`liaise.gate.hold_for`), bound to the message, its
audience and its verdict as the gate judged them at hold time, and overriding only the
delay. At release the tick runs the whole gate again, with the audience asked of the channel
then and the case's provenance read from the ledger then, and that approval on the context.
While all three hold, the delay is settled and the message goes out; if anything changed —
the repository went public, a stranger commented and tainted the case, the disclosure now
flags something else — the approval is void and the message becomes a draft for the
operator, with the new verdict. It is never held again.

**What else keeps it from going out** (checked by the tick, in :func:`release_block`, on
every tick and for every item, due or not): a message the conversation has moved past — a
new inbound message on the case, the operator setting its state, its issue read closed
(:func:`moved_on`) — becomes a draft, since it answers a question that may no longer stand; an item
reached more than ``policy.delay_stale_minutes`` after its release becomes a draft, since
nobody was watching the window it relied on; and an item a crash left claimed becomes a
draft that says to check the channel first, since it may have gone out. An effect hold keeps
an item where it is.

**At most once.** The tick marks an item ``claimed_at`` and saves the case before it sends,
and takes it off after: a send is never repeated by liaise, and one whose outcome is unknown
goes to the operator.

Every transition is a ``gate`` entry on the case whose ``decision`` is one of
:data:`OUTBOX_DECISIONS`, and no operator notification carries anything the message says.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from liaise.gate import GateDecision, hold_for
from liaise.intake import LIAISE_ACTOR, SELF_ROLE
from liaise.ledger import Ledger
from liaise.model import Approval, Case, LedgerEntry
from liaise.outcomes import Send

#: The ``decision`` of a ``gate`` entry about the outbox: held, cancelled by the operator,
#: made a draft (``divert``), or made a draft for being reached too late (``lapse``). A
#: release that goes out is an ordinary ``send`` entry, with the hold as its approval.
HOLD, CANCEL, LAPSE, INTERRUPTED = "hold", "cancel", "lapse", "interrupted"
OUTBOX_DECISIONS = (HOLD, CANCEL, LAPSE, INTERRUPTED)
#: The ``detail["event"]`` of the ``run`` entry the tick writes on reading a case's issue
#: closed (``liaise.tick.RUN_ISSUE_CLOSED``, which imports this module).
ISSUE_CLOSED_EVENT = "issue_closed"
#: The entry kind every outbox transition is recorded as.
OUTBOX_ENTRY_KIND = "gate"
#: Who cancels a held message when nobody says: the operator.
DFLT_CANCELLED_BY = "operator"
#: The reason a cancel records when the operator gives none.
DFLT_CANCEL_REASON = "cancelled by the operator"
#: Why a held message the conversation moved past is a draft, not a send.
MOVED_ON_REASON = "the conversation moved on while this message was held ({what}): the operator decides"
#: Why a held message reached long after its release is a draft.
LAPSED_REASON = "held past its release by {late}, longer than the {limit} allowed: the operator decides"
#: Why a message whose release was interrupted is a draft.
INTERRUPTED_REASON = (
    "its release was interrupted at {claimed_at}, and it may have gone out: check {ref} "
    "before sending it"
)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _moment(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def make_held(
    outbound: Send,
    decision: GateDecision,
    *,
    at: datetime,
    delay: timedelta,
    seen: int,
) -> dict[str, Any]:
    """One item of a case's ``outbox``: ``outbound``, which ``decision`` gave ``delay``, JSON-ready.

    The message is kept **as it entered the gate** (no mention added): the hashes the hold
    binds to are of that message, and the gate adds the mention again at release. The item
    carries its ``release_at`` (``at`` plus ``delay``), the hold (:func:`liaise.gate.hold_for`),
    what the gate decided (:meth:`GateDecision.summary`) and its notes, ``seen`` (how many
    entries the case had when the message was planned, for :func:`moved_on`), and
    ``claimed_at``, None until the tick starts releasing it.
    """
    release_at = at + delay
    return {
        "at": _stamp(at),
        "release_at": _stamp(release_at),
        "outcome": outbound.purpose,
        "recipient": outbound.recipient,
        "ref": outbound.ref,
        "channel": outbound.channel,
        "title": outbound.title,
        "text": outbound.text,
        "hold": hold_for(decision, at=at, release_at=release_at).to_dict(),
        "gate": decision.summary(),
        "notes": list(decision.notes),
        "seen": seen,
        "claimed_at": None,
    }


def held_message(item: Mapping[str, Any], *, case_id: str) -> Send:
    """The message ``item`` holds, as the tick hands it to the gate again."""
    return Send(
        ref=item["ref"],
        channel=item["channel"],
        recipient=item["recipient"],
        purpose=item["outcome"],
        text=item["text"],
        title=item.get("title"),
        case_id=case_id,
    )


def hold_of(item: Mapping[str, Any]) -> Approval:
    """The outbox's approval ``item`` carries, for the context of its release."""
    return Approval.from_dict(item["hold"])


def release_at(item: Mapping[str, Any]) -> datetime:
    """When ``item`` may go out."""
    return _moment(item["release_at"])


def held_at(item: Mapping[str, Any]) -> datetime:
    """When ``item`` was held."""
    return _moment(item["at"])


def is_due(item: Mapping[str, Any], now: datetime) -> bool:
    """Whether ``item``'s window has passed at ``now``."""
    return release_at(item) <= now


def moved_on(case: Case, item: Mapping[str, Any]) -> Optional[str]:
    """What moved the conversation past ``item`` since it was planned, or None.

    ``item["seen"]`` is how many entries the case had when the run's actions were planned
    (entries are append-only, so a count is exact where a time is not: a comment heard late
    carries the time it was written). Among the entries after it, any of these moves it:

    - a ``message`` not written by the channel's own account (intake's ``self`` role: a
      comment of liaise's heard back moves nothing);
    - a ``transition`` by anyone but liaise itself: the operator set the case's state;
    - a ``run`` entry that read the case's issue closed.
    """
    seen = item.get("seen")
    later = case.entries[seen:] if isinstance(seen, int) else ()
    for entry in later:
        if entry.kind == "message" and entry.detail.get("role") != SELF_ROLE:
            return f"a message by {entry.actor or 'someone'} at {_stamp(entry.at)}"
        if entry.kind == "transition" and entry.actor != LIAISE_ACTOR:
            return (
                f"{entry.actor or 'someone'} moved the case to {entry.detail.get('to')}"
            )
        if entry.kind == "run" and entry.detail.get("event") == ISSUE_CLOSED_EVENT:
            return f"its issue was read closed at {_stamp(entry.at)}"
    return None


def release_block(
    case: Case,
    item: Mapping[str, Any],
    *,
    now: datetime,
    stale_after: Optional[timedelta],
) -> Optional[tuple[str, str]]:
    """``(decision, reason)`` when ``item``, due at ``now``, must go to the operator instead.

    In this order: a release a crash interrupted (:data:`INTERRUPTED`), a conversation that
    moved on since the run's actions were planned (``divert``, :func:`moved_on`), and an item reached more than ``stale_after`` past
    its release (:data:`LAPSE`; None never lapses). None when nothing blocks it: the gate
    decides the rest.
    """
    claimed = item.get("claimed_at")
    if claimed:
        return INTERRUPTED, INTERRUPTED_REASON.format(
            claimed_at=claimed, ref=item.get("ref")
        )
    moved = moved_on(case, item)
    if moved is not None:
        return "divert", MOVED_ON_REASON.format(what=moved)
    late = now - release_at(item)
    if stale_after is not None and late > stale_after:
        return LAPSE, LAPSED_REASON.format(late=late, limit=stale_after)
    return None


def pick_held(case: Case, index: Optional[int] = None) -> tuple[int, Mapping[str, Any]]:
    """``(index, item)``: ``case``'s held message at ``index``, or its only one when ``index`` is None.

    Raises ``ValueError``, saying which there are, for a case with none, an index it holds
    nothing at, and no index on a case holding several.
    """
    items = case.outbox
    listed = f"liaise case show {case.id} lists them"
    if not items:
        raise ValueError(f"case {case.id} has no message held in the outbox")
    last = len(items) - 1
    if index is None and last > 0:
        raise ValueError(
            f"case {case.id} holds {len(items)} messages, [0] to [{last}]: name the one "
            f"to cancel ({listed})"
        )
    index = 0 if index is None else index
    if not 0 <= index <= last:
        held = "[0]" if last == 0 else f"[0] to [{last}]"
        raise ValueError(
            f"case {case.id} holds no message [{index}]; its held messages are {held} ({listed})"
        )
    return index, items[index]


@dataclass(frozen=True)
class Cancellation:
    """What :func:`cancel_send` did: the ``index`` and ``item`` it took off, and the ``case`` after."""

    index: int
    item: Mapping[str, Any]
    case: Case


def cancel_send(
    ledger: Ledger,
    case_id: str,
    *,
    index: Optional[int] = None,
    reason: str = "",
    by: str = DFLT_CANCELLED_BY,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> Cancellation:
    """Take the case ``case_id``'s held message at ``index`` out of the outbox, unsent.

    A ``gate`` entry by ``by``, stamped ``now``, keeps its text, where it would have gone,
    when it would have, and ``reason`` (:data:`DFLT_CANCEL_REASON` when blank). The case's
    state stays as it is. A dry run writes nothing. Raises ``ValueError``, writing nothing,
    for a case the ledger does not hold and an item :func:`pick_held` cannot pick.
    """
    case = ledger.get_case(case_id)
    if case is None:
        raise ValueError(
            f"no case {case_id!r} in the ledger (liaise case list lists them)"
        )
    index, item = pick_held(case, index)
    entry = LedgerEntry(
        at=now if now is not None else datetime.now(timezone.utc),
        kind=OUTBOX_ENTRY_KIND,
        actor=by,
        text=item.get("text"),
        detail={
            "decision": CANCEL,
            "reason": reason.strip() or DFLT_CANCEL_REASON,
            "purpose": item.get("outcome"),
            "ref": item.get("ref"),
            "outbox": index,
            "release_at": item.get("release_at"),
        },
    )
    others = (*case.outbox[:index], *case.outbox[index + 1 :])
    after = replace(case, outbox=others).with_entry(entry)
    if not dry_run:
        ledger.save_case(after)
    return Cancellation(index=index, item=item, case=after)
