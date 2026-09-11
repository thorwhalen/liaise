"""The run loop (A.5 batching, A.7): intake, label, dispatch, batch-deploy, reconcile.

`liaise poll` (intake.py + cli.py) only *reports*. This module is where liaise
*acts* — always behind `--dry-run`, which prints the same plan without calling
any mutating `GitHub` method.
"""

from __future__ import annotations

import contextlib
import os
import platform
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, MutableMapping, Optional

from liaise.config import Config, PartnerConfig
from liaise.dispatch import (
    Dispatcher,
    DispatchOutcome,
    dispatch_issue,
    last_dispatch_at,
)
from liaise.github import GitHub, Issue
from liaise.intake import compute_readiness, find_partner_issues, last_partner_activity
from liaise.messages import deployed_message, nudge_message
from liaise.notify import notify as _default_notify
from liaise.state import current_state, set_state

#: States from which an issue is eligible to be (re-)considered this pass.
#: `working`, `needs-owner` and `deployed` are not — mid-flight, or waiting on
#: someone other than the partner's clock. `budget` IS eligible (H-4): the cap
#: is per-day, and `dispatch_issue` re-checks it itself, so a still-capped
#: issue simply returns to `budget` — leaving it out is what stranded a
#: budget-capped issue there forever, "resume tomorrow" or not.
_ELIGIBLE_STATES = ("intake", "needs-partner", "paused", "budget")


@dataclass(frozen=True)
class PlanItem:
    """One line of what `run_once` did (or, in `--dry-run`, would do)."""

    partner_slug: str
    issue_number: int
    issue_title: str
    action: str  # "intake" | "pause" | "skip (not ready)" | "dispatch"


@dataclass(frozen=True)
class RunReport:
    """Everything one `run_once` pass did."""

    stamped_at: datetime
    plan: list[PlanItem] = field(default_factory=list)
    dispatched: list[DispatchOutcome] = field(default_factory=list)
    #: partner slug -> issue numbers deployed (posted "it's live", set deployed)
    deployed: dict[str, list[int]] = field(default_factory=dict)


def run_once(
    gh: GitHub,
    dispatcher: Dispatcher,
    store: MutableMapping,
    config: Config,
    *,
    partner: Optional[str] = None,
    dry_run: bool = False,
    notify_fn: Callable[..., bool] = _default_notify,
    log_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> RunReport:
    """One pass: intake new issues, dispatch ready ones, batch-deploy landed ones,
    nudge stale `deployed` issues.

    `dry_run` prints the same plan without calling any mutating `GitHub`
    method and without stamping the run — it changes nothing, the same
    promise `liaise poll` makes. Otherwise the store gets `run_started_at`
    when the pass begins and `run_ended_at` when it stops, even by raising
    (see :func:`run_stamps`). Not re-entrant with itself (L-3): a real
    scheduled invocation takes a lock in `config.global_.state_dir` so a
    manual `liaise run` alongside the timer can't double-dispatch.

    `log_dir` defaults to `config.global_.log_dir`; each dispatch's log, whose
    path the agent's prompt names, is written there.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    partners = [config.partner(partner)] if partner else list(config.partners.values())
    log_dir = Path(
        log_dir if log_dir is not None else config.global_.log_dir
    ).expanduser()

    plan: list[PlanItem] = []
    dispatched: list[DispatchOutcome] = []
    deployed: dict[str, list[int]] = {}

    lock = (
        contextlib.nullcontext()
        if dry_run
        else _run_lock(Path(config.global_.state_dir).expanduser() / "run.lock")
    )
    stamps = contextlib.nullcontext() if dry_run else _stamp_run(store, now=now)
    with lock, stamps:
        for p in sorted(partners, key=lambda p: p.slug):
            landed_this_pass: list[int] = []

            for issue in find_partner_issues(gh, p):
                # H-7 (extended): one issue's own bug or transient GitHub error
                # must not take the rest of the pass down with it — every other
                # partner, and every other issue for THIS partner, still gets a
                # chance to run.
                try:
                    action, outcome = _handle_issue(
                        gh,
                        dispatcher,
                        store,
                        p,
                        issue,
                        now=now,
                        dry_run=dry_run,
                        notify_fn=notify_fn,
                        log_dir=log_dir,
                    )
                except Exception as e:  # noqa: BLE001 - deliberately broad, see above
                    action = f"error ({e})"
                    outcome = None
                if action is not None:
                    plan.append(PlanItem(p.slug, issue.number, issue.title, action))
                if outcome is not None:
                    dispatched.append(outcome)
                    if outcome.landed_awaiting_batch_deploy:
                        landed_this_pass.append(issue.number)

            if landed_this_pass and not dry_run:
                deployed_numbers = _run_batch_deploy(
                    gh, p, landed_this_pass, notify_fn=notify_fn
                )
                if deployed_numbers:
                    deployed[p.slug] = deployed_numbers

            if not dry_run:
                _nudge_stale_deployed(gh, p, now=now, store=store)

    return RunReport(
        stamped_at=now, plan=plan, dispatched=dispatched, deployed=deployed
    )


def _handle_issue(
    gh: GitHub,
    dispatcher: Dispatcher,
    store: MutableMapping,
    p: PartnerConfig,
    issue: Issue,
    *,
    now: datetime,
    dry_run: bool,
    notify_fn: Callable[..., bool],
    log_dir: Optional[Path],
) -> tuple[Optional[str], Optional[DispatchOutcome]]:
    """One issue's worth of `run_once`'s body. Returns (plan action, dispatch outcome)."""
    state = current_state(issue, p)

    if state is None:
        if not dry_run:
            gh.add_labels(issue.repo, issue.number, [p.label])
            set_state(gh, issue, p, "intake")
        return "intake", None

    if state not in _ELIGIBLE_STATES:
        return None, None  # working / needs-owner / deployed: not this pass

    if state == "needs-partner":
        # H-3: readiness alone is satisfied by the partner's ORIGINAL message
        # — that's what triggered the first dispatch. Without this check, an
        # unanswered question gets re-dispatched every tick until the whole
        # daily budget is gone, in minutes, on one issue.
        dispatched_at = last_dispatch_at(store, issue)
        if (
            dispatched_at is not None
            and last_partner_activity(issue, p) <= dispatched_at
        ):
            return "skip (awaiting partner reply)", None

    readiness = compute_readiness(issue, p, now=now)

    if readiness.paused:
        if state != "paused":
            if not dry_run:
                set_state(gh, issue, p, "paused")
            return "pause", None
        return None, None

    if not readiness.ready:
        return "skip (not ready)", None

    if dry_run:
        return "dispatch", None

    outcome = dispatch_issue(
        gh,
        dispatcher,
        store,
        p,
        issue,
        notify_fn=notify_fn,
        log_dir=log_dir,
        now=now,
        expect_working_on_success=(p.deploy_per == "batch"),
    )
    return "dispatch", outcome


def _run_batch_deploy(
    gh: GitHub,
    partner: PartnerConfig,
    issue_numbers: list[int],
    *,
    notify_fn: Callable[..., bool],
) -> list[int]:
    """Run the partner's deploy command once, then post + set deployed per issue.

    Returns the issue numbers actually told "it's live". M-2: with no deploy
    command configured (the out-of-the-box `deploy_per = "batch"` default),
    there is nothing to run — reconciles every landed issue to `needs-owner`
    with one notification rather than stranding them at `liaise:working`
    forever. M-1: a deploy command that fails (nonzero exit, or simply
    missing) does the same rather than telling the partner it's live when it
    isn't; the command runs from `partner.dispatch.cwd` with a timeout rather
    than the scheduler's default `cwd=/`, under which any relative deploy
    command would fail every single time. H-5: in `draft` reply mode, posts
    nothing to the thread — the owner is notified instead, same as every
    other partner-facing message in that mode.
    """
    if not partner.deploy:
        _reconcile_landed_without_deploy(
            gh,
            partner,
            issue_numbers,
            notify_fn=notify_fn,
            reason="no deploy command is configured for this partner",
        )
        return []

    try:
        result = subprocess.run(
            shlex.split(partner.deploy),
            cwd=partner.dispatch.cwd,
            timeout=partner.budget.timeout_minutes * 60,
            capture_output=True,
            text=True,
        )
        failed = result.returncode != 0
        detail = f"exit {result.returncode}: {(result.stderr or result.stdout)[:300]}"
    except (OSError, subprocess.TimeoutExpired) as e:
        failed = True
        detail = str(e)

    if failed:
        _reconcile_landed_without_deploy(
            gh,
            partner,
            issue_numbers,
            notify_fn=notify_fn,
            reason=f"the deploy command failed ({detail})",
        )
        return []

    told: list[int] = []
    for number in issue_numbers:
        if partner.reply_mode != "draft":
            gh.post_comment(
                partner.repo,
                number,
                deployed_message(partner),
            )
        issue = gh.get_issue(partner.repo, number)
        set_state(gh, issue, partner, "deployed")
        told.append(number)

    if partner.reply_mode == "draft" and told:
        notify_fn(
            "liaise: batch deployed (draft mode — nothing posted)",
            f"{partner.repo} issues {', '.join(f'#{n}' for n in told)} landed and were "
            f"deployed. Nothing was posted to the partner (draft mode) — tell them "
            f'yourself: "{deployed_message(partner)}"',
        )
    return told


def _reconcile_landed_without_deploy(
    gh: GitHub,
    partner: PartnerConfig,
    issue_numbers: list[int],
    *,
    notify_fn: Callable[..., bool],
    reason: str,
) -> None:
    for number in issue_numbers:
        issue = gh.get_issue(partner.repo, number)
        set_state(gh, issue, partner, "needs-owner")
    notify_fn(
        "liaise: batch landed but did not deploy",
        f"{partner.repo} issues {', '.join(f'#{n}' for n in issue_numbers)} landed but "
        f"{reason}. Nothing was posted to the partner.",
        priority="high",
    )


def _nudge_stale_deployed(
    gh: GitHub, partner: PartnerConfig, *, now: datetime, store: MutableMapping
) -> None:
    """A.4: an issue in `deployed` with no partner activity for
    `deployed_nudge_days` gets one plain-language nudge comment, once (M-10 —
    previously unimplemented; the label and the skill both promised it).
    """
    for issue in find_partner_issues(gh, partner):
        if current_state(issue, partner) != "deployed":
            continue
        nudged_key = f"nudged__{issue.repo.replace('/', '-')}__{issue.number}"
        if store.get(nudged_key):
            continue
        activity = last_partner_activity(issue, partner)
        if now - activity < timedelta(days=partner.deployed_nudge_days):
            continue
        if partner.reply_mode != "draft":
            gh.post_comment(
                issue.repo,
                issue.number,
                nudge_message(partner),
            )
        store[nudged_key] = now.isoformat()


@contextlib.contextmanager
def _run_lock(lock_path: Path) -> Iterator[None]:
    """L-3: concurrency of one (A.1 rule 5), enforced rather than assumed.

    A plain PID file: stale locks (the owning process is gone) are reclaimed
    automatically. This is advisory, not atomic across a networked
    filesystem — good enough for "don't let a manual `liaise run` collide
    with the scheduled one on the same machine", which is the actual risk.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        try:
            other_pid = int(lock_path.read_text().strip())
        except ValueError:
            other_pid = None
        if other_pid is not None and _pid_is_alive(other_pid):
            raise RuntimeError(
                f"another liaise run (pid {other_pid}) is already in progress "
                f"(lock: {lock_path})"
            )
    lock_path.write_text(str(os.getpid()))
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def _pid_is_alive(pid: int) -> bool:
    """Whether `pid` names a live process. Never sends it a real signal.

    `os.kill(pid, 0)` is the POSIX idiom for this — signal 0 delivers
    nothing, it only validates the target. **On Windows it is not that
    idiom**: `os.kill` there calls `TerminateProcess`, so `os.kill(pid, 0)`
    actually kills whatever process holds `pid` (with exit code 0) rather
    than just checking it. Caught before this ever ran in CI: the lock's own
    test seeds the lock file with `os.getpid()` — on Windows the old code
    would have terminated the pytest process running the test.
    """
    if platform.system() == "Windows":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not signalable by us — still alive
    return True


# ---- run stamps (#22): start and end, so a long pass reads as running ----

_RUN_STARTED_KEY = "run_started_at"
_RUN_ENDED_KEY = "run_ended_at"
#: 0.0.3 and earlier: one stamp, the pass's start, written once it finished.
_LEGACY_LAST_RUN_KEY = "last_run"


@dataclass(frozen=True)
class RunStamps:
    """When the most recent non-dry-run pass started and ended."""

    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    @property
    def running(self) -> bool:
        """Whether a pass is in progress: it started later than the last one ended."""
        if self.started_at is None:
            return False
        return self.ended_at is None or self.started_at > self.ended_at


def run_stamps(store: MutableMapping) -> RunStamps:
    """The run stamps `run_once` keeps in `store`.

    A store last written by 0.0.3 or earlier holds only `last_run` — the start
    of a pass that had already finished — read here as a run that started and
    ended then.
    """
    started, ended = store.get(_RUN_STARTED_KEY), store.get(_RUN_ENDED_KEY)
    if started is None and ended is None:
        started = ended = store.get(_LEGACY_LAST_RUN_KEY)
    return RunStamps(
        started_at=datetime.fromisoformat(started) if started else None,
        ended_at=datetime.fromisoformat(ended) if ended else None,
    )


@contextlib.contextmanager
def _stamp_run(store: MutableMapping, *, now: datetime) -> Iterator[None]:
    """Stamp the pass's start on entry and its end on exit — an exit by
    exception included, since a pass that died is not still running.

    The end is `now` plus the monotonic time elapsed, so both stamps share the
    pass's own clock (an injected `now` included) and the end never precedes
    the start.
    """
    store[_RUN_STARTED_KEY] = now.isoformat()
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = timedelta(seconds=time.monotonic() - started)
        store[_RUN_ENDED_KEY] = (now + elapsed).isoformat()


def last_run_age(
    store: MutableMapping, *, now: Optional[datetime] = None
) -> Optional[float]:
    """Seconds since the most recent non-dry-run `run_once` started, or None if it never ran."""
    started_at = run_stamps(store).started_at
    if started_at is None:
        return None
    now = now if now is not None else datetime.now(timezone.utc)
    return (now - started_at).total_seconds()
