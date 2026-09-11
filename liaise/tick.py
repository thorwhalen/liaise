"""The tick: one pass of liaise 0.1's loop (design §3.1).

:func:`run_once` takes in what arrived, reconciles the runs in flight, starts the cases
that are ready, nudges quiet deliveries, and shows each touched case's state on its GitHub
labels::

    1. intake     each subject's bindings, through correspond (liaise.intake)
    2. reconcile  each run the tick has not collected: cancelled for a cancel hold or its
                  wall clock, refreshed, and collected once finished, or given up as
                  timed_out LOST_RUN_DEADLINE past its wall clock. An error takes its
                  action from liaise.errors; a success's outcomes are planned
                  (liaise.outcomes) and carried out, every message through the gate
                  (liaise.gate). A deploy per issue runs right after its case's outcomes
                  and batch deploys run last, once per subject; nothing tells a partner a
                  change is live before its deploy succeeded. Last, each case left working
                  with no run in flight, its run lost, goes to needs-owner.
    3. start      each ready case, in the order the triage seam gives, that passes holds,
                  authorization, budget, an open GitHub issue, preflight and the workspace
                  check, as a detached processor run
    4. nudge      each deployed case its partner has gone quiet on, once, unless its issue
                  is closed
    5. project    the state label of each case this tick touched, and of each whose state
                  is not the one last projected (liaise.projection)

The tick keeps its own clock: ``now`` stamps every entry, and a run's wall clock counts
from the tick that started it. The ledger's record of a run says ``running`` until the
tick collects it, whatever the processor says, so a run that ended at once (a spawn that
failed, an ``EchoProcessor`` run) is still collected, on the next tick. Any exception a
processor verb raises is a ``crashed`` run, never the tick's end (#24). One case's failure
is a problem line, never the other cases' end: a checkout release or a deploy that raises
hands its cases to the owner. A notification about a message kept from the partner never
carries that message.

**Dry run.** The ledger is ``Ledger(ChainMap({}, store))``: every step runs on real state,
and every write vanishes with the overlay. Nothing is sent (``correspond.send`` gets
``dry_run=True``), labelled, started, cancelled, deployed, locked, stamped, notified or
written (the processor is asked with ``persist=False``), and preflight, which runs a
process, is skipped. The report's plan lines say what would be.

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
from uuid import uuid4

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
from liaise.intake import (
    ADOPTED_EVENT,
    CLOSED_STATE,
    LIAISE_ACTOR,
    OPENING_ID_PREFIX,
    intake,
)
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
from liaise.subjects import DELIVERY_KINDS, DELIVERY_PERS, Subject
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
#: How long past its wall clock the tick waits for a run it cancelled to stop. Past that,
#: the run is finished as ``timed_out`` whether or not its pid is alive, since by then
#: the pid may be another process's, and its case goes to the owner.
LOST_RUN_DEADLINE = timedelta(minutes=10)
#: A deploy that runs the subject's command, and a delivery that stops at a pull request.
DEPLOY_DELIVERY, PR_ONLY_DELIVERY = DELIVERY_KINDS
#: A deploy that runs once per tick, after every case of the subject has been collected,
#: and one that runs for each case right after its outcomes.
BATCH_DELIVERY_PER, ISSUE_DELIVERY_PER = DELIVERY_PERS
#: How many hex digits of a uuid4 end a run id, so that no two runs share one.
RUN_ID_SUFFIX_DIGITS = 8

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
#: Where a notification about a message kept from the partner points the operator. It
#: never carries the message, which may hold what the gate kept back.
SEE_STATUS = "see liaise status"

#: What a ``run`` entry records: a start, a start that failed, a collection...
RUN_STARTED = "started"
RUN_START_FAILED = "start_failed"
RUN_COLLECTED = "collected"
#: ...a case found ``working`` with no run in flight, its run lost...
RUN_LOST = "lost"
#: ...and the case's GitHub issue found closed, then open again, each once per change. A
#: case whose issue is closed is neither started nor nudged.
RUN_ISSUE_CLOSED = "issue_closed"
RUN_ISSUE_REOPENED = "issue_reopened"

#: How many characters of a message a plan line shows.
PLAN_TEXT_CHARS = 72
#: How many characters of a failed deploy's output its reason carries.
DELIVERY_OUTPUT_CHARS = 300

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_CRASHED = "crashed"
_NEEDS_HUMAN = "needs_human"
_QUOTA_EXHAUSTED = "quota_exhausted"
_TIMED_OUT = "timed_out"
_WORKSPACE_CONFLICT = "workspace_conflict"
_REPLY_KIND = "reply"

#: ``(subject, *, lock_dir, sessions_dir, own_pids) -> SharedCheckout | None``: the
#: workspace seam (see :func:`liaise.workspace.workspace_for`).
WorkspaceFactory = Callable[..., Optional[SharedCheckout]]
#: ``(the ready cases, in the tick's order) -> groups of them, in the order to start``: the
#: triage seam (#19). The tick starts the cases group by group, each group in its order,
#: and a case left out is not started this tick. None keeps the tick's own order.
Triage = Callable[[Sequence[Case]], Iterable[Iterable[Case]]]


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


def _github_issue_ref(case: Case) -> Optional[str]:
    """The case's first GitHub issue conversation, as ``github:owner/repo#N``, or None."""
    return next((ref for ref in case.conversations if github_issue(ref)), None)


def _last_handover(case: Case) -> Optional[LedgerEntry]:
    """The case's latest ``run`` entry that handed the turn to the partner, or None.

    That is a run's start, or the case's adoption: a case adopted in ``needs-partner``
    waits for its partner to write after the adoption (see :mod:`liaise.intake`).
    """
    handovers = [
        entry
        for entry in case.entries
        if entry.kind == "run"
        and (entry.detail.get("event") == RUN_STARTED or entry.detail.get("adopted"))
    ]
    return max(handovers, key=lambda entry: entry.at, default=None)


def _issue_seen_closed(case: Case) -> Optional[bool]:
    """True when the case's issue was last seen closed, False when since reopened, else None.

    Read off the case's ``run`` entries that record the issue's state, the latest last.
    """
    marks = [entry for entry in case.entries if entry.kind == "run"]
    states = [entry.detail["closed"] for entry in marks if "closed" in entry.detail]
    return states[-1] if states else None


def _kept_message_body(case_id: str, outbound: Outbound, *, reason: str) -> str:
    """What the operator is told of a message kept from the partner: never its text.

    The text may hold what the gate diverted it for, such as a token or a local path, and
    a notification goes out through a service the operator does not control.
    """
    return (
        f"case: {case_id}\nto: {outbound.recipient} at {outbound.ref}\nwhy: {reason}\n"
        f"The message is kept on the case as a draft; {SEE_STATUS}."
    )


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
    triage: Optional[Triage] = None,
    lost_run_deadline: timedelta = LOST_RUN_DEADLINE,
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
      on ``global_config.notify.ntfy_topic_env``;
    - ``triage``: a :data:`Triage` that groups and orders each subject's ready cases
      before they start; None keeps the tick's own order, oldest first.

    ``only`` is a slug or slugs to run alone; an unknown one raises
    :class:`~liaise.config.ConfigError`. ``now`` is the tick's clock (the current UTC
    time when None). ``lost_run_deadline`` is how long past its wall clock a run that will
    not stop is waited on (:data:`LOST_RUN_DEADLINE`). Unless ``dry_run``, the tick holds
    the run lock in ``state_dir`` (raising :class:`RunLockHeld` while another tick holds
    it) and stamps its start and end in ``store``.
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
        triage=triage,
        lost_run_deadline=lost_run_deadline,
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

    A plain pid file, created with ``O_CREAT | O_EXCL``, so finding the lock free and
    taking it are one step: two ticks starting together cannot both take it. A lock whose
    process is gone is reclaimed, removed and then created exclusively again, so of two
    ticks reclaiming it at once only one gets it. On exit the lock is removed only while
    it still holds this process's pid. It is advisory, which is enough to keep a manual
    ``liaise run`` from colliding with the scheduled one on the same machine. Raises
    :class:`RunLockHeld` while a live process holds it.
    """

    def create() -> bool:
        """Create the lock holding this process's pid where no file is; False when one is."""
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as lock:
            lock.write(str(os.getpid()))
        return True

    def release() -> None:
        """Remove the lock, unless it holds another pid: a process that took it over since."""
        try:
            holder = int(lock_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return
        if holder == os.getpid():
            lock_path.unlink(missing_ok=True)

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if not create():
        other = _lock_owner(lock_path)
        if other is not None:
            raise RunLockHeld(
                f"another liaise run (pid {other}) is already in progress "
                f"(lock: {lock_path})"
            )
        lock_path.unlink(missing_ok=True)  # left by a process that is gone
        if not create():
            raise RunLockHeld(
                f"another liaise run took over the stale run lock first "
                f"(lock: {lock_path})"
            )
    try:
        yield
    finally:
        release()


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
        triage: Optional[Triage],
        lost_run_deadline: timedelta,
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
        self.triage = triage
        self.lost_run_deadline = lost_run_deadline
        #: Per case, whether its GitHub issue was read closed this tick (None: unreadable).
        self.issue_closed: dict[str, Optional[bool]] = {}
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
        self._watch_for_lost_runs()

    def _watch_for_lost_runs(self) -> None:
        """Hand the owner each case left ``working`` with no run in flight: its run was lost.

        A tick that failed after it had collected a run leaves such a case, and so does an
        issue adopted with a 0.0.x ``working`` label. Nothing else would ever move one on,
        so it goes to ``needs-owner``, and the operator is told once.
        """
        in_flight = {run.case_id for run in self.ledger.runs(status=RUNNING)}
        for slug in self.slugs:
            working = self.ledger.cases(subject=slug, state=WORKING)
            for case in sorted(working, key=lambda case: case.id):
                if case.id in in_flight:
                    continue
                try:
                    self._run_lost(case)
                except Exception as error:  # one case's failure is not the tick's
                    self.problem(
                        f"handing {case.id}, whose run was lost, to the owner failed: "
                        f"{_error_text(error)}"
                    )

    def _run_lost(self, case: Case) -> None:
        since = _entered_state_at(case, WORKING)
        told = any(
            entry.kind == "run"
            and entry.detail.get("event") == RUN_LOST
            and entry.at >= since
            for entry in case.entries
        )
        self._transition(case.id, NEEDS_OWNER, "run lost: no run of it is in flight")
        if not told:
            self._notify(
                f"liaise: {case.id} run lost",
                f"{case.id} was working, but no run of it is in flight, so it waits for "
                f"you in needs-owner. Nothing was sent to the partner about it; "
                f"{SEE_STATUS}, and move it on with liaise case set-state.",
            )
        self._entry(case.id, "run", detail={"event": RUN_LOST})

    def _give_up(self, subject: Subject, case: Case, run: RunRecord) -> None:
        """Finish a run still not stopped by its lost-run deadline, as ``timed_out``.

        Whether or not its pid is alive: by now that pid may be another process's, so it is
        sent nothing more. Its case goes to the owner, who is told.
        """
        age = _fmt_age((self.now - run.started_at).total_seconds())
        self.say(
            f"  run {run.run_id} ({case.id}): still not stopped {age} after it started, "
            f"past its wall clock and the lost-run deadline: given up as {_TIMED_OUT}"
        )
        finished = replace(run, status=FINISHED, ended_at=self.now)
        result = RunResult(
            run_id=run.run_id, error=_TIMED_OUT, session_id=run.session_id
        )
        self._collected(subject, case, finished, result)

    def _reconcile_run(self, run: RunRecord) -> None:
        subject = self.subjects[run.subject]
        case = self._case(run.case_id)
        budget = subject.policy.budget
        label = f"  run {run.run_id} ({case.id})"
        wall_clock = timedelta(minutes=budget.timeout_minutes)
        if self.now - run.started_at > wall_clock + self.lost_run_deadline:
            self._give_up(subject, case, run)
            return
        timed_out = self.now - run.started_at > wall_clock
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

    def _release(self, subject: Subject, run_id: str) -> Optional[str]:
        """Release ``run_id``'s checkout lock; why that failed, or None.

        A lock that cannot be released must not strand the run's case in ``working``:
        :meth:`_collected` hands the case to the owner instead.
        """
        self.released.add(run_id)
        if self.dry_run:
            return None
        try:
            checkout = self._workspace(subject)
            if checkout is not None:
                checkout.release(run_id=run_id)
        except Exception as error:  # a lock file that cannot be read or removed
            return _error_text(error)
        return None

    def _collected(
        self, subject: Subject, case: Case, run: RunRecord, result: RunResult
    ) -> None:
        self.ledger.save_run(run)
        self.collected.append(run.run_id)
        release_failure = self._release(subject, run.run_id)
        if release_failure is not None:
            self.problem(
                f"run {run.run_id}: releasing its checkout failed: {release_failure}; "
                f"counted as {_CRASHED}, and its outcomes are not carried out"
            )
            result = replace(result, error=_CRASHED, outcomes=())
        if result.session_id and result.session_id != case.session_id:
            self._save(replace(self._case(case.id), session_id=result.session_id))
        error = result.error or (None if result.outcomes else _NEEDS_HUMAN)
        cancelled = (
            release_failure is None
            and error == _CRASHED
            and self._cancelled_for_hold(case, run.run_id)
        )
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
        deploys: list[_DeliveryGroup] = []
        for item in _group_deliveries(actions):
            if isinstance(item, _DeliveryGroup):
                hold = self._effect_hold(subject, case, effect=item.deliver.kind)
                if hold is not None:
                    held.append(hold.scope)
                    self.say(f"  deliver ({item.deliver.kind}): held by {hold.scope}")
                    for send in item.sends:
                        self._hold_send(send, hold)
                elif item.deliver.kind == PR_ONLY_DELIVERY:
                    for send in item.sends:
                        self._send(subject, self._try_it(item.deliver, send))
                    if item.transition is not None:
                        final = (item.transition.state, item.transition.reason)
                else:  # a deploy: nothing says it is live before its command succeeds
                    deploys.append(item)
                    final = item
                    self.say(
                        f"  deliver: waits for {subject.slug}'s batch deploy, after "
                        f"every run is collected"
                        if _is_batch(item.deliver)
                        else f"  deliver: deploys {case_id} once its outcomes are carried out"
                    )
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
        deliveries = [
            _PendingDelivery(case_id, group, applies_state=final is group and not held)
            for group in deploys
        ]
        for delivery in deliveries:
            if _is_batch(delivery.group.deliver):
                self.pending.setdefault(subject.slug, []).append(delivery)
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
            pass  # its deploy decides the state
        elif final is not None:
            self._transition(case_id, *final)
        else:
            state = NEEDS_PARTNER if planned_send else NEEDS_OWNER
            self._transition(case_id, state, f"run {run_id} ended")
        immediate = [d for d in deliveries if not _is_batch(d.group.deliver)]
        if immediate:
            self._deliver(subject, immediate)  # a deploy per issue: a batch of one, now

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
                _kept_message_body(case.id, send, reason=decision.diverted),
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
                _kept_message_body(case.id, outbound, reason=reason),
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
            self._deliver(self.subjects[slug], self.pending[slug])

    def _deliver(self, subject: Subject, pending: Sequence[_PendingDelivery]) -> None:
        """Deploy ``pending`` (a subject's batch, or one case's deploy per issue) and act on it.

        An exception on the way is a problem line, and each of those cases still
        ``working`` goes to ``needs-owner``, the operator told once: it never ends the
        tick for the other cases and subjects.
        """
        try:
            self._run_delivery(subject, pending)
        except Exception as error:  # one delivery's failure is not the tick's
            why = _error_text(error)
            case_ids = ", ".join(item.case_id for item in pending)
            self.problem(f"delivering {case_ids} for {subject.slug} failed: {why}")
            for item in pending:
                try:
                    if self._case(item.case_id).state == WORKING:
                        reason = f"delivery failed: {why}"
                        self._transition(item.case_id, NEEDS_OWNER, reason)
                except Exception as stranded:
                    self.problem(
                        f"handing {item.case_id} to the owner failed: "
                        f"{_error_text(stranded)}"
                    )
            self._notify(
                f"liaise: delivering {case_ids} failed",
                f"{subject.slug}: delivering {case_ids} raised {type(error).__name__}, "
                f"so it waits for you in needs-owner and nothing more was sent to the "
                f"partner; {SEE_STATUS}.",
            )

    def _run_delivery(
        self, subject: Subject, pending: Sequence[_PendingDelivery]
    ) -> None:
        """Run the subject's deploy command once for ``pending``, then say "try it" or fail them.

        Only a command that ran and succeeded sends the "try it" messages and moves the
        cases to ``deployed``; anything else, an empty command included, moves them to
        ``needs-owner`` and tells the operator.
        """
        slug = subject.slug
        case_ids = ", ".join(item.case_id for item in pending)
        ok, reason, output = self._deploy(subject)
        if ok:
            verb = "would run" if self.dry_run else "ran"
            self.say(f"deploy {slug}: {verb} {subject.delivery.command} for {case_ids}")
            for item in pending:
                for send in item.group.sends:
                    self._send(subject, self._try_it(item.group.deliver, send))
                transition = item.group.transition
                if item.applies_state and transition is not None:
                    self._transition(item.case_id, transition.state, transition.reason)
            return
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
            f"liaise: {case_ids} landed but did not deploy",
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
            ready: list[tuple[Case, str]] = []
            for case in eligible:
                try:
                    why_ready = self._readiness(subject, case)
                except Exception as error:  # one case's failure is not the tick's
                    self.problem(f"starting {case.id} failed: {_error_text(error)}")
                    continue
                if why_ready is not None:
                    ready.append((case, why_ready))
            for case, why_ready in self._triaged(slug, ready):
                try:
                    self._consider(subject, self._case(case.id), why_ready)
                except Exception as error:  # one case's failure is not the tick's
                    self.problem(f"starting {case.id} failed: {_error_text(error)}")

    def _triaged(
        self, slug: str, ready: list[tuple[Case, str]]
    ) -> list[tuple[Case, str]]:
        """``ready`` in the order the triage seam gives it; as it is without one.

        The triage gets the ready cases in the tick's order and returns them in groups. A
        case it leaves out is not started this tick; one it names that is not ready, or
        names twice, is started once at most. A triage that raises is a problem line, and
        the cases start in the tick's own order.
        """
        if self.triage is None or not ready:
            return ready
        by_id = {case.id: (case, why) for case, why in ready}
        try:
            groups = self.triage(tuple(case for case, _ in ready))
            named = dict.fromkeys(case.id for group in groups for case in group)
        except Exception as error:  # a triage that fails must not stop every start
            self.problem(
                f"triage of {slug} failed: {_error_text(error)}; the cases start in "
                f"the tick's own order"
            )
            return ready
        kept = [by_id[case_id] for case_id in named if case_id in by_id]
        left_out = [case_id for case_id in by_id if case_id not in named]
        order = ", ".join(case.id for case, _ in kept) or "none"
        self.say(
            f"  triage {slug}: {order}"
            + (f"; left out this tick: {', '.join(left_out)}" if left_out else "")
        )
        return kept

    def _readiness(self, subject: Subject, case: Case) -> Optional[str]:
        """Why ``case`` is ready to start, or None, after a plan line saying why it is not."""
        label = f"  case {case.id} ({case.state})"
        policy = subject.policy
        in_flight = [
            r for r in self.ledger.runs(status=RUNNING) if r.case_id == case.id
        ]
        if in_flight:
            self.say(f"{label}: run {in_flight[0].run_id} is still in flight")
            return None
        if case.defer_until is not None and case.defer_until > self.now:
            self.say(f"{label}: deferred until {case.defer_until.isoformat()}")
            return None
        partners = {case.reporter}
        handover = _last_handover(case)
        if (
            case.state == NEEDS_PARTNER
            and handover is not None
            and last_partner_activity(case, partner_persons=partners) <= handover.at
        ):
            since = (
                "it was adopted" if handover.detail.get("adopted") else "the last run"
            )
            self.say(f"{label}: awaiting the partner's reply since {since}")
            return None
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
            return None
        if not readiness.ready:
            countdown = _fmt_age(readiness.countdown.total_seconds())
            self.say(f"{label}: not ready ({readiness.reason}, {countdown} to go)")
            return None
        return readiness.reason

    def _consider(self, subject: Subject, case: Case, why_ready: str) -> None:
        """Start ``case``, ready for ``why_ready``, if it passes every other check."""
        label = f"  case {case.id} ({case.state})"
        budget = subject.policy.budget
        passed = [f"ready ({why_ready})"]

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
            if self._issue_is_closed(case, label):
                return
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
                if self._issue_is_closed(case, label):
                    return
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
        if self._issue_is_closed(case, label):
            return
        if _github_issue_ref(case) is not None:
            passed.append("issue open")

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

        if (
            self.dry_run
        ):  # preflight runs `claude auth status`: a process, so never here
            if auto_processor:
                self.say(
                    f"{label}: held by {PROCESSOR_SCOPE} (set automatically); preflight "
                    f"skipped (dry run), so the hold is not probed"
                )
                return
            passed.append("preflight skipped (dry run)")
        elif self._preflight_passes(
            subject, case, job, label=label, auto_processor=auto_processor
        ):
            passed.append("preflight ok")
        else:
            return

        conflict = checkout.conflict() or self._lock_conflict(checkout, run_id)
        if conflict is not None:
            self.say(f"{label}: {_WORKSPACE_CONFLICT}: {conflict}")
            self._defer(case.id, defer_for_error(_WORKSPACE_CONFLICT, now=self.now))
            return
        passed.append("workspace free")
        self.say(f"{label}: {', '.join(passed)}")
        self._dispatch(subject, case, job, checkout, mode=mode)

    def _preflight_passes(
        self,
        subject: Subject,
        case: Case,
        job: Job,
        *,
        label: str,
        auto_processor: bool,
    ) -> bool:
        """Whether ``job`` passes preflight, acting on it when it does not. Never in a dry run.

        A preflight that raises is a start that failed. An automatic ``processor`` hold
        being probed stays while preflight fails, and is lifted once it passes, after which
        the other holds are asked again.
        """
        health, failure = self._call("preflight", job)
        if failure is not None:
            self._start_failed(subject, case, job.run_id, failure)
            return False
        if not health.ok:
            if auto_processor:
                self.say(
                    f"{label}: held by {PROCESSOR_SCOPE} (set automatically), and "
                    f"preflight still fails ({health.error})"
                )
            else:
                self._preflight_failed(subject, case, health)
            return False
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
                return False
        return True

    def _issue_is_closed(self, case: Case, label: str) -> bool:
        """Whether ``case`` waits because its GitHub issue is closed, or cannot be read now.

        The issue is read at most once a tick, and only for a case about to start or to be
        told something (see :meth:`_read_issue_closed`). A case with no GitHub issue never
        waits for this.
        """
        if case.id not in self.issue_closed:
            self.issue_closed[case.id] = self._read_issue_closed(case)
        closed = self.issue_closed[case.id]
        if closed is False:
            return False
        self.say(f"{label}: its issue {'is closed' if closed else 'could not be read'}")
        return True

    def _read_issue_closed(self, case: Case) -> Optional[bool]:
        """Whether ``case``'s GitHub issue is closed: True or False, None when it cannot be read.

        Read with ``correspond.read``, from the opening's ``native["state"]``. The case
        records a closing once, as a ``run`` entry ``{"closed": True}``, and a reopening as
        one with ``{"closed": False}``. A read that fails is a problem line.
        """
        ref = _github_issue_ref(case)
        if ref is None:
            return False
        try:
            messages = correspond.read(ref, registry=self.registry)
        except Exception as error:  # a channel that fails: the next tick reads it again
            self.problem(
                f"{case.id}: reading {ref} failed: {_error_text(error)}; the case waits "
                f"for the next tick"
            )
            return None
        opening = next(
            (
                message
                for message in messages
                if message.id.startswith(OPENING_ID_PREFIX)
            ),
            None,
        )
        closed = opening is not None and opening.native.get("state") == CLOSED_STATE
        seen = _issue_seen_closed(self._case(case.id))
        if closed and seen is not True:
            detail = {"event": RUN_ISSUE_CLOSED, "closed": True}
            self._entry(case.id, "run", detail=detail)
            self.say(
                f"  case {case.id}: {ref} is closed, so the case is neither started nor "
                f"nudged until it reopens"
            )
        elif not closed and seen is True:
            detail = {"event": RUN_ISSUE_REOPENED, "closed": False}
            self._entry(case.id, "run", detail=detail)
            self.say(f"  case {case.id}: {ref} was reopened")
        return closed

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
        """``<case id>-r<n>-<suffix>``: the case's n-th start, and a fresh uuid4's first hex digits.

        The suffix keeps a run id from ever naming an earlier run's directory, even where the
        ledger's count of the case's starts was lost (see :data:`RUN_ID_SUFFIX_DIGITS`).
        """

        def run_id(number: int) -> str:
            return f"{case.id}-r{number}-{uuid4().hex[:RUN_ID_SUFFIX_DIGITS]}"

        number = len(_run_starts(case)) + 1
        candidate = run_id(number)
        while self.ledger.get_run(candidate) is not None:  # a start the entries lost
            number += 1
            candidate = run_id(number)
        return candidate

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
        if _issue_seen_closed(case) or self._issue_is_closed(
            case, f"  nudge {case.id}"
        ):
            return  # a closed case is never nudged
        self.say(f"nudge {case.id}: deployed, and quiet since {since.isoformat()}")
        self._send_own(subject, case, NUDGE_MESSAGE, purpose=NUDGE_PURPOSE)
        self._entry(case.id, "gate", detail={"purpose": NUDGE_PURPOSE, "nudged": True})

    # ---- 5. labels ----

    def project(self) -> None:
        """Show each case's state on its GitHub issues, as their one state label.

        The targets are the cases this tick changed, and every other case of the subjects
        ticked whose state is not the one last projected, such as a case the operator moved
        with ``liaise case set-state`` between ticks. A projection is recorded on the case
        as a ``projection`` entry holding the state, its error too when it failed, so a
        failed one is not retried until the case changes.
        """

        def projected_state(case: Case) -> Optional[str]:
            projections = [e for e in case.entries if e.kind == "projection"]
            return projections[-1].detail.get("state") if projections else None

        touched = [self.ledger.get_case(case_id) for case_id in list(self.touched)]
        drifted = [
            case
            for slug in self.slugs
            for case in sorted(self.ledger.cases(subject=slug), key=lambda c: c.id)
            if case.id not in self.touched and projected_state(case) != case.state
        ]
        targets = [
            case
            for case in (*touched, *drifted)
            if case is not None
            and case.subject in self.subjects
            and _github_issue_ref(case) is not None
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
            if projected_state(case) != case.state:
                projected = LedgerEntry(
                    at=self.now,
                    kind="projection",
                    actor=TICK_ACTOR,
                    detail={"state": case.state},
                )
                self.ledger.append(case.id, projected)
