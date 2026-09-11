"""Dispatch (A.5): running the coding agent, budgets, and reconciliation.

`Dispatcher` is the seam: :class:`ClaudeHeadless` runs the real `claude` CLI
headless; :class:`EchoDispatcher` just records jobs, for tests. Local state
(session ids to resume, daily dispatch counters) lives in whatever
`MutableMapping` is passed in — a `dict` in tests, a `dol` store for real
(A.1 rule 3: `liaise` never keeps this state on GitHub).
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, MutableMapping, Optional, Protocol

from liaise.config import DFLT_PERMISSION_MODE, Budget, PartnerConfig
from liaise.github import GitHub, Issue
from liaise.messages import budget_capped_message, mention
from liaise.notify import notify as _default_notify
from liaise.prompt import compose_prompt
from liaise.state import current_state, set_state


@dataclass(frozen=True)
class Job:
    """Everything a :class:`Dispatcher` needs to run one dispatch."""

    prompt: str
    cwd: str
    budget: Budget
    command: str
    resume_command: str
    session_id: Optional[str] = None
    permission_mode: str = DFLT_PERMISSION_MODE


@dataclass(frozen=True)
class DispatchResult:
    """What a :class:`Dispatcher` returns."""

    returncode: int
    session_id: Optional[str]
    stdout: str = ""
    stderr: str = ""


class Dispatcher(Protocol):
    """What `liaise` needs to run a coding agent."""

    def dispatch(self, job: Job) -> DispatchResult:
        """Run `job` and return its result. Must not raise on a nonzero exit."""
        ...


class ClaudeHeadless:
    """The default :class:`Dispatcher`: runs the configured `claude` command headless.

    The prompt is written to a temp file and referenced by path in the command
    template — never passed on the command line. The fresh and the resume
    template are formatted with the same `job.permission_mode`, so a resume
    runs under the mode of the run it continues. Never inherits an assumed
    environment beyond what `subprocess.run` gives it by default; callers that
    need specific variables (see `schedule.py`) pass them explicitly.
    """

    def dispatch(self, job: Job) -> DispatchResult:
        fd, path = tempfile.mkstemp(prefix="liaise-prompt-", suffix=".md")
        prompt_file = Path(path)
        try:
            with open(fd, "w") as f:
                f.write(job.prompt)

            template = job.resume_command if job.session_id else job.command
            command = template.format(
                prompt_file=str(prompt_file),
                session_id=job.session_id or "",
                permission_mode=job.permission_mode,
            )
            try:
                proc = subprocess.run(
                    shlex.split(command),
                    cwd=job.cwd,
                    capture_output=True,
                    text=True,
                    timeout=job.budget.timeout_minutes * 60,
                )
            except subprocess.TimeoutExpired as e:
                # H-7: uncaught, this propagated out of dispatch_issue and left
                # the daily counter incremented, the issue at liaise:working
                # (excluded from every later pass's eligible states, so never
                # reconsidered), no notification, and killed the rest of
                # run_once's pass for every partner sorted after this one — a
                # hung dispatch must reconcile exactly like a crash, not
                # escape reconciliation by raising instead of returning.
                return DispatchResult(
                    returncode=124,  # the shell convention for "command timed out"
                    session_id=job.session_id,
                    stdout=_decode(e.stdout),
                    stderr=_decode(e.stderr) + "\n[liaise: dispatch timed out]",
                )
            session_id = _extract_session_id(proc.stdout) or job.session_id
            return DispatchResult(
                returncode=proc.returncode,
                session_id=session_id,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        finally:
            prompt_file.unlink(missing_ok=True)


def _decode(output) -> str:
    """`TimeoutExpired.stdout`/`.stderr` may be `None`, `str`, or `bytes` —
    `text=True` on the run that timed out still leaves the partially-captured
    buffer's type up to the platform.
    """
    if output is None:
        return ""
    return output if isinstance(output, str) else output.decode(errors="replace")


#: A session id is `.format()`-ed into `resume_command` before `shlex.split`
#: (L-1) — not a shell, but an unvalidated value containing whitespace would
#: become extra argv elements on the next `claude` invocation. Real session
#: ids are UUID-shaped; this is deliberately generous beyond that so a format
#: change upstream doesn't silently break resume, while still rejecting
#: anything that could split into more than one argv token.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _extract_session_id(stdout: str) -> Optional[str]:
    try:
        raw = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    session_id = raw.get("session_id") if isinstance(raw, dict) else None
    if session_id is not None and not _SESSION_ID_RE.match(session_id):
        return None
    return session_id


class EchoDispatcher:
    """Records every job it's given instead of running anything. For tests."""

    def __init__(self, *, returncode: int = 0):
        self.jobs: list[Job] = []
        self._returncode = returncode
        self._next_id = 0

    def dispatch(self, job: Job) -> DispatchResult:
        self.jobs.append(job)
        self._next_id += 1
        session_id = job.session_id or f"echo-session-{self._next_id}"
        return DispatchResult(returncode=self._returncode, session_id=session_id)


# ---- budgets: daily dispatch cap, session-id memory (dol-backed store, injectable) ----


def default_store(state_dir: str) -> MutableMapping:
    """The `dol`-backed store `liaise` uses by default: one JSON file per key,
    under `state_dir`. Session ids, daily counters and the last-run stamp all
    live here — never on GitHub (A.1 rule 3). Tests use a plain `dict` instead.
    """
    import dol

    return dol.Jsons(str(Path(state_dir).expanduser()))


def _today(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).date().isoformat()


def _daily_key(partner: PartnerConfig, day: str) -> str:
    # No "/" — the default store (dol.Jsons) treats it as a subdirectory
    # separator and won't create missing parent directories on write.
    return f"daily__{partner.slug}__{day}"


def _session_key(issue: Issue) -> str:
    safe_repo = issue.repo.replace("/", "-")
    return f"sessions__{safe_repo}__{issue.number}"


def daily_dispatch_count(
    store: MutableMapping, partner: PartnerConfig, *, now: Optional[datetime] = None
) -> int:
    """How many times `partner` has been dispatched to today."""
    return store.get(_daily_key(partner, _today(now)), 0)


def stored_session_id(store: MutableMapping, issue: Issue) -> Optional[str]:
    """The session id stored for `issue`, if a previous dispatch left one."""
    return store.get(_session_key(issue))


def _dispatched_at_key(issue: Issue) -> str:
    safe_repo = issue.repo.replace("/", "-")
    return f"dispatched_at__{safe_repo}__{issue.number}"


def last_dispatch_at(store: MutableMapping, issue: Issue) -> Optional[datetime]:
    """When `issue` was last actually dispatched, or None if never.

    H-3: `run.py` uses this to refuse to re-dispatch a `liaise:needs-partner`
    issue unless the partner has said something new *since* this — otherwise
    readiness (satisfied by the partner's original message, which is what
    triggered the first dispatch) lets the same unanswered question burn the
    whole daily budget in minutes.
    """
    stamp = store.get(_dispatched_at_key(issue))
    return datetime.fromisoformat(stamp) if stamp else None


@dataclass(frozen=True)
class DispatchOutcome:
    """What :func:`dispatch_issue` did."""

    dispatched: bool
    budget_capped: bool
    crashed: bool
    result: Optional[DispatchResult] = None
    log_path: Optional[str] = None
    #: True when the dispatch succeeded (exit 0) and left the issue at
    #: `liaise:working` because `expect_working_on_success` was set — the
    #: `deploy_per == "batch"` case, where the agent lands without deploying
    #: and `run.py` deploys once after the batch. Not a crash.
    landed_awaiting_batch_deploy: bool = False


def dispatch_issue(
    gh: GitHub,
    dispatcher: Dispatcher,
    store: MutableMapping,
    partner: PartnerConfig,
    issue: Issue,
    *,
    notify_fn: Callable[..., bool] = _default_notify,
    log_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    expect_working_on_success: bool = False,
) -> DispatchOutcome:
    """Dispatch `issue` to `partner`'s coding agent, honoring the daily cap.

    Sets `liaise:working` before dispatching. If the dispatcher exits nonzero
    and the issue is *still* `liaise:working`, that is a crash: reconciles to
    `liaise:needs-owner` and notifies the owner with the exit code and the log
    path (A.5 "Reconciliation"). A crashed run is not evidence of anything and
    must not look like progress.

    If the dispatcher exits **zero** and the issue is still `liaise:working`,
    that is only excused when `expect_working_on_success` is set — the
    `deploy_per == "batch"` path, where the agent lands the change but does
    not deploy or set an exit label; `run.py` sets `landed_awaiting_batch_deploy`
    and handles the batch deploy itself. Without that flag (the
    `deploy_per == "issue"` path, where the agent owns deploying, posting and
    setting its own exit label) a still-`working` success is *also* reconciled
    to `liaise:needs-owner` — the agent silently didn't do its job.

    When the dispatch ends at `liaise:needs-partner` or `liaise:deployed`,
    also reconciles the mention rule (#20): the dispatched agent is told in
    its own prompt to start every partner-facing comment with `@notify_login`,
    but a rule the agent forgets must still hold — the newest comment left by
    this dispatch's own GitHub identity is repaired to carry it if missing,
    and the repair is recorded in the dispatch log.
    """
    if daily_dispatch_count(store, partner, now=now) >= partner.budget.daily_dispatches:
        # M-8: A.1 rule 5 — "a cap that trips is a visible label AND A SHORT
        # COMMENT, never silence." The label alone was already there; nothing
        # posted anything. Comment once per issue (skip if it was already
        # `budget` — re-checked every pass since H-4, so this must not repost
        # every tick); notify the owner once per partner per day, not once
        # per capped issue, so N issues capped in one pass isn't N pushes.
        was_already_capped = current_state(issue, partner) == "budget"
        set_state(gh, issue, partner, "budget")
        if not was_already_capped:
            if partner.reply_mode != "draft":
                gh.post_comment(
                    issue.repo,
                    issue.number,
                    budget_capped_message(partner),
                )
            notified_key = f"budget_notified__{partner.slug}__{_today(now)}"
            if not store.get(notified_key):
                notify_fn(
                    "liaise: daily dispatch cap reached",
                    f"{partner.slug} ({partner.repo}) hit its daily cap of "
                    f"{partner.budget.daily_dispatches} dispatches. Resumes tomorrow.",
                )
                store[notified_key] = True
        return DispatchOutcome(dispatched=False, budget_capped=True, crashed=False)

    session_id = stored_session_id(store, issue)
    mode = "resume" if session_id else "fresh"
    # Named in the prompt, so decided before it is composed: the operating
    # rules send drafts and escalations "to the dispatch log" (#22).
    log_path = _log_path(log_dir, issue) if log_dir is not None else None
    prompt = compose_prompt(partner, issue, mode, log_path=log_path)
    job = Job(
        prompt=prompt,
        cwd=partner.dispatch.cwd,
        budget=partner.budget,
        command=partner.dispatch.command,
        resume_command=partner.dispatch.resume_command,
        session_id=session_id,
        permission_mode=partner.dispatch.permission_mode,
    )

    set_state(gh, issue, partner, "working")
    store[_daily_key(partner, _today(now))] = (
        daily_dispatch_count(store, partner, now=now) + 1
    )
    store[_dispatched_at_key(issue)] = (now or datetime.now(timezone.utc)).isoformat()

    # Created only now, for the agent to append to, so a dispatch that fails
    # before it runs leaves no log behind.
    _append_to_log(log_path, f"liaise dispatch log: {issue.url} ({mode})\n")
    result = dispatcher.dispatch(job)
    if result.session_id:
        store[_session_key(issue)] = result.session_id

    _append_to_log(log_path, _result_record(result))

    refreshed = gh.get_issue(issue.repo, issue.number)
    still_working = current_state(refreshed, partner) == "working"
    excused = still_working and result.returncode == 0 and expect_working_on_success

    if still_working and not excused:
        set_state(gh, refreshed, partner, "needs-owner")
        notify_fn(
            "liaise: dispatch crashed",
            f"{issue.url} exited {result.returncode} without changing state. "
            f"Log: {log_path or '(no log_dir configured)'}",
            priority="high",
        )
        return DispatchOutcome(
            dispatched=True,
            budget_capped=False,
            crashed=True,
            result=result,
            log_path=log_path,
        )

    final_state = current_state(refreshed, partner)
    posted_a_new_comment = len(refreshed.comments) > len(issue.comments)
    if (
        final_state in ("needs-partner", "deployed")
        and partner.notify_login
        and posted_a_new_comment
    ):
        # A reconciliation failure (a transient `gh` error, a rate limit) must
        # not discard an otherwise-successful `DispatchOutcome` — that would
        # silently drop `landed_awaiting_batch_deploy` for a landed change.
        try:
            _reconcile_mention(gh, partner, refreshed, log_path=log_path)
        except Exception as e:  # noqa: BLE001 - see comment above
            _append_to_log(
                log_path, f"\n[liaise: mention reconciliation failed: {e}]\n"
            )

    return DispatchOutcome(
        dispatched=True,
        budget_capped=False,
        crashed=False,
        result=result,
        log_path=log_path,
        landed_awaiting_batch_deploy=excused,
    )


def _reconcile_mention(
    gh: GitHub, partner: PartnerConfig, issue: Issue, *, log_path: Optional[str]
) -> None:
    """#20: repair a partner-facing comment the agent forgot to `@mention`.

    Only called when this dispatch actually posted a new comment (the
    caller checks the before/after comment count) — otherwise "the last
    comment by this identity" could be a stale comment from an unrelated,
    much earlier dispatch (e.g. an old owner-facing escalation note), which
    must never be mutated or mislogged as a fresh repair. No-op when that
    new comment already carries the mention, or when it wasn't posted by
    this dispatch's own GitHub identity (`ensure_last_comment_mentions`
    itself only ever touches its own identity's comments).
    """
    repaired = gh.ensure_last_comment_mentions(
        issue.repo, issue.number, mention(partner)
    )
    if repaired:
        _append_to_log(
            log_path,
            f"\n[liaise: repaired a partner-facing comment missing {mention(partner)}]\n",
        )


def _log_path(log_dir: Path, issue: Issue) -> str:
    """This dispatch's log file, as an absolute path. Nothing is created yet.

    Absolute because the path is named in a prompt read by an agent running
    from `partner.dispatch.cwd`, where a relative path would name another file.
    """
    log_dir = Path(log_dir).expanduser().absolute()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_repo = issue.repo.replace("/", "-")
    return str(log_dir / f"{safe_repo}-{issue.number}-{stamp}.log")


def _append_to_log(log_path: Optional[str], text: str) -> None:
    """Append `text` to the dispatch log, creating it if needed; no-op without one.

    Never raises: a log that can't be written (an unwritable `log_dir`, a full
    disk, text that won't encode) must not skip reconciliation — stranding the
    issue at `liaise:working`, the H-7 failure mode — or discard an outcome.
    """
    if not log_path:
        return
    path = Path(log_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(text)
    except OSError:
        pass


def _result_record(result: DispatchResult) -> str:
    """How the dispatch ended, appended after whatever the agent wrote.

    Leads with the agent's final message, unescaped, when the output carries
    one: that message is where the agent puts drafts it couldn't write to the
    log itself, and inside the raw JSON output they aren't sendable as is.
    """
    message = _final_message(result.stdout)
    final = f"\n--- agent's final message ---\n{message}\n" if message else ""
    return (
        f"{final}\n--- liaise: dispatch ended ---\n"
        f"exit code: {result.returncode}\nsession id: {result.session_id}\n\n"
        f"--- stdout ---\n{result.stdout}\n\n--- stderr ---\n{result.stderr}\n"
    )


def _final_message(stdout: str) -> Optional[str]:
    """The `result` text of `claude --output-format json`'s output, or None."""
    try:
        raw = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    message = raw.get("result") if isinstance(raw, dict) else None
    return message if isinstance(message, str) and message else None
