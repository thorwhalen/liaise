"""Processors (design §3.6): what runs a case's work, detached, and how that run ended.

A :class:`Processor` has six verbs, each with its default in :class:`ClaudeHeadless`:

- ``preflight(job)``: whether work could start now: the command, the checkout, and whether
  the login still works. It never starts any.
- ``start(job)`` and ``resume(session_id, job)``: spawn a detached run and return its
  :class:`~liaise.model.RunRecord` at once. Both are idempotent on ``job.run_id``.
- ``status(run)``: never blocks. It refreshes the heartbeat, the status and the end time.
- ``cancel(run, mode=...)``: interrupt, then terminate. The session stays resumable. Only a
  process verified as the run's, by when it started, is ever signalled.
- ``collect(run)``: ``None`` while the run is going, then its
  :class:`~liaise.model.RunResult`, classified by :func:`liaise.errors.classify`.

In a dry run the tick passes ``persist=False`` to ``status`` and ``collect``, which then
write nothing.

:class:`ClaudeHeadless` keeps each run's files in ``<runs_dir>/<run_id>/``::

    prompt.md       the prompt; the command line only points at it
    stream.jsonl    claude's stream-JSON stdout, whose mtime is the heartbeat
    stderr.log      claude's stderr
    record.json     the RunRecord, plus the cancel bookkeeping

The files hold everything, so a run one tick started is checked, cancelled and collected
by the next. :class:`EchoProcessor` runs nothing and returns scripted results, for tests.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Union,
    runtime_checkable,
)

from liaise.errors import StreamSummary, classify, parse_stream
from liaise.model import (
    OUTCOME_KINDS,
    Health,
    Outcome,
    RunRecord,
    RunResult,
    require_one_of,
)
from liaise.prompt import MODES

#: A run is a new session (``fresh``) or continues a stored one (``resume``).
FRESH, RESUME = MODES
#: A run's ``status`` in its RunRecord.
RUN_STATUSES = ("running", "finished")
RUNNING, FINISHED = RUN_STATUSES
#: ``graceful`` interrupts first and terminates after the grace period; ``now`` terminates.
CANCEL_MODES = ("graceful", "now")

DFLT_CLAUDE_BIN = "claude"
#: Seconds between a graceful cancel's interrupt and the terminate that may follow it.
DFLT_GRACE_S = 15.0
#: Seconds a run may go on after a cancel sent it SIGTERM before a later cancel kills it
#: with SIGKILL (POSIX only: on Windows every cancel already ends the run outright).
KILL_GRACE_S = 60.0
#: Variables a run must not inherit: with either set, claude bills that key instead of
#: the subscription the operator logged in with.
SCRUBBED_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
#: Set in every run's environment: claude retries transient API errors, a bounded number
#: of times, before it gives up and reports them.
CHILD_ENV_OVERRIDES = MappingProxyType({"CLAUDE_CODE_MAX_RETRIES": "3"})
#: The arguments that ask ``claude`` whether its login still works: ``claude auth
#: status``, which exits non-zero once the login is gone.
DFLT_AUTH_CHECK = ("auth", "status")
#: Seconds :meth:`ClaudeHeadless.preflight` gives that check to answer.
DFLT_AUTH_TIMEOUT_S = 15.0

PROMPT_FILE = "prompt.md"
STREAM_FILE = "stream.jsonl"
STDERR_FILE = "stderr.log"
RECORD_FILE = "record.json"
#: The only instruction on the command line; the prompt itself stays in its file.
PROMPT_POINTER = "Read and follow the instructions in {prompt_path}"
_SESSION_FLAGS = MappingProxyType({FRESH: "--session-id", RESUME: "--resume"})

_CONFIG_ERROR = "config_error"
_TIMED_OUT = "timed_out"
_AUTH_EXPIRED = "auth_expired"
#: record.json keys beyond the RunRecord's own fields.
_CANCEL_REQUESTED_AT = "cancel_requested_at"
_CANCEL_SIGNAL = "cancel_signal"
#: When the signal in ``cancel_signal`` was first sent: the kill grace counts from it.
_CANCEL_SIGNAL_AT = "cancel_signal_at"
#: When this processor spawned the run's process, by its own clock: the start the run's pid
#: must match to be taken for the run's process (see :meth:`ClaudeHeadless.status`).
_SPAWNED_AT = "spawned_at"


@dataclass(frozen=True)
class Job:
    """Everything a :class:`Processor` needs to run one case once.

    ``run_id`` names the run, and its directory, so it is one path segment. ``prompt``
    is the whole prompt (see :func:`liaise.prompt.compose_case_prompt`), ``cwd`` is where
    the work happens, and ``json_schema`` is the structured result the run must end
    with. ``timeout_minutes`` is the wall clock the tick enforces; a processor does not.
    ``session_id`` is the case's stored session, if it has one: the tick hands it to
    :meth:`Processor.resume`, while ``start`` always opens a new session.
    """

    run_id: str
    case_id: str
    subject: str
    prompt: str
    cwd: str
    permission_mode: str
    timeout_minutes: int
    json_schema: Mapping[str, Any]
    session_id: Optional[str] = None


@runtime_checkable
class Processor(Protocol):
    """What the tick needs to run a case's work: the processor seam (``processor=``)."""

    def preflight(self, job: Job) -> Health:
        """Whether ``job`` could start now, and if not why or until when. Starts nothing."""
        ...

    def start(self, job: Job) -> RunRecord:
        """Start ``job`` in a new session; for a ``run_id`` already started, its record."""
        ...

    def resume(self, session_id: str, job: Job) -> RunRecord:
        """Start ``job`` continuing ``session_id``, in the same permission mode as ``start``."""
        ...

    def status(self, run: RunRecord, *, persist: bool = True) -> RunRecord:
        """``run`` with its heartbeat, status and end time refreshed. Never blocks.

        With ``persist=False``, as in a dry run, the processor writes nothing of its own.
        """
        ...

    def cancel(self, run: RunRecord, *, mode: str = "graceful") -> RunRecord:
        """Ask ``run`` to stop (see :data:`CANCEL_MODES`); its session stays resumable."""
        ...

    def collect(
        self, run: RunRecord, *, timed_out: bool = False, persist: bool = True
    ) -> Optional[RunResult]:
        """``run``'s result once it has finished, or None while it is still going.

        ``persist`` is as for :meth:`status`.
        """
        ...


def child_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment a run gets: ``environ`` without :data:`SCRUBBED_ENV_VARS`, plus
    :data:`CHILD_ENV_OVERRIDES`.

    >>> env = child_env({"PATH": "/bin", "ANTHROPIC_API_KEY": "k"})
    >>> sorted(env)
    ['CLAUDE_CODE_MAX_RETRIES', 'PATH']
    """
    scrubbed = {name.upper() for name in SCRUBBED_ENV_VARS}
    env = {
        name: value for name, value in environ.items() if name.upper() not in scrubbed
    }
    env.update(CHILD_ENV_OVERRIDES)
    return env


class ClaudeHeadless:
    """The default :class:`Processor`: the ``claude`` CLI, headless, stream-JSON, detached.

    ``runs_dir`` is where each run's directory goes; the tick passes
    ``<state_dir>/runs``, and nothing but :meth:`preflight` works without it.
    ``claude_bin`` is the command, found on ``PATH`` or given as a path. ``grace_s`` is
    how long a graceful cancel waits after its interrupt before it may terminate, and
    ``kill_grace_s`` how long a run that was sent SIGTERM may go on before a cancel kills
    it (POSIX). ``auth_check`` is the arguments that ask ``claude`` whether its login
    still works (:data:`DFLT_AUTH_CHECK`), which :meth:`preflight` gives
    ``auth_timeout_s`` seconds to answer; None skips that check, for a ``claude`` without
    the command.

    A run is spawned in its own process group (a new session on POSIX), so it outlives
    the tick that started it and a cancel reaches everything it started. Its environment
    is :func:`child_env`.
    """

    def __init__(
        self,
        *,
        claude_bin: Union[str, os.PathLike] = DFLT_CLAUDE_BIN,
        runs_dir: Optional[Union[str, os.PathLike]] = None,
        grace_s: float = DFLT_GRACE_S,
        kill_grace_s: float = KILL_GRACE_S,
        auth_check: Optional[Sequence[str]] = DFLT_AUTH_CHECK,
        auth_timeout_s: float = DFLT_AUTH_TIMEOUT_S,
    ):
        self.claude_bin = str(claude_bin)
        self.runs_dir = (
            Path(runs_dir).expanduser().absolute() if runs_dir is not None else None
        )
        self.grace_s = grace_s
        self.kill_grace_s = kill_grace_s
        self.auth_check = tuple(auth_check or ())
        self.auth_timeout_s = auth_timeout_s
        # The processes this instance spawned. A child that exits stays a zombie until
        # its parent reaps it, and a zombie's pid still looks alive, so for these the
        # liveness check is Popen.poll(), which reaps. A pid is only checked for runs
        # another process (an earlier tick) started.
        self._children: dict[str, subprocess.Popen] = {}

    # ---- the verbs ----

    def preflight(self, job: Job) -> Health:
        """Whether ``job`` could start now. It starts no run.

        ``config_error`` without ``runs_dir``, a runnable ``claude_bin``, or ``job.cwd``.
        Then ``auth_expired`` when ``claude auth status`` (``auth_check``) exits non-zero,
        so a login that has expired costs no run. A check that cannot be run, or does not
        answer within ``auth_timeout_s``, tells nothing: preflight passes, and the run's
        own classification has the last word.
        """
        claude = _resolve_bin(self.claude_bin)
        if (
            self.runs_dir is None
            or claude is None
            or not Path(job.cwd).expanduser().is_dir()
        ):
            return Health(ok=False, error=_CONFIG_ERROR)
        if self.auth_check and self._login_expired(claude):
            return Health(ok=False, error=_AUTH_EXPIRED)
        return Health(ok=True)

    def _login_expired(self, claude: str) -> bool:
        """Whether ``claude <auth_check>`` says the login is gone, by exiting non-zero.

        Its output is not read, so it goes nowhere: a pipe could keep the wait going past
        the timeout on Windows, where the child of a ``.cmd`` shim holds it open.
        """
        try:
            done = subprocess.run(
                [claude, *self.auth_check],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=child_env(os.environ),
                timeout=self.auth_timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode != 0

    def start(self, job: Job) -> RunRecord:
        """Spawn ``job`` in a new session (``--session-id``) and return its record at once.

        Idempotent: when ``job.run_id`` already has a record of ``job.case_id``, that record
        is returned and nothing is spawned. A record another case's run wrote under that id
        is neither returned nor touched: the run returned is finished at once, spawned
        nothing, and :meth:`collect` classifies it as ``config_error``. Never raises for a
        command that cannot be spawned: the run is recorded as finished with the reason in
        its ``stderr.log``, which :meth:`collect` classifies as ``config_error``.
        """
        return self._launch(job, mode=FRESH, session_id=str(uuid.uuid4()))

    def resume(self, session_id: str, job: Job) -> RunRecord:
        """Like :meth:`start`, but continuing ``session_id`` (``--resume``)."""
        return self._launch(job, mode=RESUME, session_id=session_id)

    def status(self, run: RunRecord, *, persist: bool = True) -> RunRecord:
        """``run`` refreshed from its files and process, and saved to its ``record.json``.

        ``heartbeat_at`` is the stream's mtime. The run is finished once its stream holds
        a ``result`` event or its process is gone; ``ended_at`` is then the stream's mtime.
        A run another process started (an earlier tick) is known by its pid, which counts
        as its process only while that process started when the run was spawned (see
        :meth:`_process_is_the_run`): a pid a later process was given, after a reboot say,
        is gone, and a live pid whose start cannot be read is taken to be the run. It reads
        files and, for such a pid, asks ``ps`` (bounded by a timeout), so it never waits on
        the run. With ``persist=False``, as in a dry run, ``record.json`` is left as it was.
        A run whose id names another case's record is finished, and nothing is written.
        """
        return self._refreshed(run, persist=persist)[0]

    def _refreshed(
        self, run: RunRecord, *, persist: bool
    ) -> tuple[RunRecord, Optional[bool]]:
        """``run`` refreshed as :meth:`status` says, and :meth:`_process_is_the_run`'s answer.

        That answer is None, too, for a run already finished, whose process is not asked about.
        """
        if self._names_another_case(run):
            finished = replace(run, status=FINISHED, ended_at=run.ended_at or _utcnow())
            return finished, False
        stream_path = self._stream_path(run)
        heartbeat = _mtime(stream_path)
        process = None if run.status == FINISHED else self._process_is_the_run(run)
        finished = (
            run.status == FINISHED
            or process is False
            or _read_stream(stream_path).result is not None
        )
        updated = replace(
            run,
            status=FINISHED if finished else RUNNING,
            heartbeat_at=heartbeat or run.heartbeat_at,
            ended_at=(run.ended_at or heartbeat or _utcnow()) if finished else None,
        )
        child = self._children.get(run.run_id)
        if finished and child is not None and child.poll() is not None:
            del self._children[run.run_id]
        if persist:
            self._save(updated)
        return updated, process

    def cancel(
        self,
        run: RunRecord,
        *,
        mode: str = "graceful",
        now: Optional[datetime] = None,
    ) -> RunRecord:
        """Ask ``run`` to stop, and return its refreshed record (usually still running).

        On POSIX, ``graceful`` sends SIGINT to the run's process group the first time, and
        SIGTERM on a later call once ``grace_s`` has passed since that first request;
        ``now`` sends SIGTERM. A run still alive ``kill_grace_s`` after its SIGTERM is sent
        SIGKILL by the next call, whatever the mode. On Windows every call terminates the
        run and its children outright, so there is nothing to escalate to. When the cancel
        was first requested, the last signal, and when that signal was first sent are kept
        in ``record.json``, so the next tick's call continues the same cancel. ``now`` is
        the cancel's clock (the current UTC time when None). A finished run is returned as
        it is. Only a process verified as the run's (see :meth:`status`) is signalled: a
        live pid whose start cannot be read may be another process's, so it is sent nothing,
        and only the request is recorded.
        """
        require_one_of(mode, CANCEL_MODES, what="cancel mode")
        current, verified = self._refreshed(run, persist=True)
        if current.status == FINISHED:
            return current
        now = now if now is not None else _utcnow()
        raw = self._raw_record(run.run_id)
        requested = _parse_time(raw.get(_CANCEL_REQUESTED_AT))
        previous = raw.get(_CANCEL_SIGNAL)
        previous_at = _parse_time(raw.get(_CANCEL_SIGNAL_AT)) or requested
        extras = {_CANCEL_REQUESTED_AT: (requested or now).isoformat()}
        sent = None
        if verified:
            sent = self._send_cancel(
                current,
                mode=mode,
                requested=requested,
                previous=previous,
                previous_at=previous_at,
                now=now,
            )
        if sent:
            first_sent = previous_at if sent == previous and previous_at else now
            extras[_CANCEL_SIGNAL] = sent
            extras[_CANCEL_SIGNAL_AT] = first_sent.isoformat()
        self._save(current, extras=extras)
        return current

    def collect(
        self, run: RunRecord, *, timed_out: bool = False, persist: bool = True
    ) -> Optional[RunResult]:
        """``run``'s result, or None while it is running. Never raises on a bad stream.

        The outcomes are the last ``result`` event's ``structured_output.outcomes``; if
        any of them is malformed the run has no outcomes. The error class comes from
        :func:`liaise.errors.classify`, given ``stderr.log`` and ``timed_out`` (which the
        tick sets for a run it cancelled for passing its wall clock). ``persist`` is as
        for :meth:`status`. A run whose id names another case's record is ``config_error``,
        and that record's files are not read.
        """
        if self._names_another_case(run):
            return RunResult(run_id=run.run_id, error=_CONFIG_ERROR)
        current = self.status(run, persist=persist)
        if current.status != FINISHED:
            return None
        return _run_result(
            current,
            _read_stream(self._stream_path(current)),
            stderr_text=_read_text(self.run_dir(run.run_id) / STDERR_FILE),
            timed_out=timed_out,
        )

    # ---- run files ----

    def run_dir(self, run_id: str) -> Path:
        """The directory holding ``run_id``'s files. Raises ``ValueError`` without
        ``runs_dir``, or for a ``run_id`` that is not one path segment."""
        if self.runs_dir is None:
            raise ValueError(
                "ClaudeHeadless needs runs_dir= to know where each run's files go "
                "(the tick passes <state_dir>/runs)"
            )
        if run_id in ("", ".", "..") or "/" in run_id or "\\" in run_id:
            raise ValueError(
                f"run_id {run_id!r} must be one path segment: it names the run's "
                f"directory under {self.runs_dir}"
            )
        return self.runs_dir / run_id

    def _stream_path(self, run: RunRecord) -> Path:
        if run.stream_path:
            return Path(run.stream_path)
        return self.run_dir(run.run_id) / STREAM_FILE

    def _raw_record(self, run_id: str) -> dict[str, Any]:
        try:
            data = json.loads((self.run_dir(run_id) / RECORD_FILE).read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _load(self, run_id: str) -> Optional[RunRecord]:
        data = self._raw_record(run_id)
        try:
            return RunRecord.from_dict(data) if data else None
        except (ValueError, TypeError):
            return None

    def _names_another_case(self, run: RunRecord) -> bool:
        """Whether ``run``'s id names a ``record.json`` another case's run wrote.

        Such a run is never started, and its files are never touched: it is finished at
        once, and collects as ``config_error``.
        """
        recorded = self._raw_record(run.run_id).get("case_id")
        return recorded is not None and recorded != run.case_id

    def _save(
        self, record: RunRecord, *, extras: Optional[Mapping[str, Any]] = None
    ) -> None:
        """Write ``record`` to its ``record.json``, keeping the extra keys already there."""
        run_dir = self.run_dir(record.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        data = {**self._raw_record(record.run_id), **record.to_dict(), **(extras or {})}
        _write_json_atomically(run_dir / RECORD_FILE, data)

    # ---- spawning and signalling ----

    def _launch(self, job: Job, *, mode: str, session_id: str) -> RunRecord:
        existing = self._load(job.run_id)
        if existing is not None and existing.case_id != job.case_id:
            # Another case's run has this id: spawn nothing, and leave its files alone.
            now = _utcnow()
            return RunRecord(
                run_id=job.run_id,
                case_id=job.case_id,
                subject=job.subject,
                mode=mode,
                status=FINISHED,
                started_at=now,
                ended_at=now,
            )
        if existing is not None:
            return existing
        run_dir = self.run_dir(job.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = run_dir / PROMPT_FILE
        prompt_path.write_text(job.prompt, encoding="utf-8")
        stream_path, stderr_path = run_dir / STREAM_FILE, run_dir / STDERR_FILE
        argv = self._argv(
            job, prompt_path=prompt_path, mode=mode, session_id=session_id
        )
        record = RunRecord(
            run_id=job.run_id,
            case_id=job.case_id,
            subject=job.subject,
            mode=mode,
            status=RUNNING,
            started_at=_utcnow(),
            session_id=session_id,
            stream_path=str(stream_path),
        )
        spawned: dict[str, Any] = {}
        try:
            with open(stream_path, "wb") as stdout, open(stderr_path, "wb") as stderr:
                process = subprocess.Popen(
                    argv,
                    cwd=Path(job.cwd).expanduser(),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    env=child_env(os.environ),
                    **_detached_spawn_kwargs(),
                )
        except OSError as error:
            stderr_path.write_text(
                f"liaise: could not start {argv[0]!r} in {job.cwd!r}: {error}\n",
                encoding="utf-8",
            )
            record = replace(record, status=FINISHED, ended_at=record.started_at)
        else:
            self._children[job.run_id] = process
            record = replace(record, pid=process.pid)
            # Stamped just before the spawn, and kept apart from started_at, which the tick
            # sets to its own clock once the run is in its ledger.
            spawned[_SPAWNED_AT] = record.started_at.isoformat()
        self._save(record, extras=spawned)
        return record

    def _argv(
        self, job: Job, *, prompt_path: Path, mode: str, session_id: str
    ) -> list[str]:
        return [
            # Resolved, so a Windows `claude.cmd` is found the way a shell would find it.
            _resolve_bin(self.claude_bin) or self.claude_bin,
            "-p",
            PROMPT_POINTER.format(prompt_path=prompt_path),
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            job.permission_mode,
            "--json-schema",
            json.dumps(job.json_schema),
            _SESSION_FLAGS[mode],
            session_id,
        ]

    def _process_is_the_run(self, run: RunRecord) -> Optional[bool]:
        """Whether ``run``'s process is alive and is the run's; None when it lives but cannot be told.

        A process this instance spawned is its ``Popen``: alive until ``poll()`` says it has
        exited, which also reaps it, and its pid names no other process while it is held.
        Any other pid, of a run an earlier tick started, is the run's only when
        :func:`liaise.workspace.pid_matches_start` says so against the spawn time kept in
        ``record.json``, or the run's recorded start without one: a pid is reused once its
        process is gone, after a reboot say, so a live pid alone proves nothing.
        """
        process = self._children.get(run.run_id)
        if process is not None:
            return process.poll() is None
        if run.pid is None:
            return False
        # Imported here to keep processor free of an import-time dependency on workspace.
        from liaise.workspace import pid_matches_start

        spawned = _parse_time(self._raw_record(run.run_id).get(_SPAWNED_AT))
        return pid_matches_start(run.pid, spawned or run.started_at)

    def _send_cancel(
        self,
        run: RunRecord,
        *,
        mode: str,
        requested: Optional[datetime],
        previous: Optional[str],
        previous_at: Optional[datetime],
        now: datetime,
    ) -> Optional[str]:
        """Signal ``run`` as ``mode`` and the cancel so far ask. Returns what was sent, or None.

        ``requested`` is when the cancel was first asked for, ``previous`` the last signal
        it sent and ``previous_at`` when that signal was first sent (all None before any).
        """

        def posix_signal() -> Optional[signal.Signals]:
            """Interrupt, then terminate, then kill; None to wait.

            SIGINT on a graceful cancel's first call. SIGTERM for ``now``, or once
            ``grace_s`` has passed since the first request. SIGKILL once ``kill_grace_s``
            has passed since SIGTERM was first sent. A signal already sent is sent again,
            except an interrupt within its grace period.
            """
            if previous == signal.SIGKILL.name:
                return signal.SIGKILL
            if previous == signal.SIGTERM.name:
                kill_grace = timedelta(seconds=self.kill_grace_s)
                kill_due = previous_at is not None and now - previous_at >= kill_grace
                return signal.SIGKILL if kill_due else signal.SIGTERM
            grace = timedelta(seconds=self.grace_s)
            if mode == "now" or (requested is not None and now - requested >= grace):
                return signal.SIGTERM
            if requested is None:
                return signal.SIGINT
            return None  # interrupted already, and still within the grace period

        if run.pid is None:
            return None
        if sys.platform == "win32":
            self._terminate_tree(run)  # already a hard kill: nothing to escalate to
            return "terminate"
        sig = posix_signal()
        if sig is None:
            return None
        try:
            # The run was spawned as a session leader, so its process group id is its
            # pid. os.getpgid(pid) is avoided on purpose: for a pid since reused by an
            # unrelated process, it would name that process's group.
            os.killpg(run.pid, sig)
        except (ProcessLookupError, PermissionError):
            return None
        return sig.name

    def _terminate_tree(self, run: RunRecord) -> None:
        """Windows: end the run and every process it started.

        Terminating the spawned process alone would leave its children running, and
        behind a ``.cmd`` shim the child is the agent itself.
        """
        try:
            done = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(run.pid)],
                capture_output=True,
                check=False,
            )
            if done.returncode == 0:
                return
        except OSError:
            pass
        process = self._children.get(run.run_id)
        try:
            if process is not None:
                process.terminate()
            else:
                os.kill(run.pid, signal.SIGTERM)
        except OSError:
            pass


class EchoProcessor:
    """A :class:`Processor` that runs nothing: it records jobs and returns scripted results.

    ``results`` maps a case id to the :class:`~liaise.model.RunResult` its runs return,
    and ``default`` is returned for any other case; without either, a run reports one
    ``note``. ``health`` is what :meth:`preflight` returns (ok by default). A run is
    finished as soon as it starts. It writes nothing, so ``persist`` changes nothing. ``jobs``, ``preflights`` and ``cancels`` record the
    calls, for tests to assert on.
    """

    def __init__(
        self,
        *,
        results: Optional[Mapping[str, RunResult]] = None,
        default: Optional[RunResult] = None,
        health: Optional[Health] = None,
    ):
        self.jobs: list[Job] = []
        self.preflights: list[Job] = []
        self.cancels: list[tuple[str, str]] = []
        self._results = dict(results or {})
        self._default = default
        self._health = health if health is not None else Health(ok=True)
        self._runs: dict[str, RunRecord] = {}

    def preflight(self, job: Job) -> Health:
        self.preflights.append(job)
        return self._health

    def start(self, job: Job) -> RunRecord:
        return self._run(job, mode=FRESH, session_id=f"echo-session-{job.run_id}")

    def resume(self, session_id: str, job: Job) -> RunRecord:
        return self._run(job, mode=RESUME, session_id=session_id)

    def status(self, run: RunRecord, *, persist: bool = True) -> RunRecord:
        known = self._runs.get(run.run_id)
        if known is not None:
            return known
        return replace(run, status=FINISHED, ended_at=run.ended_at or _utcnow())

    def cancel(self, run: RunRecord, *, mode: str = "graceful") -> RunRecord:
        require_one_of(mode, CANCEL_MODES, what="cancel mode")
        self.cancels.append((run.run_id, mode))
        return self.status(run)

    def collect(
        self, run: RunRecord, *, timed_out: bool = False, persist: bool = True
    ) -> Optional[RunResult]:
        scripted = self._results.get(run.case_id, self._default)
        if scripted is None:
            scripted = RunResult(
                run_id=run.run_id,
                outcomes=(
                    Outcome(kind="note", text=f"EchoProcessor ran {run.case_id}."),
                ),
                summary="echo",
            )
        result = replace(
            scripted,
            run_id=run.run_id,
            session_id=scripted.session_id or run.session_id,
        )
        return replace(result, error=_TIMED_OUT) if timed_out else result

    def _run(self, job: Job, *, mode: str, session_id: str) -> RunRecord:
        if job.run_id in self._runs:
            return self._runs[job.run_id]
        self.jobs.append(job)
        now = _utcnow()
        record = RunRecord(
            run_id=job.run_id,
            case_id=job.case_id,
            subject=job.subject,
            mode=mode,
            status=FINISHED,
            started_at=now,
            heartbeat_at=now,
            ended_at=now,
            session_id=session_id,
        )
        self._runs[job.run_id] = record
        return record


# ---- helpers ----


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _detached_spawn_kwargs() -> dict[str, Any]:
    """Popen arguments giving a run its own process group, apart from the tick's."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _resolve_bin(claude_bin: str) -> Optional[str]:
    """``claude_bin`` as a runnable path: found on ``PATH``, or an executable file as given."""
    found = shutil.which(claude_bin)
    if found:
        return found
    path = Path(claude_bin).expanduser()
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def _mtime(path: Path) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_stream(path: Path) -> StreamSummary:
    """What :func:`liaise.errors.parse_stream` finds in the file; an empty summary if none."""
    try:
        with open(path, encoding="utf-8", errors="replace") as lines:
            return parse_stream(lines)
    except OSError:
        return parse_stream(())


def _write_json_atomically(path: Path, data: Mapping[str, Any]) -> None:
    """Write through a temporary file, so a reader never sees half a record."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _run_result(
    run: RunRecord, summary: StreamSummary, *, stderr_text: str, timed_out: bool
) -> RunResult:
    result = summary.result or {}
    structured = _structured_output(result)
    outcomes = _valid_outcomes(structured.get("outcomes"))
    return RunResult(
        run_id=run.run_id,
        outcomes=outcomes or (),
        usage=_mapping(result.get("usage")),
        cost_usd=_number(result.get("total_cost_usd")),
        rate_limit=dict(summary.rate_limit) if summary.rate_limit is not None else None,
        error=classify(
            summary,
            stderr_text=stderr_text,
            timed_out=timed_out,
            has_outcomes=outcomes is not None,
        ),
        session_id=_string(result.get("session_id")) or run.session_id,
        summary=_string(structured.get("summary")) or _string(result.get("result")),
    )


def _structured_output(result: Mapping[str, Any]) -> Mapping[str, Any]:
    value = result.get("structured_output")
    if isinstance(value, str):  # tolerate a result that carries it as JSON text
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, Mapping) else {}


def _valid_outcomes(raw: Any) -> Optional[tuple[Outcome, ...]]:
    """Every outcome in ``raw`` as an :class:`Outcome`; None if there are none or any is malformed."""
    if not isinstance(raw, list) or not raw:
        return None
    outcomes = tuple(map(_outcome, raw))
    return None if any(outcome is None for outcome in outcomes) else outcomes


def _outcome(item: Any) -> Optional[Outcome]:
    """``item`` as an :class:`Outcome`, or None when it is not one.

    Only the shape is checked here: a known kind, string text and reason, a list of
    string questions. What the outcomes mean together is ``liaise.outcomes``'s job.
    """
    if not isinstance(item, Mapping) or item.get("kind") not in OUTCOME_KINDS:
        return None
    text, reason = _or_default(item, "text", ""), _or_default(item, "reason", "")
    questions = _or_default(item, "questions", [])
    if not (isinstance(text, str) and isinstance(reason, str)):
        return None
    if not isinstance(questions, list) or not all(
        isinstance(q, str) for q in questions
    ):
        return None
    return Outcome(
        kind=item["kind"], text=text, questions=tuple(questions), reason=reason
    )


def _or_default(item: Mapping[str, Any], key: str, default: Any) -> Any:
    value = item.get(key)
    return default if value is None else value


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""
