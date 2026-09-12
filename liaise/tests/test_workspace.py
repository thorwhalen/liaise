"""Tests for liaise.workspace: the live-session collision check, the checkout lock, pid
liveness, and building a subject's shared checkout.

No test reads the real Claude Code session records: every SharedCheckout that is asked
for a conflict gets a sessions_dir under tmp_path."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from liaise import workspace as workspace_module
from liaise.workspace import (
    PID_START_TOLERANCE,
    SharedCheckout,
    _parse_etime,
    pid_is_alive,
    pid_matches_start,
    process_started_at,
    workspace_for,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
LIVE_PID = os.getpid()
DEAD_PID = 999999999  # not a real pid
#: When this module was imported: after this process started, and early in its test session.
IMPORTED_AT = datetime.now(timezone.utc)
#: How long before this module's import the test process may have started: pytest's own start,
#: and the collection of the modules before this one.
SESSION_START_SLACK = timedelta(minutes=1)
#: How far a start that is read may lie from one measured: ps counts whole seconds, and a child
#: takes a moment to report itself.
START_READ_MARGIN = timedelta(seconds=2)
#: How long a test waits, at most, for a child it started to exit.
CHILD_WAIT_S = 10.0
#: A child that says its own pid and when it began, then waits for its stdin to close. Its own
#: pid, since on Windows a venv's python.exe is a launcher whose child is the interpreter.
_REPORTS_ITS_START = (
    "import os, sys\n"
    "from datetime import datetime, timezone\n"
    "print(os.getpid(), datetime.now(timezone.utc).isoformat(), flush=True)\n"
    "sys.stdin.read()\n"
)


@pytest.fixture
def checkout(tmp_path):
    path = tmp_path / "code" / "app"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def sessions(tmp_path):
    path = tmp_path / "sessions"
    path.mkdir()
    return path


@pytest.fixture
def shared(tmp_path, checkout, sessions):
    return SharedCheckout(checkout, lock_dir=tmp_path / "locks", sessions_dir=sessions)


def _record(sessions, filename, **record):
    (sessions / filename).write_text(json.dumps(record))


# ---- pid liveness ----


def test_pid_is_alive():
    assert pid_is_alive(LIVE_PID)
    assert not pid_is_alive(DEAD_PID)
    for not_a_pid in (0, -1, 2**40, True, "123", None, 1.5, [LIVE_PID]):
        assert not pid_is_alive(not_a_pid)


# ---- when a process started, and whether a pid is still the process that started then (S9 #1) ----


@pytest.mark.parametrize(
    "text, elapsed",
    [
        ("00:07", timedelta(seconds=7)),
        ("   12:34\n", timedelta(minutes=12, seconds=34)),
        ("01:02:03", timedelta(hours=1, minutes=2, seconds=3)),
        ("3-04:05:06", timedelta(days=3, hours=4, minutes=5, seconds=6)),
        ("56-23:59:59", timedelta(days=56, hours=23, minutes=59, seconds=59)),
    ],
    ids=["mm:ss", "padded", "hh:mm:ss", "dd-hh:mm:ss", "the largest of each field"],
)
def test_the_elapsed_time_ps_prints_is_read_in_each_of_its_forms(text, elapsed):
    assert _parse_etime(text) == elapsed


@pytest.mark.parametrize(
    "text",
    ["", "   ", "7", "07:", "3-05:06", "00:60", "60:00", "1-24:00:00", "ab:cd", "-00:07", "01:02:03:04", "00:07 later"],
)
def test_anything_else_is_no_elapsed_time(text):
    assert _parse_etime(text) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX: the start is read from ps")
def test_the_start_of_this_process_is_read_within_its_test_session():
    started = process_started_at(os.getpid())
    assert started is not None
    assert IMPORTED_AT - SESSION_START_SLACK <= started <= IMPORTED_AT + START_READ_MARGIN


def test_the_start_of_a_child_is_read_within_seconds_of_when_it_began():
    """On POSIX from ps, on Windows from GetProcessTimes."""
    before = datetime.now(timezone.utc)
    child = subprocess.Popen(
        [sys.executable, "-c", _REPORTS_ITS_START], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        pid, began = child.stdout.readline().split()
        started = process_started_at(int(pid))
    finally:
        child.stdin.close()
        child.wait(timeout=CHILD_WAIT_S)
    assert started is not None
    assert before - START_READ_MARGIN <= started <= datetime.fromisoformat(began) + START_READ_MARGIN


def test_no_live_process_has_a_start():
    assert process_started_at(DEAD_PID) is None
    for not_a_pid in (0, -1, 2**40, True, "123", None, 1.5):
        assert process_started_at(not_a_pid) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX: the start is read from ps")
def test_without_ps_a_start_cannot_be_read(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    assert process_started_at(LIVE_PID) is None


def test_a_live_pid_is_the_process_only_when_it_started_within_the_tolerance(monkeypatch):
    second = timedelta(seconds=1)
    monkeypatch.setattr(workspace_module, "process_started_at", lambda pid: T0)
    assert pid_matches_start(LIVE_PID, T0) is True
    assert pid_matches_start(LIVE_PID, T0 - PID_START_TOLERANCE) is True
    assert pid_matches_start(LIVE_PID, T0 + PID_START_TOLERANCE) is True
    assert pid_matches_start(LIVE_PID, T0 - PID_START_TOLERANCE - second) is False  # started later: another process
    assert pid_matches_start(LIVE_PID, T0 + PID_START_TOLERANCE + second) is False
    assert pid_matches_start(LIVE_PID, T0 - second, tolerance=timedelta(0)) is False
    assert pid_matches_start(DEAD_PID, T0) is False

    monkeypatch.setattr(workspace_module, "process_started_at", lambda pid: None)
    assert pid_matches_start(LIVE_PID, T0) is None  # alive, and which process it is cannot be told
    assert pid_matches_start(DEAD_PID, T0) is False


# ---- the collision check ----


def test_a_live_session_inside_the_checkout_is_a_conflict(shared, checkout, sessions):
    cwd = checkout / "src"
    _record(sessions, "1.json", pid=LIVE_PID, sessionId="s-1", cwd=str(cwd), name="fixing-the-footer")
    assert shared.conflict() == f"live session fixing-the-footer in {cwd}"


def test_an_unnamed_session_in_the_checkout_itself_is_named_by_its_session_id(shared, checkout, sessions):
    _record(sessions, "1.json", pid=LIVE_PID, sessionId="s-1", cwd=str(checkout))
    assert shared.conflict() == f"live session s-1 in {checkout}"


def test_our_own_runs_are_not_a_conflict(tmp_path, checkout, sessions):
    _record(sessions, "1.json", pid=LIVE_PID, sessionId="s-1", cwd=str(checkout / "src"))
    ours = SharedCheckout(checkout, lock_dir=tmp_path / "locks", sessions_dir=sessions, own_pids=[LIVE_PID])
    assert ours.conflict() is None
    theirs = SharedCheckout(checkout, lock_dir=tmp_path / "locks", sessions_dir=sessions, own_pids=[DEAD_PID])
    assert theirs.conflict() == f"live session s-1 in {checkout / 'src'}"


def test_a_session_whose_process_is_gone_is_not_a_conflict(shared, checkout, sessions):
    _record(sessions, "1.json", pid=DEAD_PID, sessionId="s-1", cwd=str(checkout / "src"))
    assert shared.conflict() is None


@pytest.mark.parametrize(
    "parts",
    [
        ("code", "app2"),  # the sibling whose name starts with the checkout's
        ("code", "app2", "src"),
        ("code",),  # the checkout's parent
        ("elsewhere", "app"),
    ],
)
def test_a_session_outside_the_checkout_is_not_a_conflict(tmp_path, shared, sessions, parts):
    outside = tmp_path.joinpath(*parts)
    outside.mkdir(parents=True, exist_ok=True)
    _record(sessions, "1.json", pid=LIVE_PID, sessionId="s-1", cwd=str(outside))
    assert shared.conflict() is None


def test_paths_are_compared_resolved(tmp_path, checkout, sessions):
    detour = checkout / ".." / "app" / "src"
    _record(sessions, "1.json", pid=LIVE_PID, sessionId="s-1", cwd=str(detour))
    spelled_otherwise = tmp_path / "code" / ".." / "code" / "app"
    shared = SharedCheckout(spelled_otherwise, lock_dir=tmp_path / "locks", sessions_dir=sessions)
    assert shared.path == checkout.resolve()
    assert shared.conflict() == f"live session s-1 in {detour}"


def test_a_missing_sessions_dir_is_no_conflict(tmp_path, checkout):
    shared = SharedCheckout(checkout, lock_dir=tmp_path / "locks", sessions_dir=tmp_path / "missing")
    assert shared.conflict() is None


def test_malformed_session_records_are_skipped(tmp_path, shared, checkout, sessions):
    inside = str(checkout / "src")
    (sessions / "a-not-json.json").write_text("{not json")
    (sessions / "b-not-utf8.json").write_bytes(b"\xff\xfe\x00garbage")
    (sessions / "c-a-list.json").write_text(json.dumps([LIVE_PID, inside]))
    (sessions / "d-a-directory.json").mkdir()
    malformed = [
        {"pid": str(LIVE_PID), "cwd": inside},
        {"pid": True, "cwd": inside},
        {"pid": 0, "cwd": inside},
        {"pid": -1, "cwd": inside},
        {"pid": 2**40, "cwd": inside},
        {"pid": [LIVE_PID], "cwd": inside},
        {"pid": LIVE_PID},
        {"pid": LIVE_PID, "cwd": None},
        {"pid": LIVE_PID, "cwd": ""},
        {"pid": LIVE_PID, "cwd": ["src"]},
        {"pid": LIVE_PID, "cwd": "src"},  # not absolute
        {"pid": LIVE_PID, "cwd": str(tmp_path / "elsewhere") + "\x00"},
    ]
    for n, record in enumerate(malformed):
        _record(sessions, f"e{n:02d}.json", **record)
    (sessions / "f-not-a-record.txt").write_text(json.dumps({"pid": LIVE_PID, "cwd": inside}))
    assert shared.conflict() is None

    # and a valid record after all of them is still found
    _record(sessions, "z-valid.json", pid=LIVE_PID, sessionId="s-9", cwd=inside)
    assert shared.conflict() == f"live session s-9 in {inside}"


def test_the_default_sessions_dir_is_claude_codes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    shared = SharedCheckout(tmp_path, lock_dir=tmp_path / "locks")
    assert shared.sessions_dir == tmp_path / ".claude" / "sessions"  # not read here


# ---- the lock ----


def test_acquire_writes_the_lock_and_release_removes_it(tmp_path, shared, checkout):
    assert shared.holder() is None
    assert shared.acquire(run_id="r1", pid=LIVE_PID, now=T0) is True

    digest = hashlib.sha1(str(checkout.resolve()).encode()).hexdigest()[:16]
    assert shared.lock_path == tmp_path / "locks" / f"{digest}.lock"
    record = {"pid": LIVE_PID, "run_id": "r1", "path": str(checkout.resolve()), "at": T0.isoformat()}
    assert json.loads(shared.lock_path.read_text()) == record
    assert shared.holder() == record

    assert shared.release(run_id="r1") is True
    assert shared.holder() is None
    assert shared.release(run_id="r1") is False
    assert list((tmp_path / "locks").iterdir()) == []  # no staged file left behind


def test_the_same_run_acquires_again_and_can_move_the_lock_to_its_own_pid(shared):
    assert shared.acquire(run_id="r1", pid=LIVE_PID, now=T0) is True
    assert shared.acquire(run_id="r1", pid=LIVE_PID, now=T0) is True
    run_pid = os.getppid()
    assert shared.acquire(run_id="r1", pid=run_pid, now=T0) is True
    assert shared.holder()["pid"] == run_pid
    assert shared.acquire(run_id="r2", pid=LIVE_PID, now=T0) is False


def test_a_different_run_is_refused_while_the_holder_is_alive(tmp_path, shared, sessions):
    assert shared.acquire(run_id="r1", pid=LIVE_PID, now=T0) is True
    assert shared.acquire(run_id="r2", pid=LIVE_PID, now=T0) is False
    assert shared.release(run_id="r2") is False
    assert shared.holder()["run_id"] == "r1"
    # the same checkout, spelled another way, is the same lock
    spelled_otherwise = tmp_path / "code" / ".." / "code" / "app"
    other = SharedCheckout(spelled_otherwise, lock_dir=tmp_path / "locks", sessions_dir=sessions)
    assert other.acquire(run_id="r2", pid=LIVE_PID, now=T0) is False


def test_a_lock_left_by_a_dead_pid_is_reclaimed(shared):
    assert shared.acquire(run_id="r1", pid=DEAD_PID, now=T0) is True
    assert shared.acquire(run_id="r2", pid=LIVE_PID, now=T0) is True
    assert shared.holder()["run_id"] == "r2"
    assert shared.release(run_id="r1") is False  # the old run holds nothing now
    assert shared.lock_path.exists()


def test_an_unreadable_lock_is_reclaimed(shared):
    shared.lock_path.parent.mkdir(parents=True)
    shared.lock_path.write_text("{half a lock")
    assert shared.holder() is None
    assert shared.release(run_id="r1") is False
    assert shared.acquire(run_id="r1", pid=LIVE_PID, now=T0) is True
    assert shared.holder()["run_id"] == "r1"


def test_each_checkout_has_its_own_lock(tmp_path, shared, sessions):
    sibling_path = tmp_path / "code" / "app2"
    sibling_path.mkdir()
    sibling = SharedCheckout(sibling_path, lock_dir=tmp_path / "locks", sessions_dir=sessions)
    assert shared.acquire(run_id="r1", pid=LIVE_PID, now=T0) is True
    assert sibling.acquire(run_id="r2", pid=LIVE_PID, now=T0) is True
    assert shared.lock_path != sibling.lock_path


# ---- a subject's workspace ----


def _subject(path):
    """Stands in for a Subject: workspace_for reads only subject.workspace.path."""
    return SimpleNamespace(slug="example-app", workspace=SimpleNamespace(kind="shared", path=path))


def test_workspace_for_builds_the_subjects_shared_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    shared = workspace_for(
        _subject("~/code/example-app"),
        lock_dir=tmp_path / "locks",
        sessions_dir=tmp_path / "sessions",
        own_pids=[LIVE_PID],
    )
    assert isinstance(shared, SharedCheckout)
    assert shared.path == (tmp_path / "code" / "example-app").resolve()
    assert shared.lock_dir == tmp_path / "locks"
    assert shared.sessions_dir == tmp_path / "sessions"
    assert shared.own_pids == frozenset({LIVE_PID})


def test_workspace_for_a_subject_without_a_workspace_path_is_none(tmp_path):
    assert workspace_for(_subject(""), lock_dir=tmp_path / "locks") is None
