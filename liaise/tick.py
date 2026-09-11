"""The tick: one pass of liaise 0.1's loop (design §3.1).

:func:`run_once` takes in what arrived, reconciles the runs in flight, starts the cases
that are ready, nudges quiet deliveries, and shows each touched case's state on its GitHub
labels::

    1. intake     each subject's bindings, through correspond (liaise.intake)
    2. reconcile  each run the tick has not collected: cancelled for a cancel hold or its
                  wall clock, refreshed, and collected once finished. An error takes its
                  action from liaise.errors; a success's outcomes are planned
                  (liaise.outcomes) and carried out, every message through the gate
                  (liaise.gate). Batch deploys run last, once per subject.
    3. start      each ready case that passes holds, authorization, budget, preflight and
                  the workspace check, as a detached processor run
    4. nudge      each deployed case its partner has gone quiet on, once
    5. project    each touched case's state label (liaise.projection)

The tick keeps its own clock: ``now`` stamps every entry, and a run's wall clock counts
from the tick that started it. The ledger's record of a run says ``running`` until the
tick collects it, whatever the processor says, so a run that ended at once (a spawn that
failed, an ``EchoProcessor`` run) is still collected, on the next tick. Any exception a
processor verb raises is a ``crashed`` run, never the tick's end (#24), and one case's
failure is a problem line, never the other cases' end.

**Dry run.** The ledger is ``Ledger(ChainMap({}, store))``: every step runs on real state,
and every write vanishes with the overlay. Nothing is sent (``correspond.send`` gets
``dry_run=True``), labelled, started, cancelled, deployed, locked, stamped, notified or
written (the processor is asked with ``persist=False``). The report's plan lines say what
would be.

**One tick at a time.** A tick holds the run lock in ``state_dir`` (:func:`run_lock_path`)
and stamps its start and end in the store (:func:`run_stamps`), so ``liaise status`` can
tell a running tick from a finished or an interrupted one. :func:`status_lines` is what
``liaise status`` prints.
"""

from __future__ import annotations

import functools
import os
import shlex
import subprocess
import time
from collections import ChainMap
from collections.abc import (
    Collection,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Union

import correspond

from liaise.access import Resolver, resolve_person
from liaise.config import ConfigError, GlobalConfig
from liaise.errors import ERROR_ACTIONS, classify_delivery_failure
from liaise.errors import defer_until as defer_for_error
from liaise.gate import (
    DFLT_OUTBOUND_FILTERS,
    GateContext,
    Outbound,
    OutboundFilter,
    run_gate,
)
from liaise.github import GhCli, GitHub
from liaise.holds import (
    AUTO_SET_BY_PREFIX,
    auto_hold,
    blocking_hold,
    release_auto_holds,
    scopes_for,
)
from liaise.intake import LIAISE_ACTOR, intake
from liaise.ledger import Ledger
from liaise.model import (
    CASE_STATES,
    Case,
    Hold,
    LedgerEntry,
    Outcome,
    RunRecord,
    RunResult,
)
from liaise.notify import notify
from liaise.outcomes import (
    DFLT_OPERATOR_PRIORITY,
    OUTCOME_SCHEMA,
    Action,
    Defer,
    Deliver,
    DigestNote,
    NotifyOperator,
    Send,
    StoreDraft,
    Transition,
    make_draft,
    plan_outcomes,
)
from liaise.processor import FINISHED, FRESH, RESUME, RUNNING, ClaudeHeadless, Job
from liaise.projection import github_issue, project_labels
from liaise.prompt import compose_case_prompt
from liaise.readiness import compute_readiness, last_partner_activity
from liaise.subjects import DELIVERY_KINDS, Subject
from liaise.workspace import (
    DFLT_LOCKS_SUBDIR,
    SharedCheckout,
    pid_is_alive,
    workspace_for,
)

INTAKE, PAUSED, WORKING, NEEDS_PARTNER, NEEDS_OWNER, DEPLOYED, BUDGET = CASE_STATES
#: The states a case may be started from. ``working``, ``needs-owner`` and ``deployed``
#: wait on something other than the partner's clock. ``budget`` is one, since the cap is
#: per day (0.0.x H-4).
ELIGIBLE_STATES = (INTAKE, NEEDS_PARTNER, PAUSED, BUDGET)
#: How long a ``defer`` outcome puts a case aside.
DFLT_DEFER = timedelta(hours=24)
#: The processor runs' directory under ``state_dir``.
DFLT_RUNS_SUBDIR = "runs"
#: How many of the latest unrouted messages and digest notes :func:`status_lines` lists.
DFLT_STATUS_RECENT = 5
#: The actor of the entries the tick writes.
TICK_ACTOR = LIAISE_ACTOR
#: The permission a case's reporter needs for the tick to start work on it.
REQUEST_WORK = "request_work"
#: The hold scope of the processor, which the tick holds itself after some errors.
PROCESSOR_SCOPE = "processor"
#: How long an automatic ``processor`` hold stands before the tick probes it with
#: preflight, lifting it if preflight passes. A login that has expired where preflight
#: cannot see it then costs one failed run, and one notification, per interval, not a tick.
AUTH_PROBE_INTERVAL = timedelta(minutes=30)
#: A deploy that runs the subject's command, and a delivery that stops at a pull request.
DEPLOY_DELIVERY, PR_ONLY_DELIVERY = DELIVERY_KINDS
#: A deploy that runs once per tick, after every case of the subject has been collected.
BATCH_DELIVERY_PER = "batch"

#: The messages the tick sends to a partner on its own, through the gate, which adds the
#: mention: when the daily cap trips, when a batch deploy is live, and on a quiet delivery.
BUDGET_MESSAGE = (
    "Today's limit on automatic work has been reached. This will pick back up tomorrow."
)
TRY_IT_MESSAGE = "It's live: please have a look and let us know how it goes."
NUDGE_MESSAGE = (
    "Just checking in: did you get a chance to try this? Let us know how it goes."
)
#: The ``purpose`` of those messages, as the gate and the ledger see it.
BUDGET_PURPOSE = "budget"
NUDGE_PURPOSE = "nudge"
#: The ntfy priority of the daily-cap notice: news for the operator, not a call to act.
DAILY_CAP_PRIORITY = "default"

#: What a ``run`` entry records: a start, a start that failed, a collection.
RUN_STARTED = "started"
RUN_START_FAILED = "start_failed"
RUN_COLLECTED = "collected"

#: How many characters of a message a plan line shows.
PLAN_TEXT_CHARS = 72
#: How many characters of a failed deploy's output its reason carries.
DELIVERY_OUTPUT_CHARS = 300

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_CRASHED = "crashed"
_NEEDS_HUMAN = "needs_human"
_QUOTA_EXHAUSTED = "quota_exhausted"
_WORKSPACE_CONFLICT = "workspace_conflict"
_REPLY_KIND = "reply"

#: ``(subject, *, lock_dir, sessions_dir, own_pids) -> SharedCheckout | None``: the
#: workspace seam (see :func:`liaise.workspace.workspace_for`).
WorkspaceFactory = Callable[..., Optional[SharedCheckout]]


@dataclass(frozen=True)
class Diversion:
    """A message that did not go out: the gate diverted it, or a hold kept it."""

    outbound: Outbound
    reason: str


@dataclass(frozen=True)
class TickReport:
    """What one :func:`run_once` did or, in a dry run, would do.

    ``plan_lines`` has a line per step, event, case and decision, for ``--dry-run`` to
    print. ``dispatched`` and ``collected`` are run ids, ``sent`` the messages as they went
    out (mention added), ``diverted`` those that stayed with the operator as drafts.
    """

    plan_lines: tuple[str, ...] = ()
    dispatched: tuple[str, ...] = ()
    collected: tuple[str, ...] = ()
    sent: tuple[Outbound, ...] = ()
    diverted: tuple[Diversion, ...] = ()
    problems: tuple[str, ...] = ()
    dry_run: bool = False


@dataclass(frozen=True)
class _DeliveryGroup:
    """A planned ``Deliver``, with the "try it" sends and the transition that follow it."""

    deliver: Deliver
    sends: tuple[Send, ...] = ()
    transition: Optional[Transition] = None


@dataclass(frozen=True)
class _PendingDelivery:
    """A delivery waiting for its subject's batch deploy, at the end of reconcile."""

    case_id: str
    group: _DeliveryGroup
    #: Whether the group's transition is the last state the run planned.
    applies_state: bool


# ---- pure helpers ----


def _preview(text: Optional[str]) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= PLAN_TEXT_CHARS else flat[: PLAN_TEXT_CHARS - 1] + "…"


def _fmt_age(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds}s"
    if seconds < _SECONDS_PER_HOUR:
        return f"{seconds // _SECONDS_PER_MINUTE}m"
    hours, rest = divmod(seconds, _SECONDS_PER_HOUR)
    return f"{hours}h{rest // _SECONDS_PER_MINUTE:02d}m"


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _run_starts(case: Case) -> list[LedgerEntry]:
    """The case's ``run`` entries that record a start, oldest first."""
    return [
        entry
        for entry in case.entries
        if entry.kind == "run" and entry.detail.get("event") == RUN_STARTED
    ]


def _latest_grade(case: Case, person: str) -> Optional[str]:
    """The grade of ``person``'s latest message on the case, or None without one."""
    messages = [e for e in case.entries if e.kind == "message" and e.actor == person]
    return max(messages, key=lambda e: e.at).grade if messages else None


def _entered_state_at(case: Case, state: str) -> datetime:
    """When the case last moved into ``state`` (its creation, if no transition says)."""
    moves = [
        entry.at
        for entry in case.entries
        if entry.kind == "transition" and entry.detail.get("to") == state
    ]
    return max(moves, default=case.created_at)


def _github_repo(case: Case) -> Optional[str]:
    """``owner/repo`` of the case's first GitHub issue, or None."""
    issues = (github_issue(ref) for ref in case.conversations)
    return next((issue[0] for issue in issues if issue is not None), None)


def _is_auto(found: Optional[Hold]) -> bool:
    return found is not None and (found.set_by or "").startswith(AUTO_SET_BY_PREFIX)


def _is_batch(deliver: Deliver) -> bool:
    return deliver.kind == DEPLOY_DELIVERY and deliver.per == BATCH_DELIVERY_PER


def _group_deliveries(
    actions: Sequence[Action],
) -> list[Union[Action, _DeliveryGroup]]:
    """``actions`` with each ``Deliver`` gathered with the sends and transition it plans.

    :func:`liaise.outcomes.plan_outcomes` plans a ``deliver`` as ``Deliver``, its "try
    it" ``Send`` (or a draft and an operator notification when nothing reaches the
    reporter), then ``Transition`` to ``deployed``. The sends and the transition belong
    to the delivery: they go out only once it has happened.
    """
    grouped: list[Union[Action, _DeliveryGroup]] = []
    open_at: Optional[int] = None
    for action in actions:
        if isinstance(action, Deliver):
            grouped.append(_DeliveryGroup(action))
            open_at = len(grouped) - 1
        elif open_at is not None and isinstance(action, (Send, Transition)):
            group = grouped[open_at]
            if isinstance(action, Send):
                grouped[open_at] = replace(group, sends=(*group.sends, action))
            else:
                grouped[open_at] = replace(group, transition=action)
                open_at = None
        else:
            grouped.append(action)
    return grouped


def _selected_slugs(
    subjects: Mapping[str, Subject], only: Optional[Union[str, Collection[str]]]
) -> tuple[str, ...]:
    """The slugs to run, sorted: all of ``subjects``, or those ``only`` names."""
    if only is None:
        return tuple(sorted(subjects))
    wanted = {only} if isinstance(only, str) else set(only)
    unknown = sorted(wanted - set(subjects))
    if unknown:
        known = ", ".join(sorted(subjects)) or "(none)"
        raise ConfigError(
            f"no subject {', '.join(map(repr, unknown))} is configured; the subjects "
            f"are: {known}. Each is a file subjects/<slug>.toml under the config root."
        )
    return tuple(sorted(wanted))


# ---- the facade ----


def run_once(
    subjects: Mapping[str, Subject],
    store: MutableMapping[str, Any],
    *,
    global_config: GlobalConfig,
    registry: Optional[Mapping[str, Any]] = None,
    processor: Optional[Any] = None,
    resolver: Resolver = resolve_person,
    workspace: WorkspaceFactory = workspace_for,
    labeler: Optional[GitHub] = None,
    notify_fn: Optional[Callable[..., Any]] = None,
    sessions_dir: Optional[Union[str, os.PathLike]] = None,
    now: Optional[datetime] = None,
    dry_run: bool = False,
    only: Optional[Union[str, Collection[str]]] = None,
    outbound_filters: Iterable[OutboundFilter] = DFLT_OUTBOUND_FILTERS,
) -> TickReport:
    """One tick over ``subjects`` (slug to :class:`~liaise.subjects.Subject`), on the ledger ``store``.

    See the module docstring for the steps. The seams, each with a working default:

    - ``registry``: correspond's channel registry (its own when None);
    - ``processor``: a :class:`~liaise.processor.Processor`, by default
      ``ClaudeHeadless(runs_dir=<state_dir>/runs)``;
    - ``resolver``: who a channel address is (:func:`liaise.access.resolve_person`);
    - ``workspace``: a subject's checkout (:func:`liaise.workspace.workspace_for`), with
      its locks in ``<state_dir>/locks`` and Claude Code's session records read from
      ``sessions_dir`` (``~/.claude/sessions`` when None);
    - ``labeler``: the GitHub labels (``GhCli()``);
    - ``notify_fn``: ``(title, body, *, priority)``, by default :func:`liaise.notify.notify`
      on ``global_config.notify.ntfy_topic_env``.

    ``only`` is a slug or slugs to run alone; an unknown one raises
    :class:`~liaise.config.ConfigError`. ``now`` is the tick's clock (the current UTC
    time when None). Unless ``dry_run``, the tick holds the run lock in ``state_dir``
    (raising :class:`RunLockHeld` while another tick holds it) and stamps its start and
    end in ``store``.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    state_dir = Path(global_config.state_dir).expanduser()
    tick = _Tick(
        subjects,
        Ledger(ChainMap({}, store) if dry_run else store),
        slugs=_selected_slugs(subjects, only),
        registry=registry,
        processor=(
            processor
            if processor is not None
            else ClaudeHeadless(runs_dir=state_dir / DFLT_RUNS_SUBDIR)
        ),
        resolver=resolver,
        workspace=workspace,
        lock_dir=state_dir / DFLT_LOCKS_SUBDIR,
        sessions_dir=sessions_dir,
        labeler=labeler if labeler is not None else GhCli(),
        notify_fn=(
            notify_fn
            if notify_fn is not None
            else functools.partial(
                notify, topic_env=global_config.notify.ntfy_topic_env
            )
        ),
        now=now,
        dry_run=dry_run,
        outbound_filters=tuple(outbound_filters),
    )
    lock = nullcontext() if dry_run else _run_lock(run_lock_path(state_dir))
    stamps = nullcontext() if dry_run else _stamp_run(store, now=now)
    with lock, stamps:
        tick.say(f"tick at {now.isoformat()}" + (" [dry run]" if dry_run else ""))
        tick.intake_all()
        tick.reconcile()
        tick.start_all()
        tick.nudge_all()
        tick.project()
    return tick.report()


# ---- the run lock and the run stamps ----

#: The run lock's file under ``state_dir``.
RUN_LOCK_FILE = "run.lock"
#: Where the store keeps a tick's start and end.
RUN_STARTED_KEY = "run_started_at"
RUN_ENDED_KEY = "run_ended_at"
#: 0.0.3 and earlier kept one stamp: a pass's start, written once the pass had finished.
LEGACY_LAST_RUN_KEY = "last_run"


class RunLockHeld(RuntimeError):
    """Another live liaise process holds the run lock, so this tick did not start."""


def run_lock_path(state_dir: Union[str, os.PathLike]) -> Path:
    """Where a tick keeps its run lock.

    ``liaise status`` reads it too, to tell a running tick from one whose process died.
    """
    return Path(state_dir).expanduser() / RUN_LOCK_FILE


def _lock_owner(lock_path: Path) -> Optional[int]:
    """The pid of the live process holding ``lock_path``, or None.

    None for no lock, an unreadable one, or one left by a process that is gone. Liveness
    is :func:`liaise.workspace.pid_is_alive`'s, which is right on Windows too.
    """
    try:
        pid = int(lock_path.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_is_alive(pid) else None


@contextmanager
def _run_lock(lock_path: Path) -> Iterator[None]:
    """Hold the run lock for one tick, so one tick runs at a time (0.0.x L-3).

    A plain pid file, and a lock whose process is gone is reclaimed. It is advisory, which
    is enough to keep a manual ``liaise run`` from colliding with the scheduled one on the
    same machine. Raises :class:`RunLockHeld` while a live process holds it.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    other = _lock_owner(lock_path)
    if other is not None:
        raise RunLockHeld(
            f"another liaise run (pid {other}) is already in progress "
            f"(lock: {lock_path})"
        )
    lock_path.write_text(str(os.getpid()))
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class RunStamps:
    """When the latest tick that was not a dry run started and ended."""

    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    #: Whether a live process held the run lock when these were read; None when the lock
    #: was not checked.
    lock_held: Optional[bool] = None

    @property
    def state(self) -> str:
        """``never``, ``finished``, ``running`` or ``interrupted``.

        A tick that started later than the last one ended is ``running``, unless the lock
        was checked and no live process holds it. Then the tick was killed before it could
        stamp its end (a ``launchctl bootout``, a shutdown): ``interrupted``, since a job
        that stopped must not read as one that is busy.
        """
        if self.started_at is None:
            return "never"
        if self.ended_at is not None and self.started_at <= self.ended_at:
            return "finished"
        return "interrupted" if self.lock_held is False else "running"


def run_stamps(
    store: Mapping[str, Any], *, lock_path: Optional[Union[str, os.PathLike]] = None
) -> RunStamps:
    """The run stamps a tick keeps in ``store``.

    Pass ``lock_path`` (see :func:`run_lock_path`) to tell a running tick from a killed
    one. A store last written by 0.0.3 or earlier holds only ``last_run``, the start of a
    pass that had already finished, read here as a run that started and ended then.
    """
    started, ended = store.get(RUN_STARTED_KEY), store.get(RUN_ENDED_KEY)
    if started is None and ended is None:
        started = ended = store.get(LEGACY_LAST_RUN_KEY)
    return RunStamps(
        started_at=datetime.fromisoformat(started) if started else None,
        ended_at=datetime.fromisoformat(ended) if ended else None,
        lock_held=(
            None if lock_path is None else _lock_owner(Path(lock_path)) is not None
        ),
    )


@contextmanager
def _stamp_run(store: MutableMapping[str, Any], *, now: datetime) -> Iterator[None]:
    """Stamp the tick's start on entry and its end on exit, an exit by exception included.

    The end is ``now`` plus the monotonic time elapsed, so both stamps share the tick's
    own clock (an injected ``now`` included) and the end never precedes the start.
    """
    store[RUN_STARTED_KEY] = now.isoformat()
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = timedelta(seconds=time.monotonic() - started)
        store[RUN_ENDED_KEY] = (now + elapsed).isoformat()


def last_run_age(
    store: Mapping[str, Any], *, now: Optional[datetime] = None
) -> Optional[float]:
    """Seconds since the latest tick that was not a dry run started, or None before any."""
    started_at = run_stamps(store).started_at
    if started_at is None:
        return None
    now = now if now is not None else datetime.now(timezone.utc)
    return (now - started_at).total_seconds()


# ---- status ----


def _stamp_lines(stamps: RunStamps, *, now: datetime) -> list[str]:
    if stamps.state == "never":
        return ["last_run: never"]

    def fmt(stamp: Optional[datetime]) -> str:
        if stamp is None:
            return "never"
        age = _fmt_age((now - stamp).total_seconds())
        return f"{stamp.isoformat(timespec='seconds')} ({age} ago)"

    note = (
        " (no live liaise process holds the run lock)"
        if stamps.state == "interrupted"
        else ""
    )
    return [
        f"last_run: {stamps.state}{note}",
        f"  run_started_at: {fmt(stamps.started_at)}",
        f"  run_ended_at:   {fmt(stamps.ended_at)}",
    ]


def status_lines(
    subjects: Mapping[str, Subject],
    store: MutableMapping[str, Any],
    *,
    global_config: GlobalConfig,
    now: Optional[datetime] = None,
    recent: int = DFLT_STATUS_RECENT,
) -> list[str]:
    """What ``liaise status`` prints: what the ledger in ``store`` says, read only.

    The run stamps (``running``, ``interrupted`` or ``finished``, the lock checked in
    ``state_dir``), the holds, the runs in flight with their heartbeat age, each subject's
    cases by state and dispatches today, the unrouted queue (its size and the ``recent``
    latest), the drafts waiting for the operator, and the ``recent`` latest digest notes.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    ledger = Ledger(store)
    lock_path = run_lock_path(Path(global_config.state_dir).expanduser())
    lines = _stamp_lines(run_stamps(store, lock_path=lock_path), now=now)

    holds = sorted(ledger.holds(), key=lambda found: found.scope)
    lines.append(f"holds: {len(holds)}")
    for found in holds:
        since = f" since {found.set_at:%Y-%m-%d %H:%M}" if found.set_at else ""
        reason = f": {found.reason}" if found.reason else ""
        by = found.set_by or "unknown"
        lines.append(f"  {found.scope}: {found.mode}, set by {by}{since}{reason}")

    runs = sorted(ledger.runs(status=RUNNING), key=lambda run: run.run_id)
    lines.append(f"runs in flight: {len(runs)}")
    for run in runs:
        heartbeat = run.heartbeat_at or run.started_at
        lines.append(
            f"  {run.run_id} ({run.case_id}, {run.mode}): started "
            f"{_fmt_age((now - run.started_at).total_seconds())} ago, heartbeat "
            f"{_fmt_age((now - heartbeat).total_seconds())} ago"
        )

    cases = sorted(ledger.cases(), key=lambda case: (case.created_at, case.id))
    for slug in sorted(subjects):
        mine = [case for case in cases if case.subject == slug]
        today = ledger.daily_count(slug, now.date())
        cap = subjects[slug].policy.budget.daily_dispatches
        lines.append(
            f"subject {slug}: {len(mine)} case(s), {today}/{cap} dispatches today"
        )
        for state in CASE_STATES:
            ids = [case.id for case in mine if case.state == state]
            if ids:
                lines.append(f"  {state}: {', '.join(ids)}")

    unrouted = sorted(
        ledger.unrouted(), key=lambda item: str(item.get("at") or ""), reverse=True
    )
    lines.append(f"unrouted: {len(unrouted)}")
    for item in unrouted[:recent]:
        where = item.get("conversation") or item.get("url") or "?"
        lines.append(
            f"  {item.get('at')} {item.get('author')} on {where}: {item.get('reason')}"
        )

    drafts = [(case.id, draft) for case in cases for draft in case.drafts]
    lines.append(f"drafts waiting for the operator: {len(drafts)}")
    for case_id, draft in sorted(drafts, key=lambda pair: str(pair[1].get("at"))):
        to = draft.get("ref") or draft.get("recipient")
        lines.append(
            f"  {case_id} {draft.get('outcome')} to {to}: {draft.get('reason')}"
        )

    notes = sorted(
        (
            (case.id, entry)
            for case in cases
            for entry in case.entries
            if entry.kind == "note"
        ),
        key=lambda pair: pair[1].at,
        reverse=True,
    )
    lines.append(f"digest notes: {len(notes)}")
    lines += [
        f"  {case_id}: {_preview(entry.text)}" for case_id, entry in notes[:recent]
    ]
    return lines


# ---- the tick itself ----


class _Tick:
    """One pass's context and its steps. :func:`run_once` builds it and runs the steps."""

    def __init__(
        self,
        subjects: Mapping[str, Subject],
        ledger: Ledger,
        *,
        slugs: tuple[str, ...],
        registry: Optional[Mapping[str, Any]],
        processor: Any,
        resolver: Resolver,
        workspace: WorkspaceFactory,
        lock_dir: Path,
        sessions_dir: Optional[Union[str, os.PathLike]],
        labeler: GitHub,
        notify_fn: Callable[..., Any],
        now: datetime,
        dry_run: bool,
        outbound_filters: tuple[OutboundFilter, ...],
    ):
        self.subjects = subjects
        self.ledger = ledger
        self.slugs = slugs
        self.registry = registry
        self.processor = processor
        self.resolver = resolver
        self.workspace = workspace
        self.lock_dir = lock_dir
        self.sessions_dir = sessions_dir
        self.labeler = labeler
        self.notify_fn = notify_fn
        self.now = now
        self.dry_run = dry_run
        self.outbound_filters = outbound_filters
        self.lines: list[str] = []
        self.problems: list[str] = []
        self.dispatched: list[str] = []
        self.collected: list[str] = []
        self.sent: list[Outbound] = []
        self.diverted: list[Diversion] = []
        #: Cases this tick changed, in the order it changed them: the label projection's.
        self.touched: dict[str, None] = {}
        #: Per subject slug, the deliveries waiting for its batch deploy.
        self.pending: dict[str, list[_PendingDelivery]] = {}
        #: Runs whose checkout lock this tick released (in a dry run, would have).
        self.released: set[str] = set()

    # ---- shared plumbing ----

    def say(self, line: str) -> None:
        self.lines.append(line)

    def problem(self, text: str) -> None:
        self.problems.append(text)
        self.lines.append(f"  problem: {text}")

    def report(self) -> TickReport:
        return TickReport(
            plan_lines=tuple(self.lines),
            dispatched=tuple(self.dispatched),
            collected=tuple(self.collected),
            sent=tuple(self.sent),
            diverted=tuple(self.diverted),
            problems=tuple(self.problems),
            dry_run=self.dry_run,
        )

    def _case(self, case_id: str) -> Case:
        case = self.ledger.get_case(case_id)
        if case is None:
            raise KeyError(f"no case {case_id!r} in the ledger")
        return case

    def _save(self, case: Case) -> None:
        self.ledger.save_case(case)
        self.touched[case.id] = None

    def _entry(
        self,
        case_id: str,
        kind: str,
        *,
        text: Optional[str] = None,
        detail: Mapping[str, Any],
    ) -> None:
        entry = LedgerEntry(
            at=self.now, kind=kind, actor=TICK_ACTOR, text=text, detail=dict(detail)
        )
        self.ledger.append(case_id, entry)
        self.touched[case_id] = None

    def _transition(self, case_id: str, state: str, reason: str) -> None:
        before = self._case(case_id).state
        if before == state:
            return
        self.ledger.transition(
            case_id, state, at=self.now, actor=TICK_ACTOR, reason=reason
        )
        self.touched[case_id] = None
        self.say(f"  case {case_id}: {before} -> {state} ({reason})")

    def _defer(self, case_id: str, until: datetime) -> None:
        self._save(replace(self._case(case_id), defer_until=until))
        self.say(f"  case {case_id}: deferred until {until.isoformat()}")

    def _add_draft(self, case_id: str, draft: Mapping[str, Any]) -> None:
        case = self._case(case_id)
        self._save(replace(case, drafts=(*case.drafts, dict(draft))))

    def _notify(
        self, title: str, body: str, *, priority: str = DFLT_OPERATOR_PRIORITY
    ) -> None:
        if self.dry_run:
            self.say(f"  would notify the operator: {title}")
            return
        try:
            self.notify_fn(title, body, priority=priority)
        except Exception as error:  # a notifier must not end the tick
            self.problem(f"notifying the operator failed: {_error_text(error)}")

    def _call(self, verb: str, *args: Any, **kwargs: Any) -> tuple[Any, Optional[str]]:
        """``processor.<verb>(...)``, and the exception it raised as text, if any (#24)."""
        try:
            return getattr(self.processor, verb)(*args, **kwargs), None
        except Exception as error:
            return None, f"processor.{verb} raised {_error_text(error)}"

    def _scopes(
        self,
        subject: Subject,
        case: Case,
        *,
        effect: Optional[str] = None,
        processor: bool = True,
    ) -> tuple[str, ...]:
        return scopes_for(
            subject=subject.slug,
            person=case.reporter,
            repo=_github_repo(case),
            checkout=subject.workspace.path or None,
            effect=effect,
            processor=processor,
        )

    def _effect_hold(
        self, subject: Subject, case: Case, *, effect: Optional[str] = None
    ) -> Optional[Hold]:
        """The hold keeping a finished run's effects waiting. The processor has no part in them."""
        scopes = self._scopes(subject, case, effect=effect, processor=False)
        return blocking_hold(self.ledger, scopes, for_="effect")

    def _workspace(self, subject: Subject) -> Optional[SharedCheckout]:
        own_pids = {run.pid for run in self.ledger.runs(status=RUNNING) if run.pid}
        return self.workspace(
            subject,
            lock_dir=self.lock_dir,
            sessions_dir=self.sessions_dir,
            own_pids=own_pids,
        )

    # ---- 1. intake ----

    def intake_all(self) -> None:
        for slug in self.slugs:
            try:
                report = intake(
                    self.subjects[slug],
                    self.ledger,
                    registry=self.registry,
                    resolver=self.resolver,
                    now=self.now,
                    dry_run=self.dry_run,
                )
            except Exception as error:  # one subject's failure is not the tick's
                self.problem(f"intake of {slug} failed: {_error_text(error)}")
                continue
            self.lines += report.plan_lines()
            self.problems += report.problems
            for case in (*report.new_cases, *report.updated_cases):
                self.touched[case.id] = None

    # ---- 2. reconcile ----

    def reconcile(self) -> None:
        runs = sorted(self.ledger.runs(status=RUNNING), key=lambda run: run.run_id)
        orphans = [run for run in runs if run.subject not in self.subjects]
        for run in orphans:
            self.problem(
                f"run {run.run_id} belongs to subject {run.subject}, which is not "
                f"configured, so it cannot be reconciled"
            )
        mine = [run for run in runs if run.subject in self.slugs]
        self.say(f"reconcile: {len(mine)} run(s) in flight")
        for run in mine:
            try:
                self._reconcile_run(run)
            except Exception as error:  # one run's failure is not the tick's
                self.problem(
                    f"reconciling run {run.run_id} failed: {_error_text(error)}"
                )
        self._run_batches()

    def _reconcile_run(self, run: RunRecord) -> None:
        subject = self.subjects[run.subject]
        case = self._case(run.case_id)
        budget = subject.policy.budget
        label = f"  run {run.run_id} ({case.id})"
        timed_out = self.now - run.started_at > timedelta(
            minutes=budget.timeout_minutes
        )
        hold = (
            None
            if timed_out
            else blocking_hold(self.ledger, self._scopes(subject, case), for_="running")
        )
        if timed_out or hold is not None:
            mode = "now" if timed_out else "graceful"
            why = (
                f"past its {budget.timeout_minutes}-minute wall clock"
                if timed_out
                else f"held by {hold.scope} ({hold.mode})"
            )
            if self.dry_run:
                self.say(f"{label}: {why}: would cancel ({mode})")
                current, failure = self._call("status", run, persist=False)
            else:
                self.say(f"{label}: {why}: cancelling ({mode})")
                current, failure = self._call("cancel", run, mode=mode)
                if hold is not None and not self._cancelled_for_hold(case, run.run_id):
                    self._entry(
                        case.id,
                        "hold",
                        detail={
                            "scope": hold.scope,
                            "mode": hold.mode,
                            "cancelled_run": run.run_id,
                        },
                    )
        else:
            current, failure = self._call("status", run, persist=not self.dry_run)

        result: Optional[RunResult] = None
        if failure is None and current.status != FINISHED:
            self.ledger.save_run(
                replace(current, status=RUNNING, started_at=run.started_at)
            )
            if not (timed_out or hold is not None):
                beat = current.heartbeat_at or run.started_at
                age = _fmt_age((self.now - beat).total_seconds())
                self.say(f"{label}: running, heartbeat {age} ago")
            return
        if failure is None:
            result, failure = self._call(
                "collect", current, timed_out=timed_out, persist=not self.dry_run
            )
            if failure is None and result is None:
                self.ledger.save_run(
                    replace(current, status=RUNNING, started_at=run.started_at)
                )
                self.say(f"{label}: finished, its result not readable yet")
                return
        if failure is not None:
            self.problem(f"run {run.run_id}: {failure}; counted as {_CRASHED}")
            current = current if current is not None else run
            result = RunResult(
                run_id=run.run_id, error=_CRASHED, session_id=run.session_id
            )
        finished = replace(
            current,
            status=FINISHED,
            started_at=run.started_at,
            ended_at=current.ended_at or self.now,
        )
        self._collected(subject, case, finished, result)

    @staticmethod
    def _cancelled_for_hold(case: Case, run_id: str) -> bool:
        return any(
            entry.kind == "hold" and entry.detail.get("cancelled_run") == run_id
            for entry in case.entries
        )

    def _release(self, subject: Subject, run_id: str) -> None:
        self.released.add(run_id)
        if self.dry_run:
            return
        checkout = self._workspace(subject)
        if checkout is not None:
            checkout.release(run_id=run_id)

    def _collected(
        self, subject: Subject, case: Case, run: RunRecord, result: RunResult
    ) -> None:
        self.ledger.save_run(run)
        self.collected.append(run.run_id)
        self._release(subject, run.run_id)
        if result.session_id and result.session_id != case.session_id:
            self._save(replace(self._case(case.id), session_id=result.session_id))
        error = result.error or (None if result.outcomes else _NEEDS_HUMAN)
        cancelled = error == _CRASHED and self._cancelled_for_hold(case, run.run_id)
        defer = None
        if error and not cancelled and error in ERROR_ACTIONS:
            attempt = max(len(_run_starts(case)) - 1, 0)
            defer = defer_for_error(
                error, now=self.now, attempt=attempt, rate_limit=result.rate_limit
            )
        quota_notified = error == _QUOTA_EXHAUSTED and self._quota_notified(defer)
        detail = {
            "event": RUN_COLLECTED,
            "run_id": run.run_id,
            "error": error,
            "cost_usd": result.cost_usd,
        }
        if defer is not None:
            detail["defer_until"] = defer.isoformat()
        self._entry(case.id, "run", text=result.summary or None, detail=detail)
        self.say(
            f"  run {run.run_id} ({case.id}): collected, "
            f"{'cancelled for a hold' if cancelled else error or 'no error'}"
        )
        if cancelled:
            self._transition(
                case.id, INTAKE, f"run {run.run_id} was cancelled for a hold"
            )
            self._notify(
                f"liaise: {case.id}'s run was cancelled",
                f"Run {run.run_id} stopped for a cancel hold. Its session is kept, "
                f"so the case resumes once the hold is lifted.",
            )
        elif error:
            self._apply_error(
                subject,
                case.id,
                error,
                source=f"run {run.run_id}",
                defer=defer,
                uncount_run=run.run_id,
                notify_operator=not quota_notified,
            )
        else:
            self._carry_out(subject, case.id, result.outcomes, run_id=run.run_id)

    def _quota_notified(self, reset: Optional[datetime]) -> bool:
        """Whether a run already reported this quota reset, so the operator heard once."""
        if reset is None:
            return False
        stamp = reset.isoformat()
        return any(
            entry.kind == "run"
            and entry.detail.get("error") == _QUOTA_EXHAUSTED
            and entry.detail.get("defer_until") == stamp
            for case in self.ledger.cases()
            for entry in case.entries
        )

    def _apply_error(
        self,
        subject: Subject,
        case_id: str,
        error: str,
        *,
        source: str,
        defer: Optional[datetime] = None,
        uncount_run: Optional[str] = None,
        notify_operator: bool = True,
    ) -> None:
        """What :data:`liaise.errors.ERROR_ACTIONS` says for ``error``; an unknown class is ``crashed``.

        An error whose action holds a scope itself tells the operator only when that hold
        is new, so a login that has expired is one notification, not one per run.
        """
        action = ERROR_ACTIONS.get(error) or ERROR_ACTIONS[_CRASHED]
        reason = f"{source}: {error}"
        if defer is not None:
            self._defer(case_id, defer)
        if action.state is not None:
            self._transition(case_id, action.state, reason)
        if action.auto_hold:
            placed, created = auto_hold(
                self.ledger, action.auto_hold, error_class=error, now=self.now
            )
            self._entry(
                case_id,
                "hold",
                detail={
                    "scope": placed.scope,
                    "mode": placed.mode,
                    "set_by": placed.set_by,
                },
            )
            self.say(f"  hold {placed.scope}: {placed.mode} ({placed.set_by})")
            notify_operator = notify_operator and created
        if not action.counts and uncount_run is not None:
            self._uncount(subject.slug, case_id, uncount_run)
        if action.notify and notify_operator:
            case = self._case(case_id)
            self._notify(
                f"liaise: {case_id} {error}",
                f"{reason}\ncase: {case_id} ({case.state})\n"
                f"conversations: {', '.join(case.conversations)}\n"
                f"Nothing was sent to the partner about it.",
            )

    def _uncount(self, slug: str, case_id: str, run_id: str) -> None:
        """Take back the daily dispatch counted when ``run_id`` started."""
        day = next(
            (
                entry.detail.get("day")
                for entry in reversed(_run_starts(self._case(case_id)))
                if entry.detail.get("run_id") == run_id
            ),
            None,
        )
        if day is None:
            return
        if self.ledger.daily_count(slug, day) > 0:
            self.ledger.decrement_daily(slug, day)
            self.say(f"  {slug}: run {run_id} not counted against {day}'s cap")

    # ---- 2b. a finished run's outcomes ----

    def _carry_out(
        self,
        subject: Subject,
        case_id: str,
        outcomes: Sequence[Outcome],
        *,
        run_id: str,
    ) -> None:
        for outcome in outcomes:
            self._entry(
                case_id,
                "outcome",
                text=outcome.text or None,
                detail={
                    "kind": outcome.kind,
                    "questions": list(outcome.questions),
                    "reason": outcome.reason,
                    "run_id": run_id,
                },
            )
            summary = outcome.text or outcome.reason or "; ".join(outcome.questions)
            self.say(f"  outcome {outcome.kind}: {_preview(summary)}")
        try:
            actions = plan_outcomes(
                self._case(case_id), outcomes, subject, now=self.now
            )
        except ValueError as error:
            self.problem(f"planning {case_id}'s outcomes failed: {error}")
            self._apply_error(subject, case_id, _NEEDS_HUMAN, source=f"run {run_id}")
            return
        self._execute(subject, case_id, actions, run_id=run_id)

    def _execute(
        self,
        subject: Subject,
        case_id: str,
        actions: Sequence[Action],
        *,
        run_id: str,
    ) -> None:
        """Carry out a run's planned actions in order: the S5b tick rules."""
        case = self._case(case_id)
        planned_send = any(isinstance(action, Send) for action in actions)
        held: list[str] = []
        final: Union[None, tuple[str, str], _DeliveryGroup] = None
        deferred: Optional[datetime] = None
        batched: list[_DeliveryGroup] = []
        for item in _group_deliveries(actions):
            if isinstance(item, _DeliveryGroup):
                hold = self._effect_hold(subject, case, effect=item.deliver.kind)
                if hold is not None:
                    held.append(hold.scope)
                    self.say(f"  deliver ({item.deliver.kind}): held by {hold.scope}")
                    for send in item.sends:
                        self._hold_send(send, hold)
                elif _is_batch(item.deliver):
                    batched.append(item)
                    final = item
                    self.say(
                        f"  deliver: waits for {subject.slug}'s batch deploy, after "
                        f"every run is collected"
                    )
                else:
                    for send in item.sends:
                        self._send(subject, self._try_it(item.deliver, send))
                    if item.transition is not None:
                        final = (item.transition.state, item.transition.reason)
            elif isinstance(item, Send):
                hold = self._effect_hold(subject, case)
                if hold is not None:
                    held.append(hold.scope)
                    self._hold_send(item, hold)
                else:
                    self._send(subject, item)
            elif isinstance(item, Transition):
                final = (item.state, item.reason)
            elif isinstance(item, Defer):
                final = (INTAKE, f"deferred: {item.reason}")
                deferred = self.now + DFLT_DEFER
            elif isinstance(item, NotifyOperator):
                self._notify(item.title, item.body, priority=item.priority)
            elif isinstance(item, StoreDraft):
                self._add_draft(case_id, item.draft)
                self.say(f"  draft kept for the operator: {item.draft.get('reason')}")
            elif isinstance(item, DigestNote):
                self._entry(case_id, "note", text=item.text, detail={"run_id": run_id})
        for group in batched:
            self.pending.setdefault(subject.slug, []).append(
                _PendingDelivery(case_id, group, applies_state=final is group)
            )
        if deferred is not None:
            self._defer(case_id, deferred)
        if held:
            scopes = ", ".join(dict.fromkeys(held))
            self._transition(case_id, NEEDS_OWNER, f"effects held by {scopes}")
            self._notify(
                f"liaise: {case_id}'s effects are held",
                f"Run {run_id} finished, but {scopes} holds its effects. What it would "
                f"have sent is kept on the case as drafts.",
            )
        elif isinstance(final, _DeliveryGroup):
            pass  # the batch deploy decides the state
        elif final is not None:
            self._transition(case_id, *final)
        else:
            state = NEEDS_PARTNER if planned_send else NEEDS_OWNER
            self._transition(case_id, state, f"run {run_id} ended")

    def _hold_send(self, send: Send, hold: Hold) -> None:
        reason = f"held: {hold.scope}"
        draft = make_draft(
            at=self.now,
            outcome=send.purpose,
            recipient=send.recipient,
            ref=send.ref,
            text=send.text,
            reason=reason,
        )
        self._add_draft(send.case_id, draft)
        self.diverted.append(Diversion(send, reason))
        self.say(f"  {send.purpose} to {send.ref}: {reason}, kept as a draft")

    @staticmethod
    def _try_it(deliver: Deliver, send: Send) -> Send:
        """A deployed change's message ends by telling the partner it is live."""
        if deliver.kind != DEPLOY_DELIVERY:
            return send
        text = f"{send.text}\n\n{TRY_IT_MESSAGE}" if send.text else TRY_IT_MESSAGE
        return replace(send, text=text)

    def _send(self, subject: Subject, send: Send) -> bool:
        """Put ``send`` through the gate, then send it or keep it as a draft; True once sent."""
        case = self._case(send.case_id)
        decision = run_gate(
            send,
            GateContext(subject=subject, case=case, now=self.now),
            outbound_filters=self.outbound_filters,
        )
        head = f"  gate {send.purpose} to {send.ref}"
        detail = {
            "purpose": send.purpose,
            "ref": send.ref,
            "notes": list(decision.notes),
        }
        notes = [f"    note: {note}" for note in decision.notes]
        if decision.send is None:
            draft = make_draft(
                at=self.now,
                outcome=send.purpose,
                recipient=send.recipient,
                ref=send.ref,
                text=send.text,
                reason=decision.diverted,
                notes=decision.notes,
            )
            self._add_draft(case.id, draft)
            self._entry(
                case.id,
                "gate",
                text=send.text,
                detail={**detail, "decision": "divert", "reason": decision.diverted},
            )
            self.diverted.append(Diversion(send, decision.diverted))
            self.say(f"{head}: diverted ({decision.diverted}), kept as a draft")
            self.lines += notes
            self._notify(
                f"liaise: a draft for {case.id} waits for you",
                f"why: {decision.diverted}\nto: {send.recipient} at {send.ref}\n\n"
                f"{send.text}",
            )
            return False
        outbound = decision.send
        try:
            result = correspond.send(
                outbound.ref,
                outbound.text,
                dry_run=self.dry_run,
                registry=self.registry,
            )
            failure = None if result.ok else (result.error or "the channel refused it")
        except Exception as error:  # an unknown channel, an adapter that raised
            result, failure = None, _error_text(error)
        if failure is not None:
            reason = f"send failed: {failure}"
            self._entry(
                case.id,
                "gate",
                text=outbound.text,
                detail={**detail, "decision": "send", "error": failure},
            )
            self._add_draft(
                case.id,
                make_draft(
                    at=self.now,
                    outcome=outbound.purpose,
                    recipient=outbound.recipient,
                    ref=outbound.ref,
                    text=outbound.text,
                    reason=reason,
                    notes=decision.notes,
                ),
            )
            self.problem(f"{case.id}: {outbound.purpose} to {outbound.ref}: {reason}")
            self._notify(
                f"liaise: a message for {case.id} was not sent",
                f"{reason}\nto: {outbound.recipient} at {outbound.ref}\n\n"
                f"{outbound.text}",
            )
            return False
        self._entry(
            case.id,
            "gate",
            text=outbound.text,
            detail={**detail, "decision": "send", "url": result.url},
        )
        self.sent.append(outbound)
        verb = "would send" if self.dry_run else "sent"
        self.say(f"{head}: {verb}: {_preview(outbound.text)}")
        self.lines += notes
        return True

    def _send_own(
        self, subject: Subject, case: Case, text: str, *, purpose: str
    ) -> None:
        """Send one of the tick's own messages to the reporter, as a ``reply`` would go."""
        actions = plan_outcomes(
            case, (Outcome(kind=_REPLY_KIND, text=text),), subject, now=self.now
        )
        for action in actions:
            if isinstance(action, Send):
                send = replace(action, purpose=purpose)
                hold = self._effect_hold(subject, case)
                if hold is None:
                    self._send(subject, send)
                    continue
                self._hold_send(send, hold)
                self._notify(
                    f"liaise: {case.id}'s {purpose} message is held",
                    f"{hold.scope} holds effects; the message is kept as a draft.",
                )
            elif isinstance(action, StoreDraft):
                self._add_draft(case.id, {**action.draft, "outcome": purpose})
            elif isinstance(action, NotifyOperator):
                self._notify(action.title, action.body, priority=action.priority)

    # ---- 2c. batch deploys ----

    def _run_batches(self) -> None:
        for slug in sorted(self.pending):
            subject, pending = self.subjects[slug], self.pending[slug]
            case_ids = ", ".join(item.case_id for item in pending)
            ok, reason, output = self._deploy(subject)
            if ok:
                verb = "would run" if self.dry_run else "ran"
                self.say(
                    f"deploy {slug}: {verb} {subject.delivery.command} for {case_ids}"
                )
                for item in pending:
                    for send in item.group.sends:
                        self._send(subject, self._try_it(item.group.deliver, send))
                    transition = item.group.transition
                    if item.applies_state and transition is not None:
                        self._transition(
                            item.case_id, transition.state, transition.reason
                        )
                continue
            error = classify_delivery_failure(output)
            self.say(f"deploy {slug}: failed for {case_ids}: {reason}")
            if error is not None:
                action = ERROR_ACTIONS[error]
                placed, _ = auto_hold(
                    self.ledger, action.auto_hold, error_class=error, now=self.now
                )
                self.say(f"  hold {placed.scope}: {placed.mode} ({placed.set_by})")
            for item in pending:
                if error is not None:
                    self._entry(
                        item.case_id,
                        "hold",
                        detail={"scope": placed.scope, "set_by": placed.set_by},
                    )
                self._transition(item.case_id, NEEDS_OWNER, f"deploy failed: {reason}")
            self._notify(
                "liaise: batch landed but did not deploy",
                f"{slug}: {case_ids} landed, but {reason}. Nothing was sent to the "
                f"partner." + (f" Error class: {error}." if error else ""),
            )

    def _deploy(self, subject: Subject) -> tuple[bool, str, str]:
        """Run the subject's deploy command once: ``(ok, reason, output)``."""
        command = subject.delivery.command
        if not command:
            return False, "no deploy command is configured for this subject", ""
        if self.dry_run:
            return True, "", ""
        path = subject.workspace.path
        try:
            done = subprocess.run(
                shlex.split(command),
                cwd=Path(path).expanduser() if path else None,
                timeout=subject.policy.budget.timeout_minutes * _SECONDS_PER_MINUTE,
                capture_output=True,
                text=True,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            return False, f"the deploy command did not finish ({error})", str(error)
        output = f"{done.stdout or ''}\n{done.stderr or ''}".strip()
        if done.returncode != 0:
            shown = output[:DELIVERY_OUTPUT_CHARS]
            return (
                False,
                f"the deploy command exited {done.returncode}: {shown}",
                output,
            )
        return True, "", output

    # ---- 3. start ----

    def start_all(self) -> None:
        for slug in self.slugs:
            subject = self.subjects[slug]
            cases = sorted(
                self.ledger.cases(subject=slug), key=lambda c: (c.created_at, c.id)
            )
            eligible = [case for case in cases if case.state in ELIGIBLE_STATES]
            self.say(f"start {slug}: {len(eligible)} eligible case(s)")
            for case in eligible:
                try:
                    self._consider(subject, case)
                except Exception as error:  # one case's failure is not the tick's
                    self.problem(f"starting {case.id} failed: {_error_text(error)}")

    def _consider(self, subject: Subject, case: Case) -> None:
        label = f"  case {case.id} ({case.state})"
        policy, budget = subject.policy, subject.policy.budget
        in_flight = [
            r for r in self.ledger.runs(status=RUNNING) if r.case_id == case.id
        ]
        if in_flight:
            self.say(f"{label}: run {in_flight[0].run_id} is still in flight")
            return
        if case.defer_until is not None and case.defer_until > self.now:
            self.say(f"{label}: deferred until {case.defer_until.isoformat()}")
            return
        partners = {case.reporter}
        starts = _run_starts(case)
        if (
            case.state == NEEDS_PARTNER
            and starts
            and last_partner_activity(case, partner_persons=partners) <= starts[-1].at
        ):
            self.say(f"{label}: awaiting the partner's reply since the last run")
            return
        readiness = compute_readiness(
            case,
            quiet_minutes=policy.readiness.quiet_minutes,
            go_minutes=policy.readiness.go_minutes,
            markers=policy.readiness.markers,
            partner_persons=partners,
            now=self.now,
        )
        if readiness.paused:
            if case.state == PAUSED:
                self.say(f"{label}: paused ({readiness.reason})")
            else:
                self._transition(case.id, PAUSED, readiness.reason)
            return
        if not readiness.ready:
            countdown = _fmt_age(readiness.countdown.total_seconds())
            self.say(f"{label}: not ready ({readiness.reason}, {countdown} to go)")
            return
        passed = [f"ready ({readiness.reason})"]

        # An automatic processor hold is probed with preflight, and lifted once it passes
        # again, but only AUTH_PROBE_INTERVAL after it was set: a login that has expired
        # where preflight cannot see it would otherwise cost a failed run every tick.
        processor_hold = self.ledger.get_hold(PROCESSOR_SCOPE)
        auto_processor = (
            _is_auto(processor_hold)
            and processor_hold.set_at is not None
            and self.now - processor_hold.set_at >= AUTH_PROBE_INTERVAL
        )
        scopes = self._scopes(subject, case, processor=not auto_processor)
        hold = blocking_hold(self.ledger, scopes, for_="start")
        if hold is not None:
            self.say(
                f"{label}: held by {hold.scope} ({hold.mode}) {hold.reason}".rstrip()
            )
            return
        passed.append("no hold")

        refusal = self._authorization_refusal(subject, case)
        if refusal is not None:
            self._transition(case.id, NEEDS_OWNER, f"cannot start work: {refusal}")
            self._notify(
                f"liaise: {case.id} needs you before work starts",
                f"{refusal}\nconversations: {', '.join(case.conversations)}",
            )
            return
        passed.append(f"{case.reporter} may {REQUEST_WORK}")

        today = self.ledger.daily_count(subject.slug, self.now.date())
        if today >= budget.daily_dispatches:
            if case.state == BUDGET:
                self.say(
                    f"{label}: still over the daily cap ({today}/{budget.daily_dispatches})"
                )
            else:
                reason = f"daily cap of {budget.daily_dispatches} dispatches reached"
                self._transition(case.id, BUDGET, reason)
                self._send_own(
                    subject, self._case(case.id), BUDGET_MESSAGE, purpose=BUDGET_PURPOSE
                )
            self._notify_daily_cap(subject, case, count=today)
            return
        running = sum(
            1 for r in self.ledger.runs(status=RUNNING) if r.subject == subject.slug
        )
        if running >= budget.concurrent:
            self.say(
                f"{label}: ready, but {running} run(s) in flight "
                f"(concurrent cap {budget.concurrent})"
            )
            return
        passed.append(
            f"within budget ({today}/{budget.daily_dispatches} today, "
            f"{running}/{budget.concurrent} running)"
        )

        checkout = self._workspace(subject)
        if checkout is None:
            self.problem(
                f"{subject.source or subject.slug} names no workspace path, so "
                f"{case.id} has nowhere to run"
            )
            return
        run_id = self._next_run_id(case)
        mode = RESUME if case.session_id else FRESH
        try:
            job = Job(
                run_id=run_id,
                case_id=case.id,
                subject=subject.slug,
                prompt=compose_case_prompt(subject, case, mode),
                cwd=str(checkout.path),
                permission_mode=subject.processor.permission_mode,
                timeout_minutes=budget.timeout_minutes,
                json_schema=OUTCOME_SCHEMA,
                session_id=case.session_id,
            )
        except Exception as error:  # a brief that cannot be read, say
            self._start_failed(
                subject, case, run_id, f"composing the prompt: {_error_text(error)}"
            )
            return

        health, failure = self._call("preflight", job)
        if failure is not None:
            self._start_failed(subject, case, run_id, failure)
            return
        if not health.ok:
            if auto_processor:
                self.say(
                    f"{label}: held by {PROCESSOR_SCOPE} (set automatically), and "
                    f"preflight still fails ({health.error})"
                )
            else:
                self._preflight_failed(subject, case, health)
            return
        if auto_processor:
            for lifted in release_auto_holds(self.ledger, PROCESSOR_SCOPE):
                self._entry(
                    case.id,
                    "hold",
                    detail={"scope": lifted.scope, "released": lifted.set_by},
                )
                self.say(
                    f"  released the hold on {lifted.scope} ({lifted.set_by}): "
                    f"preflight passes again"
                )
            hold = blocking_hold(self.ledger, self._scopes(subject, case), for_="start")
            if hold is not None:
                self.say(f"{label}: held by {hold.scope} ({hold.mode})")
                return
        passed.append("preflight ok")

        conflict = checkout.conflict() or self._lock_conflict(checkout, run_id)
        if conflict is not None:
            self.say(f"{label}: {_WORKSPACE_CONFLICT}: {conflict}")
            self._defer(case.id, defer_for_error(_WORKSPACE_CONFLICT, now=self.now))
            return
        passed.append("workspace free")
        self.say(f"{label}: {', '.join(passed)}")
        self._dispatch(subject, case, job, checkout, mode=mode)

    def _notify_daily_cap(self, subject: Subject, case: Case, *, count: int) -> None:
        """Tell the operator the daily cap holds ``subject``'s work back: once a day (0.0.x M-8).

        The ledger keeps the day the operator was told, so a second capped case that day
        adds no notification, and the first one the next day does.
        """
        day = self.now.date()
        if self.ledger.daily_cap_notified(subject.slug, day):
            return
        self.ledger.mark_daily_cap_notified(subject.slug, day, at=self.now)
        cap = subject.policy.budget.daily_dispatches
        self._notify(
            f"liaise: {subject.slug} reached its daily cap",
            f"{count} of {cap} dispatches used today, so {case.id} waits in budget. "
            f"Capped cases start again tomorrow; to allow more, raise "
            f"policy.budget.daily_dispatches in subjects/{subject.slug}.toml.",
            priority=DAILY_CAP_PRIORITY,
        )

    def _authorization_refusal(self, subject: Subject, case: Case) -> Optional[str]:
        """Why the reporter may not have work started, or None when they may."""
        role = subject.policy.roles.get(case.reporter)
        if REQUEST_WORK not in subject.permissions_for(role):
            return f"{case.reporter}'s role ({role or 'none'}) does not grant {REQUEST_WORK}"
        grade = _latest_grade(case, case.reporter)
        if grade is None:
            return f"{case.reporter} has no message on the case to grade"
        if not subject.accepts(REQUEST_WORK, grade):
            return (
                f"the grade of {case.reporter}'s latest message ({grade}) is not "
                f"accepted for {REQUEST_WORK}"
            )
        return None

    def _next_run_id(self, case: Case) -> str:
        number = len(_run_starts(case)) + 1
        while self.ledger.get_run(f"{case.id}-r{number}") is not None:
            number += 1
        return f"{case.id}-r{number}"

    def _lock_conflict(self, checkout: SharedCheckout, run_id: str) -> Optional[str]:
        holder = checkout.holder()
        if (
            holder is None
            or holder.get("run_id") == run_id
            or holder.get("run_id") in self.released
            or not pid_is_alive(holder.get("pid"))
        ):
            return None
        return f"the checkout is locked by run {holder.get('run_id')}"

    def _preflight_failed(self, subject: Subject, case: Case, health: Any) -> None:
        label = f"  case {case.id} ({case.state})"
        if health.error is None:
            self.say(f"{label}: preflight not ok")
            if health.defer_until is not None:
                self._defer(case.id, health.defer_until)
            return
        self.say(f"{label}: preflight: {health.error}")
        defer = health.defer_until
        if defer is None and health.error in ERROR_ACTIONS:
            defer = defer_for_error(health.error, now=self.now)
        self._apply_error(
            subject, case.id, health.error, source="preflight", defer=defer
        )

    def _start_failed(
        self, subject: Subject, case: Case, run_id: str, why: str
    ) -> None:
        self.problem(f"{case.id} did not start: {why}")
        self._entry(
            case.id,
            "run",
            detail={"event": RUN_START_FAILED, "run_id": run_id, "error": _CRASHED},
            text=why,
        )
        self._apply_error(subject, case.id, _CRASHED, source=f"start of {run_id}")

    def _dispatch(
        self,
        subject: Subject,
        case: Case,
        job: Job,
        checkout: SharedCheckout,
        *,
        mode: str,
    ) -> None:
        if self.dry_run:
            record = RunRecord(
                run_id=job.run_id,
                case_id=case.id,
                subject=subject.slug,
                mode=mode,
                status=RUNNING,
                started_at=self.now,
            )
            self.say(f"  would dispatch {case.id} as run {job.run_id} ({mode})")
        else:
            if not checkout.acquire(run_id=job.run_id, pid=os.getpid(), now=self.now):
                self.say(
                    f"  case {case.id}: {_WORKSPACE_CONFLICT}: the checkout is locked"
                )
                self._defer(case.id, defer_for_error(_WORKSPACE_CONFLICT, now=self.now))
                return
            if job.session_id:
                record, failure = self._call("resume", job.session_id, job)
            else:
                record, failure = self._call("start", job)
            if failure is None and not isinstance(record, RunRecord):
                failure = f"processor returned {type(record).__name__}, not a RunRecord"
            if failure is not None:
                checkout.release(run_id=job.run_id)
                self._start_failed(subject, case, job.run_id, failure)
                return
            if record.pid:
                checkout.acquire(run_id=job.run_id, pid=record.pid, now=self.now)
            self.say(f"  dispatched {case.id} as run {job.run_id} ({mode})")
        day = self.now.date().isoformat()
        self.ledger.save_run(replace(record, status=RUNNING, started_at=self.now))
        self._entry(
            case.id,
            "run",
            detail={
                "event": RUN_STARTED,
                "run_id": job.run_id,
                "mode": mode,
                "day": day,
            },
        )
        if case.defer_until is not None:
            self._save(replace(self._case(case.id), defer_until=None))
        self._transition(case.id, WORKING, f"dispatched run {job.run_id} ({mode})")
        self.ledger.increment_daily(subject.slug, day)
        self.dispatched.append(job.run_id)

    # ---- 4. nudge ----

    def nudge_all(self) -> None:
        for slug in self.slugs:
            subject = self.subjects[slug]
            quiet = timedelta(days=subject.policy.deployed_nudge_days)
            deployed = sorted(
                self.ledger.cases(subject=slug, state=DEPLOYED), key=lambda c: c.id
            )
            for case in deployed:
                try:
                    self._nudge(subject, case, quiet=quiet)
                except Exception as error:  # one case's failure is not the tick's
                    self.problem(f"nudging {case.id} failed: {_error_text(error)}")

    def _nudge(self, subject: Subject, case: Case, *, quiet: timedelta) -> None:
        since_deployed = _entered_state_at(case, DEPLOYED)
        if any(
            entry.kind == "gate"
            and entry.detail.get("nudged")
            and entry.at >= since_deployed
            for entry in case.entries
        ):
            return
        activity = last_partner_activity(case, partner_persons={case.reporter})
        since = max(activity, since_deployed)
        if self.now - since < quiet:
            return
        self.say(f"nudge {case.id}: deployed, and quiet since {since.isoformat()}")
        self._send_own(subject, case, NUDGE_MESSAGE, purpose=NUDGE_PURPOSE)
        self._entry(case.id, "gate", detail={"purpose": NUDGE_PURPOSE, "nudged": True})

    # ---- 5. labels ----

    def project(self) -> None:
        targets = [
            case
            for case in map(self.ledger.get_case, list(self.touched))
            if case is not None
            and case.subject in self.subjects
            and any(github_issue(ref) for ref in case.conversations)
        ]
        self.say(f"labels: {len(targets)} case(s)")
        for case in targets:
            try:
                lines = project_labels(
                    case,
                    self.subjects[case.subject],
                    labeler=self.labeler,
                    dry_run=self.dry_run,
                )
            except Exception as error:  # a GitHubError, usually: labels never set up
                why = _error_text(error)
                self.ledger.append(
                    case.id,
                    LedgerEntry(
                        at=self.now,
                        kind="projection",
                        actor=TICK_ACTOR,
                        detail={"state": case.state, "error": why},
                    ),
                )
                self.problem(f"labelling {case.id} {case.state} failed: {why}")
                continue
            self.lines += [f"  {line}" for line in lines]
