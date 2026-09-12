"""Cases as the operator sees and moves them: ``liaise case list``, ``show`` and ``set-state``.

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
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from datetime import datetime, timezone
from typing import Any, Optional

from liaise.holds import DFLT_SET_BY
from liaise.ledger import Ledger
from liaise.model import CASE_STATES, Case, LedgerEntry, require_one_of
from liaise.processor import RUNNING
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
    for draft in case.drafts:
        to = draft.get("ref") or f"{draft.get('recipient')} (no channel)"
        lines.append(
            f"  {draft.get('at')} {draft.get('outcome')} to {to}: {draft.get('reason')}"
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
    in_flight = sorted(
        run.run_id for run in ledger.runs(status=RUNNING) if run.case_id == case_id
    )
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
