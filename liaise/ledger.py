"""The ledger: liaise's own record of what it has seen, opened, decided and started.

One :class:`Ledger` over one ``MutableMapping``: the store seam. The default is
:func:`default_ledger_store`, JSON files under ``<state_dir>/ledger``. Every key is a
flat string with no "/", since ``dol.Jsons`` would read one as a subdirectory::

    inbox__<sha1(delivery_id)[:16]>     {delivery_id, channel, kind, at}; presence means seen
    case__<case_id>                     a Case
    conversation__<encoded ref>         the id of the case that conversation belongs to
    run__<run_id>                       a RunRecord
    hold__<scope>                       a Hold
    unrouted__<sha1(delivery_id)[:16]>  {delivery_id, subject, author, grade, reason, url, at}
    cursor__<encoded ref>               a channel cursor (see Ledger.cursors)
    counter__cases                      the last case number handed out
    daily__<subject>__<YYYY-MM-DD>      that day's dispatches, as 0.0.x kept them
    budget_notified__<subject>__<day>   when the operator heard that day's cap was reached
    issue_check__<case_id>              an IssueCheck: the tick's reads of the case's issue state

The variable parts (ids, refs, scopes) are percent-encoded, so the scope
``repo:example/app`` is stored under ``hold__repo%3Aexample%2Fapp``. The result has
no "/", no ":" (which a Windows filename cannot hold), and decodes back exactly.

**Dry runs.** Wrap the store as ``ChainMap({}, store)``. Reads see the real ledger,
while writes land in the dict in front and vanish with it. A delete there cannot
reach the store behind, so it leaves a ``None`` tombstone that every read treats as
absent.
"""

from __future__ import annotations

import copy
import hashlib
import os
from collections.abc import Iterator, MutableMapping
from dataclasses import replace
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import quote, unquote

from liaise.model import (
    CASE_STATES,
    INITIAL_CASE_STATE,
    Case,
    Hold,
    IssueCheck,
    LedgerEntry,
    RunRecord,
    require_one_of,
    to_jsonable,
)

#: The ledger's directory under `state_dir`.
DFLT_LEDGER_SUBDIR = "ledger"
#: How many hex digits of a delivery id's sha1 inbox and unrouted keys keep.
DFLT_DIGEST_LENGTH = 16

_SEP = "__"
_CASE_COUNTER_KEY = "counter__cases"


def default_ledger_store(
    state_dir: Union[str, os.PathLike],
) -> MutableMapping[str, Any]:
    """The default ledger store: one JSON file per key in ``<state_dir>/ledger``.

    Creates the directory, since ``dol.Jsons`` will not create one on write.
    """
    import dol

    root = Path(state_dir).expanduser() / DFLT_LEDGER_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return dol.Jsons(str(root))


def _escape(part: str) -> str:
    return quote(str(part), safe="")


def _key(prefix: str, *parts: str) -> str:
    return _SEP.join((prefix, *map(_escape, parts)))


def _digest(delivery_id: str) -> str:
    return hashlib.sha1(delivery_id.encode()).hexdigest()[:DFLT_DIGEST_LENGTH]


def _day_stamp(day: Union[date, str]) -> str:
    """``day`` as ``YYYY-MM-DD``: a datetime counts as its date, a string is kept."""
    if isinstance(day, datetime):
        day = day.date()
    return day if isinstance(day, str) else day.isoformat()


def _daily_key(subject: str, day: Union[date, str]) -> str:
    return _key("daily", subject, _day_stamp(day))


def _cap_notice_key(subject: str, day: Union[date, str]) -> str:
    return _key("budget_notified", subject, _day_stamp(day))


def _delete(store: MutableMapping[str, Any], key: str) -> None:
    """Delete ``key``, leaving a ``None`` tombstone where a lower layer still holds it."""
    try:
        del store[key]
    except KeyError:
        pass
    if store.get(key) is not None:  # a ChainMap whose lower layer has it
        store[key] = None


class _PrefixView(MutableMapping):
    """The store's ``<prefix>__<escaped key>`` entries, as a mapping of the plain keys."""

    def __init__(self, store: MutableMapping[str, Any], prefix: str):
        self._store = store
        self._head = prefix + _SEP

    def _stored_key(self, key: str) -> str:
        return self._head + _escape(key)

    def __getitem__(self, key: str) -> Any:
        value = self._store.get(self._stored_key(key))
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        self._store[self._stored_key(key)] = value

    def __delitem__(self, key: str) -> None:
        if key not in self:
            raise KeyError(key)
        _delete(self._store, self._stored_key(key))

    def __iter__(self) -> Iterator[str]:
        for stored in list(self._store):
            if stored.startswith(self._head) and self._store.get(stored) is not None:
                yield unquote(stored[len(self._head) :])

    def __len__(self) -> int:
        return sum(1 for _ in self)


class Ledger:
    """What liaise has seen, its cases, runs and holds, the unrouted queue and the cursors.

    ``store`` is any ``MutableMapping`` of JSON-ready values: a ``dict`` in tests,
    :func:`default_ledger_store` for real, ``ChainMap({}, store)`` for a dry run. The
    ledger keeps no state of its own, so two ledgers over one store agree.
    """

    def __init__(self, store: MutableMapping[str, Any]):
        self.store = store

    # ---- inbox: every channel event, deduplicated on its delivery id ----

    def seen(self, delivery_id: str) -> bool:
        """Whether the event with ``delivery_id`` has already been taken in."""
        return self._get(_key("inbox", _digest(delivery_id))) is not None

    def mark_seen(
        self, delivery_id: str, *, channel: str, kind: str, at: datetime
    ) -> None:
        """Record the event with ``delivery_id`` as taken in, so :meth:`seen` dedupes it."""
        record = {
            "delivery_id": delivery_id,
            "channel": channel,
            "kind": kind,
            "at": at,
        }
        self.store[_key("inbox", _digest(delivery_id))] = to_jsonable(record)

    # ---- cases ----

    def new_case(
        self, subject: str, conversation: str, *, reporter: str, at: datetime
    ) -> Case:
        """Open a case on ``conversation`` (an encoded ref), numbered ``<subject>-<n>``.

        The case starts in ``intake``. Raises ``ValueError``, handing out no number, when
        the conversation already belongs to a case: its messages go to that case.
        """
        owner = self._get(_key("conversation", conversation))
        if owner is not None:
            raise ValueError(
                f"conversation {conversation!r} already belongs to case {owner!r}; "
                f"append to that case instead of opening another"
            )
        number = int(self._get(_CASE_COUNTER_KEY) or 0) + 1
        while self._get(_key("case", f"{subject}-{number}")) is not None:
            number += 1  # never overwrite a case, even if the counter was lost
        self.store[_CASE_COUNTER_KEY] = number
        case = Case(
            id=f"{subject}-{number}",
            subject=subject,
            conversations=(conversation,),
            reporter=reporter,
            state=INITIAL_CASE_STATE,
            created_at=at,
            updated_at=at,
        )
        self.save_case(case)
        return case

    def get_case(self, case_id: str) -> Optional[Case]:
        """The case with ``case_id``, or None."""
        data = self._get(_key("case", case_id))
        return None if data is None else Case.from_dict(data)

    def save_case(self, case: Case) -> None:
        """Write ``case``, and index each of its conversations to it.

        Raises ``ValueError``, writing nothing, when one of its conversations belongs
        to another case.
        """
        unindexed = []
        for ref in case.conversations:
            owner = self._get(_key("conversation", ref))
            if owner is None:
                unindexed.append(ref)
            elif owner != case.id:
                raise ValueError(
                    f"conversation {ref!r} belongs to case {owner!r}, not {case.id!r}"
                )
        self.store[_key("case", case.id)] = case.to_dict()
        for ref in unindexed:
            self.store[_key("conversation", ref)] = case.id

    def cases(
        self, *, subject: Optional[str] = None, state: Optional[str] = None
    ) -> Iterator[Case]:
        """Every case, or those of ``subject`` and/or in ``state``, in no set order."""
        if state is not None:
            require_one_of(state, CASE_STATES, what="case state")
        return (
            case
            for case in map(Case.from_dict, self._values("case"))
            if (subject is None or case.subject == subject)
            and (state is None or case.state == state)
        )

    def case_for_conversation(self, encoded_ref: str) -> Optional[Case]:
        """The case ``encoded_ref`` belongs to, or None."""
        case_id = self._get(_key("conversation", encoded_ref))
        return None if case_id is None else self.get_case(case_id)

    def add_conversation(self, case_id: str, encoded_ref: str) -> Case:
        """Attach ``encoded_ref`` to the case, indexing it. Idempotent."""
        case = self._case(case_id)
        if encoded_ref not in case.conversations:
            case = replace(case, conversations=(*case.conversations, encoded_ref))
            self.save_case(case)
        return case

    def append(self, case_id: str, entry: LedgerEntry) -> Case:
        """Append ``entry`` to the case's history and save it."""
        case = self._case(case_id).with_entry(entry)
        self.save_case(case)
        return case

    def transition(
        self,
        case_id: str,
        state: str,
        *,
        at: datetime,
        actor: Optional[str] = None,
        reason: str = "",
    ) -> Case:
        """Move the case to ``state``, recording a ``transition`` entry.

        Raises ``ValueError``, writing nothing, for a state outside ``CASE_STATES``.
        """
        case = self._case(case_id).with_state(state, at=at, actor=actor, reason=reason)
        self.save_case(case)
        return case

    # ---- runs ----

    def save_run(self, record: RunRecord) -> None:
        """Write ``record``, replacing any earlier record of that run."""
        self.store[_key("run", record.run_id)] = record.to_dict()

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        """The record of the run ``run_id``, or None."""
        data = self._get(_key("run", run_id))
        return None if data is None else RunRecord.from_dict(data)

    def runs(self, *, status: Optional[str] = None) -> Iterator[RunRecord]:
        """Every run record, or those with ``status``, in no set order."""
        return (
            record
            for record in map(RunRecord.from_dict, self._values("run"))
            if status is None or record.status == status
        )

    # ---- holds ----

    def set_hold(self, hold: Hold) -> None:
        """Put ``hold`` on its scope, replacing any hold already there."""
        self.store[_key("hold", hold.scope)] = hold.to_dict()

    def get_hold(self, scope: str) -> Optional[Hold]:
        """The hold on ``scope``, or None."""
        data = self._get(_key("hold", scope))
        return None if data is None else Hold.from_dict(data)

    def clear_hold(self, scope: str) -> None:
        """Lift the hold on ``scope``. A scope with no hold is left as it is."""
        _delete(self.store, _key("hold", scope))

    def holds(self) -> Iterator[Hold]:
        """Every hold, in no set order."""
        return map(Hold.from_dict, self._values("hold"))

    # ---- the unrouted queue ----

    def add_unrouted(self, **fields: Any) -> None:
        """Queue a message that matched a binding but failed resolution, grade or permission.

        The fields are the spec's ``delivery_id, subject, author, grade, reason, url,
        at``; only ``delivery_id`` is required, since the queue is keyed and
        deduplicated on it.
        """
        delivery_id = fields.get("delivery_id")
        if not delivery_id:
            raise ValueError(
                "add_unrouted needs a delivery_id: the unrouted queue is keyed, "
                f"and deduplicated, on it (got the fields: {', '.join(sorted(fields))})"
            )
        self.store[_key("unrouted", _digest(delivery_id))] = to_jsonable(fields)

    def unrouted(self) -> Iterator[dict[str, Any]]:
        """Every queued unrouted message, as a fresh dict, in no set order."""
        return (copy.deepcopy(item) for item in self._values("unrouted"))

    # ---- daily dispatch counters ----

    def daily_count(self, subject: str, day: Union[date, str]) -> int:
        """How many dispatches ``subject`` had on ``day`` (a date, or ``YYYY-MM-DD``)."""
        return int(self._get(_daily_key(subject, day)) or 0)

    def increment_daily(self, subject: str, day: Union[date, str]) -> int:
        """Count one more dispatch for ``subject`` on ``day``, returning the new count."""
        count = self.daily_count(subject, day) + 1
        self.store[_daily_key(subject, day)] = count
        return count

    def decrement_daily(self, subject: str, day: Union[date, str]) -> int:
        """Take back one dispatch counted for ``subject`` on ``day``, returning the new count.

        For a run that turned out not to count against the cap, such as one that found the
        login expired. A day with no dispatches stays at zero, and nothing is written.
        """
        count = self.daily_count(subject, day)
        if count <= 0:
            return 0
        self.store[_daily_key(subject, day)] = count - 1
        return count - 1

    def daily_cap_notified(self, subject: str, day: Union[date, str]) -> bool:
        """Whether the operator has been told that ``subject`` reached its cap on ``day``."""
        return self._get(_cap_notice_key(subject, day)) is not None

    def mark_daily_cap_notified(
        self, subject: str, day: Union[date, str], *, at: datetime
    ) -> None:
        """Record that the operator was told, ``at``, that ``subject`` reached its cap on ``day``.

        :meth:`daily_cap_notified` then says so, so they are told once per subject per day.
        """
        self.store[_cap_notice_key(subject, day)] = at.isoformat()

    # ---- reads of a case's issue state ----

    def get_issue_check(self, case_id: str) -> IssueCheck:
        """The tick's reads of the case's issue state; an empty :class:`IssueCheck` before any."""
        data = self._get(_key("issue_check", case_id))
        return IssueCheck() if data is None else IssueCheck.from_dict(data)

    def save_issue_check(self, case_id: str, check: IssueCheck) -> None:
        """Write ``check``, replacing what the ledger held of the case's issue state reads."""
        self.store[_key("issue_check", case_id)] = check.to_dict()

    # ---- cursors ----

    @cached_property
    def cursors(self) -> MutableMapping[str, str]:
        """The channel cursors, keyed by encoded conversation ref.

        Hand it to correspond as ``listen(ref, cursors=ledger.cursors)``.
        """
        return _PrefixView(self.store, "cursor")

    # ---- store access ----

    def _get(self, key: str) -> Any:
        return self.store.get(key)

    def _values(self, prefix: str) -> Iterator[Any]:
        head = prefix + _SEP
        for key in list(self.store):
            if key.startswith(head):
                value = self.store.get(key)
                if value is not None:
                    yield value

    def _case(self, case_id: str) -> Case:
        case = self.get_case(case_id)
        if case is None:
            raise KeyError(f"no case {case_id!r} in the ledger")
        return case
