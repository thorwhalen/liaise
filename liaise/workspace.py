"""The checkout a subject's runs share: one run at a time, and never beside a live session.

v0.1 has one kind of workspace (seam 5 in the design): the subject's own checkout,
shared by its runs. Two checks keep a run from trampling other work there:

- **The lock.** :meth:`SharedCheckout.acquire` writes
  ``<lock_dir>/<sha1(resolved path)[:16]>.lock``, the JSON ``{pid, run_id, path, at}``.
  A different run is refused while the holder's pid is alive, and a lock whose pid is
  dead is reclaimed. Like the run lock of 0.0.x it is advisory, which is enough to keep
  one machine's runs from colliding.
- **The collision check.** :meth:`SharedCheckout.conflict` reads Claude Code's session
  records (``~/.claude/sessions/*.json`` by default) and reports a live session, not one
  of liaise's own runs, whose ``cwd`` is the checkout or inside it: someone working
  there by hand.

Paths are compared resolved (``~`` expanded, symlinks followed) and on path-part
boundaries, so ``.../app`` never matches ``.../app2``.

It also says whether a pid still names a given process (:func:`pid_matches_start`): a pid
is reused once its process is gone, after a reboot say, so a live pid alone proves nothing.

The named replacement is a worktree per run, which would come in at :func:`workspace_for`.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union

if TYPE_CHECKING:
    from liaise.subjects import Subject

#: Where Claude Code keeps one JSON record per session.
DFLT_SESSIONS_DIR = "~/.claude/sessions"
#: The workspace locks' directory under ``state_dir``.
DFLT_LOCKS_SUBDIR = "locks"
#: How many hex digits of the resolved path's sha1 a lock file's name keeps.
DFLT_LOCK_DIGEST_LENGTH = 16
LOCK_SUFFIX = ".lock"
#: How far a process's start may lie from the start recorded for it, either side, for its
#: pid to be taken for that process (see :func:`pid_matches_start`). A process that started
#: further away is another one, which was given the pid after the first was gone.
PID_START_TOLERANCE = timedelta(minutes=2)
#: The command that reports a process's elapsed time on POSIX, and how many seconds it is
#: given to answer.
PS_COMMAND = "ps"
PS_TIMEOUT_S = 5.0

#: A pid is a positive signed 32-bit int on every platform liaise runs on.
_MAX_PID = 2**31 - 1
#: The Windows access right that lets a handle query a process and nothing more.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
#: What ``ps -o etime=`` prints, on macOS and Linux alike: ``[[dd-]hh:]mm:ss``.
_ETIME_PATTERN = re.compile(r"(?:(?:([0-9]+)-)?([0-9]+):)?([0-9]+):([0-9]+)")
_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60
_HOURS_PER_DAY = 24
#: Windows counts a FILETIME in 100-nanosecond ticks from 1601, UTC, in two 32-bit halves.
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_FILETIME_TICKS_PER_MICROSECOND = 10
_FILETIME_HALF_BITS = 32


def resolve_checkout(path: Union[str, os.PathLike]) -> Path:
    """``path`` in the one form checkouts are compared in: ``~`` expanded, absolute, resolved.

    :mod:`liaise.holds` spells ``checkout:<path>`` scopes with it too, so a hold set on
    one spelling of a checkout applies to every other.
    """
    return Path(path).expanduser().resolve()


#: What ``GetExitCodeProcess`` reports for a process that has not exited yet.
_STILL_ACTIVE = 259


def pid_is_alive(pid: Any) -> bool:
    """Whether ``pid`` names a live process. Never sends it a real signal.

    Anything but a positive int in pid range is no live process, so a malformed session
    record or lock never raises here. On POSIX this is ``os.kill(pid, 0)``, which delivers
    nothing. On Windows ``os.kill`` would terminate the process instead, so the check
    opens a query-only handle, and then asks for its exit code. Opening the handle is not
    enough: Windows keeps an exited process openable while anyone still holds a handle to
    it, such as the ``Popen`` that started it. Such a process is alive only if its exit
    code is still ``STILL_ACTIVE``. ``liaise.run._pid_is_alive``, retired with that module,
    lacked this check.
    """
    if not _is_pid(pid):
        return False
    if platform.system() == "Windows":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # cannot tell; an openable process counts as alive
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, it is just not ours to signal
    return True


def _is_pid(value: Any) -> bool:
    """Whether ``value`` is a positive int in pid range (a bool is not)."""
    return (
        not isinstance(value, bool) and isinstance(value, int) and 0 < value <= _MAX_PID
    )


def process_started_at(pid: Any) -> Optional[datetime]:
    """When the process ``pid`` started, in UTC; None when that cannot be determined.

    None, never an exception, for anything but a pid in range, a process that is gone, or a
    system that does not say. On POSIX it is now less the elapsed time ``ps -o etime=``
    reports, to the second, which reads the same on macOS and Linux and involves no time
    zone; ``ps`` is found on ``PATH``, else on the system's default path, and given
    :data:`PS_TIMEOUT_S` to answer. On Windows it is the creation time ``GetProcessTimes``
    reads through a query-only handle.
    """
    if not _is_pid(pid):
        return None
    if sys.platform == "win32":
        return _windows_process_started_at(pid)
    return _posix_process_started_at(pid)


def _posix_process_started_at(pid: int) -> Optional[datetime]:
    ps = shutil.which(PS_COMMAND) or shutil.which(PS_COMMAND, path=os.defpath)
    if ps is None:
        return None
    try:
        done = subprocess.run(
            [ps, "-o", "etime=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    elapsed = _parse_etime(done.stdout) if done.returncode == 0 else None
    return None if elapsed is None else datetime.now(timezone.utc) - elapsed


def _parse_etime(text: str) -> Optional[timedelta]:
    """The elapsed time ``ps -o etime=`` printed as ``[[dd-]hh:]mm:ss``; None for anything else.

    >>> _parse_etime(" 3-04:05:06")
    datetime.timedelta(days=3, seconds=14706)
    """
    match = _ETIME_PATTERN.fullmatch(text.strip())
    if match is None:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    has_days = match.group(1) is not None
    if (
        seconds >= _SECONDS_PER_MINUTE
        or minutes >= _MINUTES_PER_HOUR
        or (has_days and hours >= _HOURS_PER_DAY)
    ):
        return None
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _windows_process_started_at(pid: int) -> Optional[datetime]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    try:
        times = (ctypes.byref(when) for when in (created, exited, kernel, user))
        if not kernel32.GetProcessTimes(handle, *times):
            return None
    finally:
        kernel32.CloseHandle(handle)
    ticks = (created.dwHighDateTime << _FILETIME_HALF_BITS) | created.dwLowDateTime
    return _FILETIME_EPOCH + timedelta(
        microseconds=ticks // _FILETIME_TICKS_PER_MICROSECOND
    )


def pid_matches_start(
    pid: Any, started_at: datetime, *, tolerance: timedelta = PID_START_TOLERANCE
) -> Optional[bool]:
    """Whether ``pid`` is the live process that started at ``started_at``; None when that cannot be told.

    True when the process is alive (:func:`pid_is_alive`) and started
    (:func:`process_started_at`) within ``tolerance`` of ``started_at``, either side. False
    when it is gone, or started further away: its pid now names another process, given it
    after the first ended or a reboot. None when it is alive but its start cannot be read,
    so it may be either. Only a True pid may be signalled as the process that started then.
    ``started_at`` is timezone-aware.
    """
    if not pid_is_alive(pid):
        return False
    started = process_started_at(pid)
    if started is None:
        return None
    return abs(started - started_at) <= tolerance


class SharedCheckout:
    """One checkout shared by a subject's runs: its lock, and the live sessions inside it.

    ``path`` is resolved once (see :func:`resolve_checkout`). ``lock_dir`` holds one lock
    file per checkout. ``sessions_dir`` is where Claude Code keeps its session records
    (:data:`DFLT_SESSIONS_DIR` when None). ``own_pids`` are the pids of liaise's own
    runs, whose sessions are never a conflict.
    """

    def __init__(
        self,
        path: Union[str, os.PathLike],
        *,
        lock_dir: Union[str, os.PathLike],
        sessions_dir: Optional[Union[str, os.PathLike]] = None,
        own_pids: Iterable[int] = (),
    ):
        self.path = resolve_checkout(path)
        self.lock_dir = Path(lock_dir).expanduser()
        self.sessions_dir = Path(
            DFLT_SESSIONS_DIR if sessions_dir is None else sessions_dir
        ).expanduser()
        self.own_pids = frozenset(own_pids)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self.path)!r})"

    @property
    def lock_path(self) -> Path:
        """This checkout's lock file: ``<lock_dir>/<sha1(resolved path)[:16]>.lock``."""
        digest = hashlib.sha1(os.fsencode(self.path)).hexdigest()
        return self.lock_dir / f"{digest[:DFLT_LOCK_DIGEST_LENGTH]}{LOCK_SUFFIX}"

    # ---- the collision check ----

    def conflict(self) -> Optional[str]:
        """``live session <name> in <cwd>`` when another live session works in the checkout.

        A session record conflicts when its ``pid`` is alive and not in ``own_pids``, and
        its ``cwd`` is the checkout or a path inside it. The first such record, in file
        name order, is reported by its ``name``, else its ``sessionId``. None when there
        is none. A missing directory, an unreadable or malformed record, or a ``cwd`` that
        is not absolute never conflicts and never raises.
        """

        def records() -> Iterator[dict[str, Any]]:
            try:
                paths = sorted(self.sessions_dir.glob("*.json"))
            except OSError:
                return
            for path in paths:
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(record, dict):
                    yield record

        def inside(cwd: Any) -> bool:
            if not isinstance(cwd, str) or not cwd:
                return False
            try:
                where = Path(cwd)
                return where.is_absolute() and where.resolve().is_relative_to(self.path)
            except (OSError, RuntimeError, ValueError):
                return False

        for record in records():
            pid, cwd = record.get("pid"), record.get("cwd")
            if not pid_is_alive(pid) or pid in self.own_pids or not inside(cwd):
                continue
            name = record.get("name") or record.get("sessionId") or f"pid {pid}"
            return f"live session {name} in {cwd}"
        return None

    # ---- the lock ----

    def holder(self) -> Optional[dict[str, Any]]:
        """The lock's record, ``{pid, run_id, path, at}``, or None when there is no readable lock.

        The holder's pid may be dead: :meth:`acquire` reclaims such a lock.
        """
        try:
            record = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) else None

    def acquire(self, *, run_id: str, pid: int, now: Optional[datetime] = None) -> bool:
        """Take the checkout's lock for ``run_id``, held while ``pid`` lives; False when refused.

        Refused only while a different run holds the lock and its pid is alive. A lock
        left by a dead pid, or one that cannot be read, is reclaimed. The same run
        acquiring again succeeds and rewrites the lock, which is how a lock taken before
        a run starts moves to the run's own pid once it has one.
        """
        holder = self.holder()
        if (
            holder is not None
            and holder.get("run_id") != run_id
            and pid_is_alive(holder.get("pid"))
        ):
            return False
        now = now if now is not None else datetime.now(timezone.utc)
        record = {
            "pid": pid,
            "run_id": run_id,
            "path": str(self.path),
            "at": now.isoformat(),
        }
        lock_path = self.lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        staged = lock_path.with_name(f"{lock_path.name}.{os.getpid()}.tmp")
        staged.write_text(json.dumps(record), encoding="utf-8")
        os.replace(staged, lock_path)  # so a reader never sees half a lock
        return True

    def release(self, *, run_id: str) -> bool:
        """Remove the lock if ``run_id`` holds it; True when it was removed."""
        holder = self.holder()
        if holder is None or holder.get("run_id") != run_id:
            return False
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            return False
        return True


def workspace_for(
    subject: Subject,
    *,
    lock_dir: Union[str, os.PathLike],
    sessions_dir: Optional[Union[str, os.PathLike]] = None,
    own_pids: Iterable[int] = (),
) -> Optional[SharedCheckout]:
    """The shared checkout ``subject`` works in, or None when its file names no workspace path.

    ``lock_dir``, ``sessions_dir`` and ``own_pids`` go to :class:`SharedCheckout`.
    """
    path = subject.workspace.path
    if not path:
        return None
    return SharedCheckout(
        path, lock_dir=lock_dir, sessions_dir=sessions_dir, own_pids=own_pids
    )
