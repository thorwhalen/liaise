"""The liaise 0.1 data model: cases, ledger entries, outcomes, holds and runs.

Everything :mod:`liaise.ledger` keeps is one of these frozen dataclasses, stored as
the JSON-ready dict its ``to_dict`` returns and read back with ``from_dict``:
datetimes as ISO-8601 strings, tuples as lists, enums (a correspond ``Grade``) as
their values. ``from_dict`` rebuilds each field from its annotation, so a field's
type is written down in one place.

The vocabularies live here too (case states, entry kinds, outcome kinds,
permissions, hold modes), so each has one home: :mod:`liaise.state` takes its label
names from :data:`CASE_STATES`.

Nothing in this module touches storage, a channel or the clock. The case helpers
are pure and take the time they record as an argument.
"""

from __future__ import annotations

import copy
import types
import typing
from dataclasses import MISSING, dataclass, field, fields, replace
from datetime import datetime
from enum import Enum
from functools import cache
from typing import Any, Mapping, Optional, Self

#: A case's state: the 0.0.x `liaise:` label vocabulary, unchanged and in its
#: order. The design's `delivered` is `deployed` here.
CASE_STATES = (
    "intake",
    "paused",
    "working",
    "needs-partner",
    "needs-owner",
    "deployed",
    "budget",
)
#: The state a new case opens in.
INITIAL_CASE_STATE = CASE_STATES[0]
#: What a :class:`LedgerEntry` records.
ENTRY_KINDS = (
    "message",
    "transition",
    "outcome",
    "gate",
    "run",
    "hold",
    "projection",
)
#: The closed vocabulary a processor run reports its outcomes in. Validating an
#: :class:`Outcome` (and treating `decline` as `escalate`) is `liaise.outcomes`'s job.
OUTCOME_KINDS = (
    "ask",
    "reply",
    "escalate",
    "propose",
    "deliver",
    "decline",
    "defer",
    "note",
)
#: What a role can grant on a subject (see :mod:`liaise.subjects`).
PERMISSIONS = ("report", "request_work", "approve_candidate")
#: How a :class:`Hold` stops work in its scope.
HOLD_MODES = ("block", "drain", "cancel")


def require_one_of(value: Any, allowed: tuple[str, ...], *, what: str) -> Any:
    """Return ``value`` when it is in ``allowed``; otherwise raise ``ValueError`` listing them.

    >>> require_one_of("paused", CASE_STATES, what="case state")
    'paused'
    """
    if value not in allowed:
        raise ValueError(f"{what} {value!r} is not one of: {', '.join(allowed)}")
    return value


def to_jsonable(value: Any) -> Any:
    """``value`` as plain JSON-ready data.

    Datetimes become ISO-8601 strings, enums their values, records their
    ``to_dict()``, tuples lists. Mappings and lists are rebuilt rather than shared,
    so storing the result can never alias the object it came from.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, _Record):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


_UNION_TYPES = (typing.Union, types.UnionType)


def _from_jsonable(hint: Any, value: Any) -> Any:
    """``value``, as :func:`to_jsonable` left it, rebuilt as the annotation ``hint`` says."""
    if value is None:
        return None
    origin = typing.get_origin(hint)
    if origin in _UNION_TYPES:  # Optional[X]
        (inner,) = (arg for arg in typing.get_args(hint) if arg is not type(None))
        return _from_jsonable(inner, value)
    if origin is tuple:  # tuple[X, ...]
        item_hint = typing.get_args(hint)[0]
        return tuple(_from_jsonable(item_hint, item) for item in value)
    if hint is datetime:
        return value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if isinstance(hint, type) and issubclass(hint, _Record):
        return value if isinstance(value, hint) else hint.from_dict(value)
    return copy.deepcopy(value)


@cache
def _field_hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


class _Record:
    """``to_dict`` and ``from_dict`` for the frozen dataclasses of this module."""

    def to_dict(self) -> dict[str, Any]:
        """This record as JSON-ready data (see :func:`to_jsonable`)."""
        return {f.name: to_jsonable(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """The record ``data`` describes, as :meth:`to_dict` wrote it.

        Keys the record has no field for are ignored, so a ledger written by a newer
        liaise still reads. A missing required field raises ``ValueError``.
        """
        hints = _field_hints(cls)
        required = [
            f.name
            for f in fields(cls)
            if f.default is MISSING and f.default_factory is MISSING
        ]
        missing = [name for name in required if name not in data]
        if missing:
            raise ValueError(
                f"cannot build a {cls.__name__} without {', '.join(missing)} "
                f"(got the keys: {', '.join(sorted(data)) or 'none'})"
            )
        return cls(
            **{
                f.name: _from_jsonable(hints[f.name], data[f.name])
                for f in fields(cls)
                if f.name in data
            }
        )


@dataclass(frozen=True)
class LedgerEntry(_Record):
    """One thing that happened on a case: appended, never changed.

    ``kind`` is one of :data:`ENTRY_KINDS`. ``actor`` is a person id (for a
    ``message``, the person it is attributed to); ``grade`` and ``permission`` are
    what access was judged on; ``delivery_id`` is the channel event it came from.
    ``detail`` holds whatever else the kind needs, such as a transition's ``from``,
    ``to`` and ``reason``.
    """

    at: datetime
    kind: str
    actor: Optional[str] = None
    grade: Optional[str] = None
    permission: Optional[str] = None
    delivery_id: Optional[str] = None
    text: Optional[str] = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_one_of(self.kind, ENTRY_KINDS, what="ledger entry kind")


@dataclass(frozen=True)
class Case(_Record):
    """One piece of work on a subject, from its first message to its delivery.

    ``id`` is ``<subject>-<n>``. ``conversations`` are the encoded refs
    (``github:example/app#12``) whose messages belong to it; ``reporter`` is the
    person who opened it; ``state`` is one of :data:`CASE_STATES`. ``entries`` is the
    append-only history and ``drafts`` the outbound messages diverted to the operator.
    ``defer_until``, when set, is the earliest time the case may be dispatched again
    (after a quota reset, a rate limit, or a busy workspace).
    """

    id: str
    subject: str
    conversations: tuple[str, ...]
    reporter: str
    state: str
    created_at: datetime
    updated_at: datetime
    session_id: Optional[str] = None
    entries: tuple[LedgerEntry, ...] = ()
    drafts: tuple[Mapping[str, Any], ...] = ()
    defer_until: Optional[datetime] = None

    def __post_init__(self) -> None:
        require_one_of(self.state, CASE_STATES, what="case state")

    def with_entry(self, entry: LedgerEntry) -> Case:
        """This case with ``entry`` appended, and ``updated_at`` moved forward to it."""
        return replace(
            self,
            entries=(*self.entries, entry),
            updated_at=max(self.updated_at, entry.at),
        )

    def with_state(
        self,
        state: str,
        *,
        at: datetime,
        actor: Optional[str] = None,
        reason: str = "",
    ) -> Case:
        """This case in ``state``, with a ``transition`` entry recording the change.

        Raises ``ValueError`` for a state outside :data:`CASE_STATES`.
        """
        require_one_of(state, CASE_STATES, what="case state")
        entry = LedgerEntry(
            at=at,
            kind="transition",
            actor=actor,
            detail={"from": self.state, "to": state, "reason": reason},
        )
        return replace(self, state=state).with_entry(entry)


@dataclass(frozen=True)
class Outcome(_Record):
    """One outcome a processor run reports: ``kind`` from :data:`OUTCOME_KINDS`.

    Not validated here: ``liaise.outcomes`` validates a run's outcomes as a whole.
    """

    kind: str
    text: str = ""
    questions: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class Hold(_Record):
    """A stop on work in ``scope`` (``global``, ``subject:<slug>``, ``repo:<o/r>``, ...)."""

    scope: str
    mode: str
    reason: str = ""
    set_by: Optional[str] = None
    set_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        require_one_of(self.mode, HOLD_MODES, what="hold mode")


@dataclass(frozen=True)
class Health(_Record):
    """Whether a processor can take work now, and if not, until when or why."""

    ok: bool
    defer_until: Optional[datetime] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class RunRecord(_Record):
    """A processor run started on a case: how it was started, and where it is now."""

    run_id: str
    case_id: str
    subject: str
    mode: str
    status: str
    started_at: datetime
    pid: Optional[int] = None
    heartbeat_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    session_id: Optional[str] = None
    stream_path: Optional[str] = None


@dataclass(frozen=True)
class RunResult(_Record):
    """What a finished run returned: its outcomes, what it cost, and how it ended."""

    run_id: str
    outcomes: tuple[Outcome, ...] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)
    cost_usd: Optional[float] = None
    rate_limit: Optional[Mapping[str, Any]] = None
    error: Optional[str] = None
    session_id: Optional[str] = None
    summary: str = ""
