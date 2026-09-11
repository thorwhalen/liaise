"""Cases as the operator sees and moves them: ``liaise case list`` and ``liaise case set-state``.

The tick moves a case through its states on its own (see :mod:`liaise.tick`), except where
a state waits on the operator: nothing the tick does moves a ``needs-owner`` case on, and a
``deployed`` case never starts again. :func:`set_case_state` is how the operator moves one,
recorded on the case as a ``transition`` entry by the operator.

A case's ``liaise:`` label on GitHub is a projection of its state in the ledger, so
relabelling an issue by hand changes nothing, and the tick overwrites it. After
:func:`set_case_state`, the label follows on the next tick.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from datetime import datetime, timezone
from typing import Any, Optional

from liaise.holds import DFLT_SET_BY
from liaise.ledger import Ledger
from liaise.model import CASE_STATES, Case, require_one_of
from liaise.processor import RUNNING

#: Who a state set with :func:`set_case_state` is recorded as set by.
OPERATOR_ACTOR = DFLT_SET_BY
#: The reason recorded for a state the operator set without giving one.
DFLT_OPERATOR_REASON = "set by the operator"
#: States only the tick sets: ``working`` says a run is in flight, which only a start makes so.
TICK_ONLY_STATES = ("working",)


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
        raise ValueError(
            f"no case {case_id!r} in the ledger; liaise case list shows the cases there are"
        )
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
