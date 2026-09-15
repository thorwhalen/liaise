"""Releasing a message: through the gate, then through correspond, as one step.

Every message liaise sends goes through :func:`gate_and_send`. That covers what the tick
sends for a run's outcomes, the tick's own notices, and a draft the operator releases
with ``liaise case send-draft``. It runs :func:`liaise.gate.run_gate`, and only a message
the gate passed reaches ``correspond.send``, as the filters left it. It records nothing:
what a :class:`SendAttempt` means for a case, a draft or a notification is for its caller
to keep.

One path for every sender is what makes the gate a gate. A filter added to it applies to
all of them at once, and none of them has a way to send around it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

import correspond

from liaise.gate import (
    DFLT_OUTBOUND_FILTERS,
    GateContext,
    GateDecision,
    Outbound,
    OutboundFilter,
    run_gate,
)

#: Why a send failed when the channel said no without saying why.
DFLT_REFUSAL = "the channel refused it"


def error_text(error: BaseException) -> str:
    """How liaise names an exception it recovered from: its class, then its message.

    >>> error_text(ValueError("no such channel"))
    'ValueError: no such channel'
    """
    return f"{type(error).__name__}: {error}"


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
    it, on ``registry`` (correspond's own when None). ``dry_run`` asks correspond for its
    plan and sends nothing. A channel that refuses the message, or raises, becomes a
    ``failure`` on the attempt rather than an exception, so the caller still has the message
    to keep.
    """
    decision = run_gate(outbound, ctx, outbound_filters=outbound_filters)
    passed = decision.send
    if passed is None:
        return SendAttempt(decision)
    try:
        result = correspond.send(
            passed.ref, passed.text, dry_run=dry_run, registry=registry
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
