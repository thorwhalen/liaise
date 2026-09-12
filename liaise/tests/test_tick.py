"""Tests for liaise.tick: run_once end to end over fakes, and status_lines.

Every channel is a fake from liaise.testing, heard and written through correspond. The
processor is an EchoProcessor, or a subclass whose runs keep going. The labeler is
FakeGitHub, the store a dict, the clock explicit, and every path is under tmp_path, so no
test reads Claude Code's real session records, runs claude or gh, or sends anything.
acquaint is made unimportable, so no test reads real people records.
"""

from __future__ import annotations

import copy
import functools
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from correspond.errors import ChannelError

from liaise import tick as tick_module
from liaise import workspace as workspace_module
from liaise.cases import case_show_lines, set_case_state
from liaise.config import ConfigError, GlobalConfig
from liaise.gate import Divert
from liaise.github import FakeGitHub, Issue
from liaise.holds import hold
from liaise.ledger import Ledger
from liaise.model import Health, LedgerEntry, Outcome, RunRecord, RunResult
from liaise.notify import NOTICE_DIVERTED, NOTICE_SEND_FAILED
from liaise.outcomes import OUTCOME_SCHEMA
from liaise.processor import RECORD_FILE, STREAM_FILE, ClaudeHeadless, EchoProcessor
from liaise.subjects import BudgetPolicy, Delivery, Policy, ProcessorConfig, Subject, Workspace
from liaise.testing import FakeGitHubChannel, demo_registry
from liaise.tests._fake_claude import fake_claude
from liaise.tests.conftest import write_executable_script
from liaise.tick import (
    AUTH_PROBE_INTERVAL,
    BUDGET_MESSAGE,
    CLOSED_RECHECK_INTERVAL,
    LOST_RUN_DEADLINE,
    NUDGE_MESSAGE,
    RUN_ID_SUFFIX_DIGITS,
    STATE_READ_FAILURE_LIMIT,
    TRY_IT_MESSAGE,
    RunLockHeld,
    last_run_age,
    run_lock,
    run_lock_path,
    run_once,
    run_stamps,
    status_lines,
)
from liaise.workspace import PID_START_TOLERANCE, SharedCheckout, pid_is_alive

T0 =datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=1)
LATER = NOW + timedelta(minutes=5)
SLUG = "example-app"
REPO = "example/app"
BINDING = "github:example/app?labels=partner:pat"
ISSUE_12 = "github:example/app#12"
CASE_1 = "example-app-1"
#: How every run id ends in these tests, in place of a uuid4's hex (see fixed_run_suffix).
RUN_SUFFIX = "0a1b2c3d"


def _run_id(case_id: str, number: int) -> str:
    """The id of ``case_id``'s run number ``number``, as the tick gives it here."""
    return f"{case_id}-r{number}-{RUN_SUFFIX}"


RUN_1 = _run_id(CASE_1, 1)


@pytest.fixture(autouse=True)
def fixed_run_suffix(monkeypatch):
    """Run ids end with a uuid4's hex; here with RUN_SUFFIX, so each test knows them.

    A test that needs the real suffixes sets ``tick_module.uuid4`` back to ``uuid.uuid4``.
    """
    monkeypatch.setattr(tick_module, "uuid4", lambda: SimpleNamespace(hex=RUN_SUFFIX.ljust(32, "0")))

ASK = Outcome(kind="ask", text="Thanks for the report.", questions=("Which browser? (default: all current ones)",))
ASKED = RunResult(run_id="", outcomes=(ASK,), summary="asked")
DELIVERED = RunResult(
    run_id="", outcomes=(Outcome(kind="deliver", text="The export keeps every row now."),), summary="delivered"
)


@pytest.fixture(autouse=True)
def no_real_acquaint(monkeypatch):
    monkeypatch.setitem(sys.modules, "acquaint", None)


def _subject(
    workspace: Path, *, reply_mode="direct", delivery=None, budget=None, person="pat", login="pat"
) -> Subject:
    """The subject every test ticks: ``person``, GitHub login ``login``, is its partner."""
    return Subject(
        slug=SLUG,
        bindings=(BINDING,),
        policy=Policy(
            people={f"github:{login}": person},
            roles={person: "partner"},
            relays=("github:example-bot",),
            claim_labels={"partner:pat": person},
            default_reply_mode=reply_mode,
            budget=budget or BudgetPolicy(),
        ),
        workspace=Workspace(path=str(workspace)),
        delivery=delivery or Delivery(kind="pr_only"),
    )


class SlowProcessor(EchoProcessor):
    """An EchoProcessor whose runs keep going until cancelled, and have stopped by the next call."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.stopping: set[str] = set()

    def start(self, job):
        return replace(super().start(job), status="running", ended_at=None)

    def status(self, run, *, persist=True):
        if run.run_id in self.stopping:
            return replace(run, status="finished", ended_at=run.started_at)
        return replace(run, status="running", ended_at=None)

    def cancel(self, run, *, mode="graceful"):
        current = self.status(run)
        if current.status == "running":
            self.cancels.append((run.run_id, mode))
            self.stopping.add(run.run_id)
        return current

    def collect(self, run, *, timed_out=False, persist=True):
        error = "timed_out" if timed_out else "crashed"  # a stopped claude run ends with no result
        return RunResult(run_id=run.run_id, error=error, session_id=run.session_id)


@dataclass
class World:
    """One subject over fakes, and the calls that tick it."""

    tmp_path: Path
    workspace: Path
    sessions: Path
    subject: Subject
    github: FakeGitHubChannel = field(default_factory=FakeGitHubChannel)
    labeler: FakeGitHub = field(default_factory=FakeGitHub)
    processor: EchoProcessor = field(default_factory=lambda: EchoProcessor(results={CASE_1: ASKED}))
    store: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    #: The partner the subject knows (their person id and GitHub login), and their issues' title.
    person: str = "pat"
    login: str = "pat"
    issue_title: str = "Export"

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.store)

    @property
    def config(self) -> GlobalConfig:
        return GlobalConfig(owner_login="owner", state_dir=str(self.tmp_path / "state"))

    def notify(self, title, body, *, priority="default"):
        self.notes.append((title, body, priority))
        return True

    def tick(self, now=NOW, **kwargs):
        options = dict(
            global_config=self.config,
            registry=demo_registry(github=self.github),
            processor=self.processor,
            labeler=self.labeler,
            notify_fn=self.notify,
            sessions_dir=self.sessions,
            now=now,
        )
        options.update(kwargs)
        return run_once({SLUG: self.subject}, self.store, **options)

    def issue(self, number=12, *, author=None, minutes=0, labels=("partner:pat",)):
        author = self.login if author is None else author
        created = T0 + timedelta(minutes=minutes)
        body = "The export drops the last row."
        title = self.issue_title
        self.github.add_issue(REPO, number, author=author, title=title, body=body, labels=labels, created_at=created)
        self.labeler.seed(Issue(REPO, number, title, author, body, created, created, "open", labels=tuple(labels)))

    def labels(self, number=12):
        return self.labeler.get_issue(REPO, number).labels

    def case(self, case_id=CASE_1):
        return self.ledger.get_case(case_id)

    def titles(self):
        return [title for title, _, _ in self.notes]


@pytest.fixture
def world(tmp_path) -> World:
    workspace = tmp_path / "code" / "example-app"
    workspace.mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    return World(tmp_path=tmp_path, workspace=workspace, sessions=sessions, subject=_subject(workspace))


def _collected_error(case, run_id=RUN_1):
    (entry,) = [
        e for e in case.entries if e.kind == "run" and e.detail.get("event") == "collected" and e.detail["run_id"] == run_id
    ]
    return entry.detail["error"]


# ---- end to end ----


def test_two_ticks_take_an_issue_to_a_run_and_its_question_to_the_partner(world):
    world.issue()
    first = world.tick()

    assert first.problems == ()
    assert first.dispatched == (RUN_1,)
    assert world.case().state == "working"
    (job,) = world.processor.jobs
    assert (job.run_id, job.case_id, job.session_id) == (RUN_1, CASE_1, None)
    assert (job.cwd, job.json_schema) == (str(world.workspace.resolve()), OUTCOME_SCHEMA)
    assert "liaise:working" in world.labels()
    assert world.ledger.daily_count(SLUG, NOW.date()) == 1
    assert world.ledger.get_run(RUN_1).status == "running"  # until the tick collects it

    second = world.tick(LATER)

    assert second.problems == ()
    assert second.collected == (RUN_1,)
    ((ref, draft),) = world.github.sent
    assert ref.encoded == ISSUE_12
    assert draft.text.startswith("@pat Thanks for the report.")
    assert "1. Which browser? (default: all current ones)" in draft.text
    assert [s.text for s in second.sent] == [draft.text]
    case = world.case()
    assert case.state == "needs-partner"
    assert case.session_id == f"echo-session-{RUN_1}"
    assert [e.detail["kind"] for e in case.entries if e.kind == "outcome"] == ["ask"]
    (gate,) = [e for e in case.entries if e.kind == "gate"]
    assert (gate.detail["decision"], gate.text) == ("send", draft.text)
    assert "liaise:needs-partner" in world.labels() and "liaise:working" not in world.labels()
    assert world.ledger.get_run(RUN_1).status == "finished"
    assert SharedCheckout(world.workspace, lock_dir=world.tmp_path / "state" / "locks").holder() is None
    assert world.notes == []

    # 0.0.x H-3: an unanswered question is not dispatched again
    third = world.tick(LATER + timedelta(hours=1))
    assert third.dispatched == ()
    assert f"  case {CASE_1} (needs-partner): awaiting the partner's reply since the last run" in third.plan_lines


def test_in_draft_mode_the_question_is_stored_as_a_draft_and_the_operator_told(world):
    world.subject = _subject(world.workspace, reply_mode="draft")
    world.issue()
    world.tick()
    report = world.tick(LATER)

    assert world.github.sent == []
    case = world.case()
    (draft,) = case.drafts
    assert (draft["outcome"], draft["reason"], draft["ref"], draft["recipient"]) == (
        "ask",
        "draft reply mode",
        ISSUE_12,
        "pat",
    )
    assert case.state == "needs-partner"
    (diversion,) = report.diverted
    assert diversion.reason == "draft reply mode"
    assert any(CASE_1 in title for title in world.titles())


def test_a_dry_run_plans_every_step_and_changes_nothing(world):
    world.issue()
    world.tick()
    world.issue(13, minutes=1)
    snapshot = copy.deepcopy(world.store)
    labels = (world.labels(12), world.labels(13))
    lock = run_lock_path(world.config.state_dir)
    lock_before = lock.read_bytes()  # kept by the real tick: a lock file is never removed (S8 #4)

    report = world.tick(LATER + timedelta(minutes=10), dry_run=True)

    lines = report.plan_lines
    assert report.dry_run and report.problems == ()
    assert "intake example-app: 1 new event [dry run]" in lines
    assert "  case example-app-2: created, state intake, reporter pat" in lines
    assert f"  run {RUN_1} ({CASE_1}): collected, no error" in lines
    assert any(line.startswith(f"  gate ask to {ISSUE_12}: would send: @pat Thanks") for line in lines)
    (planned,) = [line for line in lines if line.startswith("  case example-app-2 (intake): ready")]
    checks = ("no hold", "pat may request_work", "within budget", "issue open", "preflight skipped (dry run)")
    for check in (*checks, "workspace free"):
        assert check in planned
    run_2 = _run_id("example-app-2", 1)
    assert f"  would dispatch example-app-2 as run {run_2} (fresh)" in lines
    assert any("would label github:example/app#13 liaise:working" in line for line in lines)
    assert report.dispatched == (run_2,)

    assert world.store == snapshot
    assert world.github.sent == []
    assert (len(world.processor.jobs), len(world.processor.preflights)) == (1, 1)  # the real tick's only
    assert (world.labels(12), world.labels(13)) == labels
    assert world.notes == []
    assert lock.read_bytes() == lock_before


# ---- #24: a processor that raises ----


def test_a_start_that_raises_is_a_crash_for_the_owner(world):
    class StartRaises(EchoProcessor):
        def start(self, job):
            raise RuntimeError("the spawn exploded")

    world.processor = StartRaises()
    world.issue()
    report = world.tick()

    assert report.dispatched == ()
    assert world.case().state == "needs-owner"
    assert any("the spawn exploded" in problem for problem in report.problems)
    assert any("crashed" in title for title in world.titles())
    assert world.ledger.daily_count(SLUG, NOW.date()) == 0
    assert list(world.ledger.runs()) == []
    assert SharedCheckout(world.workspace, lock_dir=world.tmp_path / "state" / "locks").holder() is None


@pytest.mark.parametrize("verb", ["status", "collect"])
def test_a_processor_verb_that_raises_collects_the_run_as_crashed(world, verb):
    world.issue()
    world.tick()

    def explode(*args, **kwargs):
        raise OSError(f"{verb} exploded")

    setattr(world.processor, verb, explode)
    report = world.tick(LATER)

    assert report.collected == (RUN_1,)
    case = world.case()
    assert (case.state, _collected_error(case)) == ("needs-owner", "crashed")
    assert world.github.sent == []
    assert any(f"{verb} exploded" in problem for problem in report.problems)
    assert any("crashed" in title for title in world.titles())
    assert world.ledger.get_run(RUN_1).status == "finished"


# ---- error classes ----


def test_auth_expired_holds_the_processor_until_preflight_passes_again(world):
    world.processor = EchoProcessor(results={CASE_1: RunResult(run_id="", error="auth_expired")})
    world.issue()
    world.tick()
    assert world.ledger.daily_count(SLUG, NOW.date()) == 1

    world.tick(LATER)

    case = world.case()
    assert case.state == "intake"
    assert world.ledger.get_hold("processor").set_by == "auto:auth_expired"
    assert any("auth_expired" in title for title in world.titles())
    assert world.ledger.daily_count(SLUG, NOW.date()) == 0  # not counted against the cap
    assert world.github.sent == []

    world.processor = EchoProcessor(health=Health(ok=False, error="auth_expired"))
    notified = len(world.notes)
    held = world.tick(LATER + timedelta(minutes=2))
    assert held.dispatched == ()
    assert world.processor.preflights == []  # not probed before AUTH_PROBE_INTERVAL
    still = world.tick(LATER + AUTH_PROBE_INTERVAL)
    assert still.dispatched == ()
    assert len(world.processor.preflights) == 1  # probed, and still failing
    assert world.ledger.get_hold("processor") is not None
    assert len(world.notes) == notified  # the operator heard once

    world.processor = EchoProcessor()
    recovered = world.tick(LATER + AUTH_PROBE_INTERVAL + timedelta(minutes=2))
    assert world.ledger.get_hold("processor") is None
    assert recovered.dispatched == (_run_id(CASE_1, 2),)
    (job,) = world.processor.jobs
    assert job.session_id == f"echo-session-{RUN_1}"  # a resume


# ---- holds ----


def test_a_block_hold_on_the_subject_starts_nothing(world):
    world.issue()
    hold(world.ledger, f"subject:{SLUG}", mode="block", reason="partner away", now=T0)
    report = world.tick()

    assert report.dispatched == ()
    assert world.processor.jobs == []
    assert world.case().state == "intake"
    assert f"  case {CASE_1} (intake): held by subject:{SLUG} (block) partner away" in report.plan_lines


def test_a_block_hold_keeps_a_finished_runs_messages_as_drafts(world):
    world.issue()
    world.tick()
    hold(world.ledger, f"subject:{SLUG}", mode="block", now=NOW)
    report = world.tick(LATER)

    assert report.collected == (RUN_1,)
    assert world.github.sent == []
    case = world.case()
    (draft,) = case.drafts
    assert draft["reason"] == f"held: subject:{SLUG}"
    assert case.state == "needs-owner"
    assert any("held" in title for title in world.titles())


def test_a_drain_hold_lets_a_running_runs_effects_execute_and_starts_nothing_new(world):
    world.issue()
    world.tick()
    hold(world.ledger, f"subject:{SLUG}", mode="drain", now=NOW)
    world.issue(13, minutes=1)
    report = world.tick(LATER + timedelta(minutes=10))

    assert report.collected == (RUN_1,)
    assert len(world.github.sent) == 1
    assert world.case().state == "needs-partner"
    assert report.dispatched == ()


def test_a_cancel_hold_cancels_the_running_run_gracefully_and_keeps_its_case_resumable(world):
    world.processor = SlowProcessor()
    world.issue()
    world.tick()
    going = world.tick(NOW + timedelta(minutes=2))
    assert going.collected == ()
    assert any(line.startswith(f"  run {RUN_1} ({CASE_1}): running, heartbeat") for line in going.plan_lines)

    hold(world.ledger, f"subject:{SLUG}", mode="cancel", now=NOW)
    cancelling = world.tick(NOW + timedelta(minutes=4))
    assert world.processor.cancels == [(RUN_1, "graceful")]
    assert cancelling.collected == ()

    stopped = world.tick(NOW + timedelta(minutes=6))
    assert stopped.collected == (RUN_1,)
    assert world.processor.cancels == [(RUN_1, "graceful")]
    assert world.case().state == "intake"
    assert world.github.sent == []
    assert stopped.dispatched == ()


# ---- workspace, budget, wall clock ----


def test_a_live_session_in_the_checkout_defers_the_case(world):
    inside = world.workspace / "src"
    inside.mkdir()
    record = {"pid": os.getpid(), "sessionId": "s-1", "name": "fixing-the-footer", "cwd": str(inside)}
    (world.sessions / "1.json").write_text(json.dumps(record))
    world.issue()
    report = world.tick()

    assert report.dispatched == ()
    assert world.processor.jobs == []
    case = world.case()
    assert (case.state, case.defer_until) == ("intake", NOW + timedelta(minutes=10))
    assert any("workspace_conflict: live session fixing-the-footer" in line for line in report.plan_lines)
    assert world.tick(NOW + timedelta(minutes=5)).dispatched == ()  # still deferred


def test_the_daily_cap_moves_the_case_to_budget_and_tells_the_partner_once(world):
    world.subject = _subject(world.workspace, budget=BudgetPolicy(daily_dispatches=1))
    world.ledger.increment_daily(SLUG, NOW.date())
    world.issue()
    report = world.tick()

    assert report.dispatched == ()
    assert world.case().state == "budget"
    ((_, draft),) = world.github.sent
    assert draft.text == f"@pat {BUDGET_MESSAGE}"

    again = world.tick(LATER)
    assert again.dispatched == ()
    assert len(world.github.sent) == 1
    assert world.case().state == "budget"


def test_the_daily_cap_tells_the_operator_once_per_subject_per_day(world):
    """0.0.x M-8: a cap holding work back is one notification a day, not one per capped case."""
    world.subject = _subject(world.workspace, budget=BudgetPolicy(daily_dispatches=1))
    world.ledger.increment_daily(SLUG, NOW.date())
    world.issue()
    world.tick()
    assert world.case().state == "budget"
    assert _titled(world, "reached its daily cap") == 1

    world.issue(13, minutes=1)
    world.tick(LATER)
    assert world.case("example-app-2").state == "budget"  # a second capped case, the same day
    assert _titled(world, "reached its daily cap") == 1

    tomorrow = NOW + timedelta(days=1)
    world.ledger.increment_daily(SLUG, tomorrow.date())  # the next day's cap, used up already
    world.tick(tomorrow)
    world.tick(tomorrow + timedelta(minutes=5))
    assert _titled(world, "reached its daily cap") == 2
    (title,) = {title for title in world.titles() if "daily cap" in title}
    assert title == f"liaise: {SLUG} reached its daily cap"


def test_a_dry_run_says_it_would_tell_the_operator_of_the_cap_and_remembers_nothing(world):
    world.subject = _subject(world.workspace, budget=BudgetPolicy(daily_dispatches=1))
    world.ledger.increment_daily(SLUG, NOW.date())
    world.issue()
    report = world.tick(dry_run=True)

    assert f"  would notify the operator: liaise: {SLUG} reached its daily cap" in report.plan_lines
    assert world.notes == []
    assert not world.ledger.daily_cap_notified(SLUG, NOW.date())


def test_a_run_past_its_wall_clock_is_cancelled_now_then_collected_as_timed_out(world):
    world.subject = _subject(world.workspace, budget=BudgetPolicy(timeout_minutes=30))
    world.processor = SlowProcessor()
    world.issue()
    world.tick()

    late = world.tick(NOW + timedelta(minutes=31))
    assert world.processor.cancels == [(RUN_1, "now")]
    assert late.collected == ()

    done = world.tick(NOW + timedelta(minutes=33))
    assert done.collected == (RUN_1,)
    case = world.case()
    assert (case.state, _collected_error(case)) == ("needs-owner", "timed_out")
    assert world.github.sent == []
    assert any("timed_out" in title for title in world.titles())


# ---- batch delivery ----


def test_a_batch_deploy_that_succeeds_marks_the_case_deployed_and_says_try_it(world, tmp_path):
    script = write_executable_script(tmp_path / "deploy", "print('deployed')\n")
    delivery = Delivery(kind="deploy", per="batch", command=script.as_posix())
    world.subject = _subject(world.workspace, delivery=delivery)
    world.processor = EchoProcessor(results={CASE_1: DELIVERED})
    world.issue()
    world.tick()
    report = world.tick(LATER)

    assert report.problems == ()
    ((_, draft),) = world.github.sent
    assert draft.text == f"@pat The export keeps every row now.\n\n{TRY_IT_MESSAGE}"
    assert world.case().state == "deployed"
    assert "liaise:deployed" in world.labels()
    assert any(line.startswith(f"deploy {SLUG}: ran ") for line in report.plan_lines)


def test_a_batch_deploy_refused_for_billing_holds_deploys_and_needs_the_owner(world, tmp_path):
    body = "import sys\nprint('Error: the Actions spending limit has been reached')\nsys.exit(1)\n"
    script = write_executable_script(tmp_path / "deploy", body)
    delivery = Delivery(kind="deploy", per="batch", command=script.as_posix())
    world.subject = _subject(world.workspace, delivery=delivery)
    world.processor = EchoProcessor(results={CASE_1: DELIVERED})
    world.issue()
    world.tick()
    world.tick(LATER)

    assert world.github.sent == []
    assert world.case().state == "needs-owner"
    assert world.ledger.get_hold("effect:deploy").set_by == "auto:effect_blocked"
    assert any("did not deploy" in title for title in world.titles())


# ---- access and selection ----


def test_a_claim_label_from_an_untrusted_author_opens_no_case_and_starts_nothing(world):
    world.issue(14, author="someone-else")
    report = world.tick()

    assert list(world.ledger.cases()) == []
    (item,) = world.ledger.unrouted()
    assert item["reason"] == "label claim by an untrusted author"
    assert report.dispatched == ()
    assert world.processor.jobs == []


def test_only_an_unknown_subject_is_a_config_error(world):
    with pytest.raises(ConfigError, match="no subject 'elsewhere'"):
        world.tick(only="elsewhere")


# ---- status ----


def test_status_lines_show_stamps_holds_runs_cases_unrouted_drafts_and_notes(world):
    world.subject = _subject(world.workspace, reply_mode="draft")
    note = Outcome(kind="note", text="The export code has no tests.")
    world.processor = EchoProcessor(results={CASE_1: RunResult(run_id="", outcomes=(ASK, note))})
    world.issue()
    world.issue(14, author="someone-else", minutes=2)
    world.tick()
    world.tick(LATER)
    hold(world.ledger, f"repo:{REPO}", mode="drain", reason="release freeze", now=LATER)
    world.ledger.save_run(
        RunRecord(
            run_id="example-app-9-r1",
            case_id="example-app-9",
            subject=SLUG,
            mode="fresh",
            status="running",
            started_at=LATER,
            heartbeat_at=LATER,
        )
    )

    lines = status_lines({SLUG: world.subject}, world.store, global_config=world.config, now=LATER + timedelta(seconds=90))

    assert lines[0] == "last_run: finished"
    assert "holds: 1" in lines
    assert any(line.startswith("  repo:example/app: drain, set by operator") and "release freeze" in line for line in lines)
    assert "runs in flight: 1" in lines
    assert "  example-app-9-r1 (example-app-9, fresh): started 1m ago, heartbeat 1m ago" in lines
    assert "subject example-app: 1 case(s), 1/6 dispatches today" in lines
    assert "  needs-partner: example-app-1" in lines
    assert "unrouted: 1" in lines
    assert any("label claim by an untrusted author" in line for line in lines)
    assert "drafts waiting for the operator: 1" in lines
    assert f"  {CASE_1} ask to {ISSUE_12}: draft reply mode" in lines
    assert "digest notes: 1" in lines
    assert f"  {CASE_1}: The export code has no tests." in lines
    (note,) = [e for e in world.case().entries if e.kind == "note"]
    assert note.text == "The export code has no tests."


# ---- helpers for the tests below ----

#: A pid no process has: in range, so the liveness check asks the system, which says no.
DEAD_PID = 999999999


def _titled(world, text) -> int:
    """How many operator notifications carry ``text`` in their title."""
    return sum(text in title for title in world.titles())


def _lock(world, pid) -> Path:
    lock = run_lock_path(world.config.state_dir)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(pid))
    return lock


def _seed_running_case(world, number, *, started_at=NOW) -> str:
    """Case ``example-app-2``, on issue ``number``, working on a run no tick has collected.

    A shared checkout runs one case at a time, so two runs that finish in one tick come
    from the ledger, not from two starts. Returns the run's id.
    """
    world.issue(number)
    ledger = world.ledger
    case = ledger.new_case(SLUG, f"github:{REPO}#{number}", reporter="pat", at=T0)
    message = LedgerEntry(at=T0, kind="message", actor="pat", grade="platform", permission="report", text="And the CSV.")
    ledger.append(case.id, message)
    ledger.transition(case.id, "working", at=started_at, actor="liaise", reason="seeded")
    run_id = f"{case.id}-r1"
    started = {"event": "started", "run_id": run_id, "mode": "fresh", "day": started_at.date().isoformat()}
    ledger.append(case.id, LedgerEntry(at=started_at, kind="run", actor="liaise", detail=started))
    ledger.save_run(
        RunRecord(run_id=run_id, case_id=case.id, subject=SLUG, mode="fresh", status="running", started_at=started_at)
    )
    ledger.increment_daily(SLUG, started_at.date())
    return run_id


# ---- an expired login: one notification per probe interval, not one per tick ----


def test_an_expired_login_preflight_cannot_see_notifies_once_per_probe_interval(world):
    world.processor = EchoProcessor(default=RunResult(run_id="", error="auth_expired"))
    world.issue()
    world.tick()
    world.tick(LATER)  # r1 finds the login expired: the processor is held, the operator told
    assert _titled(world, "auth_expired") == 1

    for minutes in range(2, 30, 4):
        assert world.tick(LATER + timedelta(minutes=minutes)).dispatched == ()
    assert (len(world.processor.jobs), len(world.processor.preflights)) == (1, 1)
    assert _titled(world, "auth_expired") == 1

    probe = world.tick(LATER + AUTH_PROBE_INTERVAL)  # preflight passes, so the hold is lifted
    assert probe.dispatched == (_run_id(CASE_1, 2),)
    world.tick(LATER + AUTH_PROBE_INTERVAL + timedelta(minutes=2))  # and r2 finds it expired
    assert world.ledger.get_hold("processor").set_by == "auto:auth_expired"
    assert _titled(world, "auth_expired") == 2


def test_runs_that_find_the_login_expired_in_one_tick_notify_once(world):
    world.processor = EchoProcessor(default=RunResult(run_id="", error="auth_expired"))
    world.issue()
    world.tick()
    second = _seed_running_case(world, 13)
    report = world.tick(LATER)

    assert report.collected == (RUN_1, second)
    assert _titled(world, "auth_expired") == 1
    (placed,) = world.ledger.holds()
    assert placed.set_by == "auto:auth_expired"
    assert world.ledger.daily_count(SLUG, NOW.date()) == 0  # neither run counts


# ---- a dry run writes no file ----


def test_a_dry_run_leaves_a_running_claude_runs_record_json_untouched(world):
    claude = fake_claude(world.tmp_path / "claude", scenario="hang", sleep_s=60)
    processor = ClaudeHeadless(claude_bin=claude, runs_dir=world.tmp_path / "state" / "runs")
    world.processor = processor
    world.issue()
    assert world.tick().dispatched == (RUN_1,)  # a real tick spawns the run
    run = world.ledger.get_run(RUN_1)
    try:
        record = processor.run_dir(RUN_1) / RECORD_FILE
        before = (record.read_bytes(), record.stat().st_mtime_ns)
        snapshot = copy.deepcopy(world.store)

        report = world.tick(LATER, dry_run=True)

        assert any(line.startswith(f"  run {RUN_1} ({CASE_1}): running, heartbeat") for line in report.plan_lines)
        assert (record.read_bytes(), record.stat().st_mtime_ns) == before
        assert world.store == snapshot
    finally:
        processor.cancel(run, mode="now")
        deadline = time.monotonic() + 10
        while processor.status(run).status != "finished":
            assert time.monotonic() < deadline, "the fake claude run did not stop"
            time.sleep(0.05)


# ---- 0.0.x behaviours the retired run.py and dispatch.py tests guarded ----


def test_a_case_capped_yesterday_is_started_the_next_day(world):
    """0.0.x H-4: `budget` is a state the tick starts cases from, since the cap is per day."""
    world.subject = _subject(world.workspace, budget=BudgetPolicy(daily_dispatches=1))
    world.ledger.increment_daily(SLUG, NOW.date())
    world.issue()
    world.tick()
    assert world.case().state == "budget"

    tomorrow = world.tick(NOW + timedelta(days=1))
    assert tomorrow.dispatched == (RUN_1,)
    assert world.case().state == "working"
    assert len(world.github.sent) == 1  # the budget message went out once, yesterday


def test_a_partner_reply_after_the_question_starts_a_resumed_run(world):
    """0.0.x H-3, the other half: once the partner answers, the case starts again, resumed."""
    world.issue()
    world.tick()
    world.tick(LATER)  # the question goes out, and the case waits on the partner
    world.github.add_comment(REPO, 12, author="pat", body="All current ones.", created_at=LATER + timedelta(minutes=1))

    assert world.tick(LATER + timedelta(minutes=5)).dispatched == ()  # still inside the quiet window
    ready = world.tick(LATER + timedelta(minutes=12))
    assert ready.dispatched == (_run_id(CASE_1, 2),)
    _, resumed = world.processor.jobs
    assert resumed.session_id == f"echo-session-{RUN_1}"


def test_fresh_and_resumed_runs_carry_the_subjects_permission_mode(world):
    """0.0.x #22: a resumed run runs under the permission mode of the fresh one."""
    world.subject = replace(world.subject, processor=ProcessorConfig(permission_mode="acceptEdits"))
    world.processor = EchoProcessor(results={CASE_1: RunResult(run_id="", error="unavailable")})
    world.issue()
    world.tick()
    world.tick(LATER)  # unavailable: back to intake for five minutes, the session kept
    world.tick(LATER + timedelta(minutes=6))

    fresh, resumed = world.processor.jobs
    assert resumed.session_id == f"echo-session-{RUN_1}"
    assert fresh.permission_mode == resumed.permission_mode == "acceptEdits"


def test_a_brief_that_cannot_be_read_fails_the_start_and_leaves_no_run(world):
    """0.0.x: a dispatch that failed before running left no log and no `working` label. Here
    the prompt cannot be composed, so nothing starts, nothing counts, and the owner hears."""
    world.subject = replace(world.subject, brief=str(world.tmp_path / "missing-brief.md"))
    world.issue()
    report = world.tick()

    assert (report.dispatched, world.processor.jobs) == ((), [])
    assert world.case().state == "needs-owner"
    assert any("cannot read the brief" in problem for problem in report.problems)
    assert list(world.ledger.runs()) == []
    assert world.ledger.daily_count(SLUG, NOW.date()) == 0
    assert _titled(world, "crashed") == 1


def test_a_batch_deploy_runs_its_command_once_for_every_case_it_delivers(world, tmp_path):
    """0.0.x A.5 batching: two deliveries collected in one tick share one deploy."""
    calls = tmp_path / "deploys.txt"
    script = write_executable_script(tmp_path / "deploy", f"with open({str(calls)!r}, 'a') as f:\n    f.write('deploy\\n')\n")
    world.subject = _subject(world.workspace, delivery=Delivery(kind="deploy", per="batch", command=script.as_posix()))
    world.processor = EchoProcessor(default=DELIVERED)
    world.issue()
    world.tick()
    second = _seed_running_case(world, 13)
    report = world.tick(LATER)

    assert report.collected == (RUN_1, second)
    assert calls.read_text().splitlines() == ["deploy"]
    assert {world.case(CASE_1).state, world.case("example-app-2").state} == {"deployed"}
    assert sorted(ref.encoded for ref, _ in world.github.sent) == [ISSUE_12, f"github:{REPO}#13"]


def test_a_batch_delivery_with_no_deploy_command_needs_the_owner_and_tells_the_partner_nothing(world):
    """0.0.x M-2: the default delivery, a batch deploy, with no command must not strand the case."""
    world.subject = _subject(world.workspace, delivery=Delivery(kind="deploy", per="batch", command=""))
    world.processor = EchoProcessor(results={CASE_1: DELIVERED})
    world.issue()
    world.tick()
    report = world.tick(LATER)

    assert world.github.sent == []
    assert world.case().state == "needs-owner"
    assert any("no deploy command is configured" in line for line in report.plan_lines)
    assert _titled(world, "did not deploy") == 1


def test_a_quiet_deployed_case_is_nudged_once(world):
    """0.0.x M-10: a deployed case its partner has gone quiet on gets one nudge, then no more."""
    world.processor = EchoProcessor(results={CASE_1: DELIVERED})
    world.issue()
    world.tick()
    world.tick(LATER)
    assert world.case().state == "deployed"
    sent = len(world.github.sent)

    world.tick(LATER + timedelta(days=2))  # not quiet long enough yet
    assert len(world.github.sent) == sent
    world.tick(LATER + timedelta(days=4))
    assert len(world.github.sent) == sent + 1
    assert world.github.sent[-1][1].text == f"@pat {NUDGE_MESSAGE}"
    world.tick(LATER + timedelta(days=5))
    assert len(world.github.sent) == sent + 1


# ---- the run lock and the run stamps (ported from 0.0.x test_run) ----


def test_status_says_running_during_a_tick_and_finished_after_it(world):
    """0.0.x #22: a tick stamps its start when it begins and its end when it stops, so status
    reads `running` while one is in progress, not a job that has not run for a while."""
    previous_end = T0 + timedelta(minutes=1)
    world.store.update(run_started_at=T0.isoformat(), run_ended_at=previous_end.isoformat())
    seen = []

    class Watching(EchoProcessor):
        def preflight(self, job):
            seen.append(status_lines({SLUG: world.subject}, world.store, global_config=world.config, now=NOW))
            return super().preflight(job)

    world.processor = Watching()
    world.issue()
    world.tick()

    (during,) = seen
    assert during[0] == "last_run: running"
    assert during[1] == f"  run_started_at: {NOW.isoformat(timespec='seconds')} (0s ago)"
    assert during[2].startswith(f"  run_ended_at:   {previous_end.isoformat(timespec='seconds')}")
    after = status_lines({SLUG: world.subject}, world.store, global_config=world.config, now=NOW)
    assert after[0] == "last_run: finished"
    assert run_stamps(world.store).ended_at >= NOW


def test_status_says_interrupted_when_the_ticks_process_is_gone(world):
    """0.0.x #22: a tick killed before it stamped its end, its lock held by no live process,
    is not `running`."""
    _lock(world, DEAD_PID)
    world.store.update(run_started_at=(NOW + timedelta(minutes=5)).isoformat(), run_ended_at=NOW.isoformat())
    lines = status_lines({SLUG: world.subject}, world.store, global_config=world.config, now=LATER)
    assert lines[0] == "last_run: interrupted (no live liaise process holds the run lock)"


def test_run_stamps_read_a_legacy_last_run_as_a_finished_run():
    """Stores written by 0.0.3 and earlier hold only `last_run`: a pass's start, stamped once
    it had finished."""
    stamps = run_stamps({"last_run": T0.isoformat()})
    assert stamps.started_at == stamps.ended_at == T0
    assert stamps.state == "finished"
    assert last_run_age({}) is None
    assert last_run_age({"last_run": T0.isoformat()}, now=T0 + timedelta(seconds=90)) == pytest.approx(90)


def test_a_tick_refuses_to_start_while_a_live_process_holds_the_run_lock(world):
    """0.0.x L-3, S8 #4: one tick at a time. The run lock is an OS lock on the lock file, which
    refuses a second descriptor of it, this process's own included."""
    lock = run_lock_path(world.config.state_dir)
    world.issue()
    with run_lock(lock):
        with pytest.raises(RunLockHeld, match=rf"another liaise tick \(pid {os.getpid()}\) is already in progress"):
            world.tick()
        assert (world.processor.jobs, world.store) == ([], {})
        assert lock.read_text() == str(os.getpid())
    assert world.tick().dispatched == (RUN_1,)  # free once it is released


def test_a_run_lock_left_by_a_dead_process_is_reclaimed(world):
    """S8 #4: a lock file holding a dead pid, with no OS lock on it, stops nothing. The file is
    never removed: it is emptied when the tick ends."""
    lock = _lock(world, DEAD_PID)
    world.issue()
    assert world.tick().dispatched == (RUN_1,)
    assert lock.exists() and lock.read_text() == ""


def test_a_dry_run_neither_takes_nor_minds_the_run_lock(world):
    lock = run_lock_path(world.config.state_dir)
    world.issue()
    with run_lock(lock):
        assert world.tick(dry_run=True).dispatched == (RUN_1,)  # a held lock does not stop it
        assert lock.read_text() == str(os.getpid())


def test_a_tick_that_raises_still_stamps_its_end_and_releases_the_lock(world, monkeypatch):
    """A tick that dies (its store unreachable, say) must not read as still running."""

    def unreachable(self):
        raise RuntimeError("the store is unreachable")

    monkeypatch.setattr(tick_module._Tick, "reconcile", unreachable)
    with pytest.raises(RuntimeError, match="the store is unreachable"):
        world.tick()
    lock_path = run_lock_path(world.config.state_dir)
    assert run_stamps(world.store).started_at == NOW
    assert run_stamps(world.store, lock_path=lock_path).state == "finished"
    with run_lock(lock_path):  # released: it can be taken again
        pass


# ---- S7: the fixes from the adversarial review ----

#: The tick's own uuid4, before fixed_run_suffix replaces it in each test.
REAL_UUID4 = tick_module.uuid4
#: A token-shaped string, built by concatenation, that must never reach a notification.
TOKEN_SHAPED = "ghp_" + "c" * 36


def _closed_marks(case) -> list:
    return [e.detail for e in case.entries if e.kind == "run" and "closed" in e.detail]


class StubbornProcessor(SlowProcessor):
    """A SlowProcessor whose runs ignore every cancel, so they never stop."""

    def cancel(self, run, *, mode="graceful"):
        self.cancels.append((run.run_id, mode))
        return replace(run, status="running", ended_at=None)


def test_a_working_case_with_no_run_in_flight_goes_to_the_owner_once(world):
    """#1: a case left working with no run in flight (a tick that died after collecting its
    run) is handed to the owner, who hears it once."""
    world.issue()
    world.tick()
    world.ledger.save_run(replace(world.ledger.get_run(RUN_1), status="finished"))

    world.tick(LATER)

    case = world.case()
    assert case.state == "needs-owner"
    (lost,) = [e for e in case.entries if e.kind == "run" and e.detail.get("event") == "lost"]
    assert lost.at == LATER
    ((_, body, _),) = [note for note in world.notes if "run lost" in note[0]]
    assert f"see liaise case show {CASE_1}" in body
    assert "liaise:needs-owner" in world.labels()
    world.tick(LATER + timedelta(minutes=5))
    assert _titled(world, "run lost") == 1


def test_an_issue_adopted_with_a_working_label_goes_to_the_owner(world):
    """#1: an issue 0.0.x left at liaise:working has no run liaise can collect."""
    world.issue(labels=("partner:pat", "liaise:working"))
    report = world.tick()
    assert (world.case().state, report.dispatched) == ("needs-owner", ())
    assert _titled(world, "run lost") == 1


def test_a_checkout_release_that_raises_hands_the_case_to_the_owner_and_the_tick_goes_on(world):
    """#1: a lock that cannot be released after collection must not strand the case in
    working, nor end the tick."""

    class Unreleasable(SharedCheckout):
        def release(self, *, run_id):
            raise OSError("the lock's disk went away")

    def workspace(subject, **kwargs):
        return Unreleasable(subject.workspace.path, **kwargs)

    world.issue()
    world.tick(workspace=workspace)
    report = world.tick(LATER, workspace=workspace)

    assert report.collected == (RUN_1,)
    assert any("releasing its checkout failed" in p and "went away" in p for p in report.problems)
    case = world.case()
    assert (case.state, _collected_error(case)) == ("needs-owner", "crashed")
    assert world.github.sent == []  # its outcomes were not carried out
    assert "liaise:needs-owner" in world.labels()  # the tick went on, to the labels


def test_a_delivery_that_raises_hands_its_cases_to_the_owner_and_the_tick_goes_on(world, tmp_path, monkeypatch):
    """#1: an exception in a batch deploy is a problem line and needs-owner, never the end of
    the tick."""
    script = write_executable_script(tmp_path / "deploy", "print('deployed')\n")
    world.subject = _subject(world.workspace, delivery=Delivery(kind="deploy", per="batch", command=script.as_posix()))
    world.processor = EchoProcessor(results={CASE_1: DELIVERED})
    world.issue()
    world.tick()

    def explode(self, subject):
        raise RuntimeError("the deploy runner exploded")

    monkeypatch.setattr(tick_module._Tick, "_deploy", explode)
    report = world.tick(LATER)

    assert any("the deploy runner exploded" in problem for problem in report.problems)
    assert world.case().state == "needs-owner"
    assert world.github.sent == []
    assert _titled(world, "delivering") == 1
    assert "liaise:needs-owner" in world.labels()  # the tick went on, to the labels


def test_a_run_that_will_not_stop_is_given_up_past_the_lost_run_deadline(world):
    """#2: LOST_RUN_DEADLINE after the tick cancelled it for its wall clock, a run that ignores
    every cancel is finished as timed_out, whatever its pid says, and its case goes to the owner."""
    world.subject = _subject(world.workspace, budget=BudgetPolicy(timeout_minutes=30))
    world.processor = StubbornProcessor()
    world.issue()
    world.tick()
    past_wall_clock = NOW + timedelta(minutes=31)

    world.tick(past_wall_clock)
    still = world.tick(NOW + timedelta(minutes=30) + LOST_RUN_DEADLINE)
    assert still.collected == ()
    assert world.processor.cancels == [(RUN_1, "now"), (RUN_1, "now")]
    assert world.case().state == "working"

    given_up = world.tick(past_wall_clock + LOST_RUN_DEADLINE)
    assert given_up.collected == (RUN_1,)
    assert len(world.processor.cancels) == 2  # nothing more is sent: its pid may be another's now
    case = world.case()
    assert (case.state, _collected_error(case)) == ("needs-owner", "timed_out")
    assert world.ledger.get_run(RUN_1).status == "finished"
    assert _titled(world, "timed_out") == 1
    assert any("lost-run deadline" in line for line in given_up.plan_lines)


def test_the_lost_run_deadline_is_a_keyword_of_the_tick(world):
    world.subject = _subject(world.workspace, budget=BudgetPolicy(timeout_minutes=30))
    world.processor = StubbornProcessor()
    world.issue()
    world.tick()
    world.tick(NOW + timedelta(minutes=31))  # past its wall clock: cancelled, and the deadline starts
    assert world.tick(NOW + timedelta(minutes=32), lost_run_deadline=timedelta(minutes=1)).collected == (RUN_1,)


def test_an_issue_adopted_waiting_on_its_partner_waits_for_them_to_write_after_the_adoption(world):
    """#3: 0.0.x asked, and the partner wrote before the upgrade; the adopted needs-partner case
    starts only once they write after the adoption."""
    world.issue(labels=("partner:pat", "liaise:needs-partner"))
    world.github.add_comment(REPO, 12, author="pat", body="Chrome, mostly.", created_at=T0 + timedelta(minutes=20))

    first = world.tick()

    case = world.case()
    assert case.state == "needs-partner"
    assert [e.text for e in case.entries if e.kind == "message"] == ["The export drops the last row.", "Chrome, mostly."]
    assert first.dispatched == ()
    assert f"  case {CASE_1} (needs-partner): awaiting the partner's reply since it was adopted" in first.plan_lines
    world.github.add_comment(REPO, 12, author="pat", body="Also Firefox.", created_at=NOW + timedelta(minutes=1))
    assert world.tick(NOW + timedelta(minutes=12)).dispatched == (RUN_1,)


def test_a_per_issue_deploy_runs_right_after_each_cases_outcomes(world, tmp_path):
    """#4: per = "issue" runs the command for each case as soon as its outcomes are carried
    out, and the partner hears it is live only after it succeeded."""
    calls = tmp_path / "deploys.txt"
    body = f"with open({str(calls)!r}, 'a') as f:\n    f.write('deploy\\n')\n"
    script = write_executable_script(tmp_path / "deploy", body)
    world.subject = _subject(world.workspace, delivery=Delivery(kind="deploy", per="issue", command=script.as_posix()))
    world.processor = EchoProcessor(default=DELIVERED)
    world.issue()
    world.tick()
    second = _seed_running_case(world, 13)

    report = world.tick(LATER)

    assert report.collected == (RUN_1, second)
    assert calls.read_text().splitlines() == ["deploy", "deploy"]  # once per case, not once per tick
    assert {world.case(CASE_1).state, world.case("example-app-2").state} == {"deployed"}
    assert world.github.sent[0][1].text == f"@pat The export keeps every row now.\n\n{TRY_IT_MESSAGE}"
    deploys = [i for i, line in enumerate(report.plan_lines) if line.startswith(f"deploy {SLUG}: ran ")]
    collecting_second = report.plan_lines.index(f"  run {second} (example-app-2): collected, no error")
    assert len(deploys) == 2 and deploys[0] < collecting_second


def test_a_per_issue_delivery_with_no_deploy_command_needs_the_owner_and_says_nothing(world):
    """#4: never "it's live" without a command that ran: an empty one is needs-owner."""
    world.subject = _subject(world.workspace, delivery=Delivery(kind="deploy", per="issue", command=""))
    world.processor = EchoProcessor(results={CASE_1: DELIVERED})
    world.issue()
    world.tick()
    report = world.tick(LATER)

    assert world.github.sent == []
    assert world.case().state == "needs-owner"
    assert any("no deploy command is configured" in line for line in report.plan_lines)
    assert _titled(world, "did not deploy") == 1


def test_a_closed_issue_is_not_started_until_it_reopens(world):
    """#5: a case whose issue was closed after it was taken in starts nothing and says so
    once; reopened, it starts."""
    world.issue()
    hold(world.ledger, f"subject:{SLUG}", mode="block", now=T0)
    world.tick()  # taken in, and held
    world.ledger.clear_hold(f"subject:{SLUG}")
    world.github.set_state(REPO, 12, "closed")

    closed = world.tick(LATER)
    assert closed.dispatched == ()
    assert f"  case {CASE_1} (intake): its issue is closed" in closed.plan_lines
    assert world.tick(LATER + timedelta(minutes=5)).dispatched == ()
    assert _closed_marks(world.case()) == [{"event": "issue_closed", "closed": True}]  # once
    assert (world.notes, world.github.sent) == ([], [])

    world.github.set_state(REPO, 12, "open")
    assert world.tick(LATER + CLOSED_RECHECK_INTERVAL).dispatched == (RUN_1,)  # read again once due (S8 #3)
    assert [mark["closed"] for mark in _closed_marks(world.case())] == [True, False]


def test_a_closed_deployed_case_is_never_nudged(world):
    """#5: the partner closed the delivered issue; nobody asks them whether they tried it."""
    world.processor = EchoProcessor(results={CASE_1: DELIVERED})
    world.issue()
    world.tick()
    world.tick(LATER)
    assert world.case().state == "deployed"
    sent = len(world.github.sent)
    world.github.set_state(REPO, 12, "closed")

    world.tick(LATER + timedelta(days=4))
    world.tick(LATER + timedelta(days=5))

    assert len(world.github.sent) == sent
    assert _closed_marks(world.case()) == [{"event": "issue_closed", "closed": True}]


def test_an_issue_whose_state_cannot_be_read_starts_nothing_this_tick(world):
    """#5: not knowing whether the issue is closed is not knowing it is open."""
    world.issue()
    hold(world.ledger, f"subject:{SLUG}", mode="block", now=T0)
    world.tick()
    world.ledger.clear_hold(f"subject:{SLUG}")
    read = world.github.read

    def refuse_issues(ref, **kwargs):
        if "#" in ref.id:
            raise ChannelError("gh is not logged in", kind="auth")
        return read(ref, **kwargs)

    world.github.read = refuse_issues
    report = world.tick(LATER)
    assert report.dispatched == ()
    assert any("gh is not logged in" in problem for problem in report.problems)
    del world.github.read
    assert world.tick(LATER + timedelta(minutes=5)).dispatched == (RUN_1,)


def test_two_first_starts_of_one_case_never_share_a_run_id(world, monkeypatch):
    """#6: a start that failed counts no start, so the next is the case's first again; its run
    id still differs, by its uuid suffix, and so never names the earlier run's directory."""
    monkeypatch.setattr(tick_module, "uuid4", REAL_UUID4)

    class StartRaisesOnce(EchoProcessor):
        raised = False

        def start(self, job):
            if not self.raised:
                self.raised = True
                raise RuntimeError("the spawn exploded")
            return super().start(job)

    world.processor = StartRaisesOnce()
    world.issue()
    world.tick()  # the start raises: nothing is counted, and the case needs the owner
    (failed,) = [e.detail["run_id"] for e in world.case().entries if e.detail.get("event") == "start_failed"]
    set_case_state(world.ledger, CASE_1, "intake", now=NOW)
    (started,) = world.tick(LATER).dispatched

    first_start = rf"{CASE_1}-r1-[0-9a-f]{{{RUN_ID_SUFFIX_DIGITS}}}"
    assert re.fullmatch(first_start, failed) and re.fullmatch(first_start, started)
    assert started != failed


def test_a_diverted_message_tells_the_operator_everything_but_its_text(world):
    """#7: what the gate diverted a message for never reaches the notification."""
    leaky = Outcome(kind="reply", text="Use " + TOKEN_SHAPED + " to log in.")
    world.processor = EchoProcessor(results={CASE_1: RunResult(run_id="", outcomes=(leaky,))})
    world.issue()
    world.tick()
    world.tick(LATER)

    ((_, body, _),) = [note for note in world.notes if "waits for you" in note[0]]
    assert TOKEN_SHAPED not in body and "log in" not in body and ISSUE_12 not in body
    for part in (f"case: {CASE_1}", f"event: {NOTICE_DIVERTED}", "cause: leak_scan", f"see liaise case show {CASE_1}"):
        assert part in body


def test_a_failed_send_tells_the_operator_everything_but_its_text(world):
    """#7: nor does the text of a message the channel refused."""
    leaky = Outcome(kind="reply", text="Use " + TOKEN_SHAPED + " to log in.")
    world.processor = EchoProcessor(results={CASE_1: RunResult(run_id="", outcomes=(leaky,))})
    world.issue()
    world.tick()
    world.github.send_error = ChannelError("gh is not logged in", kind="auth")
    world.tick(LATER, outbound_filters=())

    ((_, body, _),) = [note for note in world.notes if "was not sent" in note[0]]
    assert TOKEN_SHAPED not in body and "log in" not in body and ISSUE_12 not in body
    for part in (f"case: {CASE_1}", f"event: {NOTICE_SEND_FAILED}", "cause: auth", f"see liaise case show {CASE_1}"):
        assert part in body


#: A script that holds the run lock at argv[1], says so with its own pid, and lets it go once
#: its stdin closes. Its own pid, because on Windows a venv's python.exe is a launcher that
#: runs the interpreter as a child, so the pid `Popen` reports is not the lock holder's.
_LOCK_HOLDER = """
import os, sys
from liaise.tick import run_lock
with run_lock(sys.argv[1]):
    print("held", os.getpid(), flush=True)
    sys.stdin.read()
"""
#: How long a test waits, at most, for the process holding the run lock to exit.
LOCK_HOLDER_WAIT_S = 10.0


def test_a_run_lock_another_process_holds_refuses_a_tick_until_it_lets_go(world):
    """#11, S8 #4: across processes, as a manual `liaise run` meets the scheduled one."""
    lock = run_lock_path(world.config.state_dir)
    holder = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        said, holder_pid = holder.stdout.readline().split()
        assert said == "held"
        world.issue()
        with pytest.raises(RunLockHeld, match=rf"another liaise tick \(pid {holder_pid}\)"):
            world.tick()
        assert world.processor.jobs == []
    finally:
        holder.stdin.close()
        holder.wait(timeout=LOCK_HOLDER_WAIT_S)
    assert world.tick().dispatched == (RUN_1,)


def test_an_empty_run_lock_file_that_is_locked_still_blocks(world):
    """#11, S8 #4: a lock taken before its pid is written (read between another tick's open and
    its write) is held all the same: the OS lock decides, not what the file holds."""
    lock = run_lock_path(world.config.state_dir)
    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT)
    try:
        assert tick_module._try_lock(descriptor)
        assert lock.read_text() == ""
        world.issue()
        with pytest.raises(RunLockHeld, match="another liaise tick is already in progress"):
            world.tick()
        assert world.processor.jobs == []
    finally:
        tick_module._unlock(descriptor)
        os.close(descriptor)
    assert lock.exists()


def test_a_state_the_operator_sets_reaches_the_issue_on_the_next_tick(world):
    """#12: labels are projections of the ledger: after `liaise case set-state`, the next tick
    relabels the issue, though nothing else changed the case."""
    world.issue()
    world.tick()
    world.tick(LATER)  # the question goes out: needs-partner
    world.tick(LATER + timedelta(minutes=2))  # hears its own comment; the case waits on pat
    assert "liaise:needs-partner" in world.labels()

    set_case_state(world.ledger, CASE_1, "needs-owner", reason="taking this one by hand", now=LATER)
    report = world.tick(LATER + timedelta(minutes=4))

    assert world.case().state == "needs-owner"
    assert "liaise:needs-owner" in world.labels() and "liaise:needs-partner" not in world.labels()
    assert f"  labelled {ISSUE_12} liaise:needs-owner" in report.plan_lines


def test_a_triage_orders_the_ready_cases_and_the_tick_starts_them_in_that_order(world):
    """#13: the triage seam gets the ready cases in the tick's order, and its order wins."""
    world.issue()
    world.issue(13, minutes=1)
    seen = []

    def newest_first(cases):
        seen.append([case.id for case in cases])
        return [[case] for case in reversed(cases)]

    report = world.tick(triage=newest_first)

    assert seen == [[CASE_1, "example-app-2"]]
    assert report.dispatched == (_run_id("example-app-2", 1),)  # the concurrent cap starts one
    assert f"  triage {SLUG}: example-app-2, {CASE_1}" in report.plan_lines


# ---- S8: the fixes from the adversarial review of the S7 fixes ----

#: How long a test waits, at most, for a fake claude run to stop, and how often it looks.
STOP_WAIT_S = 10.0
STOP_POLL_S = 0.05
#: How long a hung fake claude sleeps: longer than any test waits for it.
HANG_S = 60.0
#: How a token-shaped poison starts, and how long it runs after that.
POISON_PREFIX = "ghp_"
POISON_TAIL = 36


def _poison(tag: str) -> str:
    """A token-shaped string naming where it was planted, built by concatenation."""
    return POISON_PREFIX + tag + "0" * (POISON_TAIL - len(tag))


def _wait_stopped(processor, run) -> None:
    deadline = time.monotonic() + STOP_WAIT_S
    while processor.status(run, persist=False).status != "finished":
        assert time.monotonic() < deadline, f"run {run.run_id} did not stop"
        time.sleep(STOP_POLL_S)


def _stop_runs(world, *, processor=None) -> None:
    """Stop every run a ClaudeHeadless started, so no fake claude outlives the test.

    ``processor`` is the one that spawned them, the world's when None: it holds each run's
    process, so its cancel needs no pid to be verified.
    """
    processor = processor if processor is not None else world.processor
    for run in world.ledger.runs():
        going = replace(run, status="running")
        processor.cancel(going, mode="now")
        _wait_stopped(processor, going)


def test_a_run_first_seen_past_the_lost_run_deadline_is_cancelled_not_given_up(world):
    """S8 #1: no tick ran for longer than the wall clock and the deadline together (a laptop
    asleep). The first tick to see the run cancels it and keeps it, with its checkout; a later
    tick collects it once it has stopped, and only then does the checkout go to another case."""
    world.subject = _subject(world.workspace, budget=BudgetPolicy(timeout_minutes=30))
    claude = fake_claude(world.tmp_path / "claude", scenario="hang", sleep_s=HANG_S)
    world.processor = ClaudeHeadless(claude_bin=claude, runs_dir=world.tmp_path / "state" / "runs", auth_check=None)
    checkout = SharedCheckout(world.workspace, lock_dir=world.tmp_path / "state" / "locks")
    world.issue()
    try:
        assert world.tick().dispatched == (RUN_1,)
        world.issue(13, minutes=1)
        first_seen = NOW + timedelta(minutes=31) + LOST_RUN_DEADLINE

        late = world.tick(first_seen)

        assert (late.collected, late.dispatched) == ((), ())
        run = world.ledger.get_run(RUN_1)
        assert (run.status, run.cancel_sent_at) == ("running", first_seen)
        assert "cancel_requested_at" in json.loads((world.processor.run_dir(RUN_1) / RECORD_FILE).read_text())
        assert checkout.holder()["run_id"] == RUN_1
        assert world.case().state == "working"
        assert not any("lost-run deadline" in line for line in late.plan_lines)

        _wait_stopped(world.processor, run)  # the cancel stopped the hung run
        collected = world.tick(first_seen + timedelta(minutes=2))

        assert collected.collected == (RUN_1,)
        case = world.case()
        assert (case.state, _collected_error(case)) == ("needs-owner", "timed_out")
        assert not any("lost-run deadline" in line for line in collected.plan_lines)
        assert collected.dispatched == (_run_id("example-app-2", 1),)  # the checkout is free only now
    finally:
        _stop_runs(world)


def test_a_run_is_given_up_only_the_lost_run_deadline_after_the_tick_cancelled_it(world):
    """S8 #1: the deadline counts from the cancel, not from the start."""
    world.subject = _subject(world.workspace, budget=BudgetPolicy(timeout_minutes=30))
    world.processor = StubbornProcessor()
    world.issue()
    world.tick()
    first_seen = NOW + timedelta(hours=3)  # long past its wall clock, and never cancelled

    assert world.tick(first_seen).collected == ()
    assert world.ledger.get_run(RUN_1).cancel_sent_at == first_seen
    assert world.tick(first_seen + LOST_RUN_DEADLINE - timedelta(minutes=1)).collected == ()
    assert world.case().state == "working"

    given_up = world.tick(first_seen + LOST_RUN_DEADLINE)

    assert given_up.collected == (RUN_1,)
    assert any("lost-run deadline" in line for line in given_up.plan_lines)
    assert world.processor.cancels == [(RUN_1, "now"), (RUN_1, "now")]
    assert world.ledger.get_run(RUN_1).cancel_sent_at == first_seen  # the first cancel's, kept


def test_a_cancelled_run_that_stopped_is_collected_past_the_deadline_not_given_up(world):
    """S8 #1: past the deadline, a run is given up only while its processor still says it runs;
    one its cancel stopped is collected as any finished run is."""
    world.subject = _subject(world.workspace, budget=BudgetPolicy(timeout_minutes=30))
    world.processor = SlowProcessor()
    world.issue()
    world.tick()
    world.tick(NOW + timedelta(minutes=31))  # cancelled; the run stops, but no tick sees it for a while

    late = world.tick(NOW + timedelta(minutes=31) + LOST_RUN_DEADLINE + timedelta(minutes=5))

    assert late.collected == (RUN_1,)
    assert not any("lost-run deadline" in line for line in late.plan_lines)
    assert f"  run {RUN_1} ({CASE_1}): collected, timed_out" in late.plan_lines
    assert world.processor.cancels == [(RUN_1, "now")]


def _seed_inbox_case(world) -> str:
    """A case reported through the web inbox alone, working on a run no tick has collected."""
    ledger = world.ledger
    case = ledger.new_case(SLUG, "webinbox:example-site#r1", reporter=world.person, at=T0)
    message = LedgerEntry(
        at=T0, kind="message", actor=world.person, grade="bound", permission="report", text="No answer."
    )
    ledger.append(case.id, message)
    ledger.transition(case.id, "working", at=NOW, actor="liaise", reason="seeded")
    ledger.save_run(RunRecord(run_id=f"{case.id}-r1", case_id=case.id, subject=SLUG, mode="fresh", status="running", started_at=NOW))
    return case.id


def _reply_with(text):
    return RunResult(run_id="", outcomes=(Outcome(kind="reply", text=text),))


def _poisoned_escalation(world, tmp_path, monkeypatch):
    outcome = Outcome(kind="escalate", text="Send " + _poison("EscalationDraft"), reason="Needs " + _poison("EscalationReason"))
    result = RunResult(run_id="", outcomes=(outcome,), summary="Did " + _poison("Summary"))
    world.processor = EchoProcessor(results={CASE_1: result})
    world.issue()
    world.tick()
    world.tick(LATER)
    return CASE_1, (_poison("EscalationDraft"), _poison("EscalationReason"), _poison("Summary"))


def _poisoned_no_channel(world, tmp_path, monkeypatch):
    world.processor = EchoProcessor(default=_reply_with("Fixed: " + _poison("NoChannelDraft")))
    case_id = _seed_inbox_case(world)
    world.tick(LATER)
    return case_id, (_poison("NoChannelDraft"),)


def _poisoned_divert(world, tmp_path, monkeypatch):
    world.processor = EchoProcessor(results={CASE_1: _reply_with("Use " + _poison("DivertedDraft"))})
    world.issue()
    world.tick()
    world.tick(LATER)
    return CASE_1, (_poison("DivertedDraft"),)


def _poisoned_send_failure(world, tmp_path, monkeypatch):
    world.processor = EchoProcessor(results={CASE_1: _reply_with("Use " + _poison("FailedDraft"))})
    world.issue()
    world.tick()
    world.github.send_error = ChannelError("GitHub refused " + _poison("SendError"), kind="validation")
    world.tick(LATER, outbound_filters=())  # no gate to divert it first
    return CASE_1, (_poison("FailedDraft"), _poison("SendError"))


def _poisoned_deploy_output(world, tmp_path, monkeypatch):
    body = "import sys\nprint('push refused: ' + " + repr(_poison("DeployOutput")) + ")\nsys.exit(2)\n"
    script = write_executable_script(tmp_path / "deploy", body)
    world.subject = replace(world.subject, delivery=Delivery(kind="deploy", per="issue", command=script.as_posix()))
    world.processor = EchoProcessor(results={CASE_1: DELIVERED})
    world.issue()
    world.tick()
    world.tick(LATER)
    return CASE_1, (_poison("DeployOutput"),)


def _poisoned_delivery_exception(world, tmp_path, monkeypatch):
    world.subject = replace(world.subject, delivery=Delivery(kind="deploy", per="issue", command="deploy"))
    world.processor = EchoProcessor(results={CASE_1: DELIVERED})
    world.issue()
    world.tick()

    def explode(self, subject):
        raise RuntimeError("the runner said " + _poison("DeliveryRaised"))

    monkeypatch.setattr(tick_module._Tick, "_deploy", explode)
    world.tick(LATER)
    return CASE_1, (_poison("DeliveryRaised"),)


def _poisoned_start_exception(world, tmp_path, monkeypatch):
    class StartRaises(EchoProcessor):
        def start(self, job):
            raise RuntimeError("the spawn said " + _poison("StartRaised"))

    world.processor = StartRaises()
    world.issue()
    world.tick()
    return CASE_1, (_poison("StartRaised"),)


def _poisoned_status_exception(world, tmp_path, monkeypatch):
    world.issue()
    world.tick()

    def explode(*args, **kwargs):
        raise OSError("status said " + _poison("StatusRaised"))

    world.processor.status = explode
    world.tick(LATER)
    return CASE_1, ()  # a problem line only: nothing of it is kept on the case


def _poisoned_held_effects(world, tmp_path, monkeypatch):
    world.processor = EchoProcessor(results={CASE_1: _reply_with("Use " + _poison("HeldDraft"))})
    world.issue()
    world.tick()
    hold(world.ledger, "checkout:" + str(world.workspace), mode="block", now=NOW)
    world.tick(LATER)
    return CASE_1, (_poison("HeldDraft"),)


def _poisoned_error_class(world, tmp_path, monkeypatch):
    world.processor = EchoProcessor(health=Health(ok=False, error="unheard of " + _poison("ErrorClass")))
    world.issue()
    world.tick()
    return CASE_1, (_poison("ErrorClass"),)


def _poisoned_state_reads(world, tmp_path, monkeypatch):
    world.issue()
    hold(world.ledger, f"subject:{SLUG}", mode="block", now=T0)
    world.tick()
    world.ledger.clear_hold(f"subject:{SLUG}")
    read = world.github.read

    def refuse_issues(ref, **kwargs):
        if "#" in ref.id:
            raise ChannelError("the issue is gone: " + _poison("Unreadable"), kind="not_found")
        return read(ref, **kwargs)

    world.github.read = refuse_issues
    for attempt in range(STATE_READ_FAILURE_LIMIT):
        world.tick(LATER + timedelta(minutes=2 * attempt))
    return CASE_1, (_poison("Unreadable"),)


#: Each operator notification that text a case holds could reach, fed a token-shaped poison
#: where that text comes from: ``(world, tmp_path, monkeypatch) -> (case id, the poisons liaise
#: case show still shows)``. The run-lost, cancel-hold, refused-start and daily-cap notices
#: take in no such text.
POISONED_PATHS = {
    "escalation draft, reason and summary": _poisoned_escalation,
    "no channel to reach the reporter": _poisoned_no_channel,
    "diverted draft": _poisoned_divert,
    "failed send and its channel error": _poisoned_send_failure,
    "failed deploy output": _poisoned_deploy_output,
    "delivery that raised": _poisoned_delivery_exception,
    "start that raised": _poisoned_start_exception,
    "status that raised": _poisoned_status_exception,
    "effects held by a checkout hold": _poisoned_held_effects,
    "unknown error class": _poisoned_error_class,
    "unreadable issue state": _poisoned_state_reads,
}


#: The partner on every poisoned path: a person id, a GitHub login and an issue title that no
#: notification may carry (S9 #2), each built by concatenation.
POISONED_PERSON = "quinn" + "PersonMark"
POISONED_LOGIN = "quinn" + "-login-mark"
POISONED_ISSUE_TITLE = "Quinn's " + "IssueTitleMark"


@pytest.mark.parametrize("path", sorted(POISONED_PATHS))
def test_no_operator_notification_carries_what_the_case_holds(world, tmp_path, monkeypatch, path):
    """S8 #2, S9 #2: an ntfy topic is readable by anyone who knows its name. Every notification
    names the case and points at `liaise case show`, which shows what the notification left out.
    Its title is held to that too, and neither title nor body names the reporter, their address,
    or their issue's title."""
    world.person, world.login, world.issue_title = POISONED_PERSON, POISONED_LOGIN, POISONED_ISSUE_TITLE
    world.subject = _subject(world.workspace, person=POISONED_PERSON, login=POISONED_LOGIN)
    case_id, kept = POISONED_PATHS[path](world, tmp_path, monkeypatch)

    assert world.case(case_id).reporter == POISONED_PERSON  # the poison is where a notice could take it from
    assert world.notes, f"the {path} path told the operator nothing"
    marks = (POISON_PREFIX, POISONED_PERSON, POISONED_LOGIN, POISONED_ISSUE_TITLE, str(world.workspace.resolve()))
    for title, body, _ in world.notes:
        for mark in marks:
            assert mark not in title and mark not in body, (mark, title, body)
    assert any(f"see liaise case show {case_id}" in body for _, body, _ in world.notes)
    shown = "\n".join(case_show_lines(world.store, case_id))
    for poison in kept:
        assert poison in shown


def _closed_after_intake(world) -> None:
    """Issue 12 taken in while a hold kept it from starting; then it is closed, and the hold lifted."""
    world.issue()
    hold(world.ledger, f"subject:{SLUG}", mode="block", now=T0)
    world.tick()
    world.ledger.clear_hold(f"subject:{SLUG}")
    world.github.set_state(REPO, 12, "closed")


def _counted_reads(world) -> list:
    """The ids of the conversations read from now on, as correspond hands them to the channel."""
    reads = []
    read = world.github.read

    def counting(ref, **kwargs):
        reads.append(ref.id)
        return read(ref, **kwargs)

    world.github.read = counting
    return reads


def test_a_closed_ready_case_reads_its_issue_once_per_recheck_interval(world):
    """S8 #3: a read costs the issue and every page of its comments, and correspond's poll never
    reports a reopening, so a closed case that is otherwise ready is read once an interval."""
    _closed_after_intake(world)
    reads = _counted_reads(world)

    ticks = [world.tick(LATER + timedelta(minutes=2 * index)) for index in range(6)]

    assert all(report.dispatched == () for report in ticks)
    assert reads == [f"{REPO}#12"]
    assert f"  case {CASE_1} (intake): its issue is closed (read 2m ago; read again in 58m)" in ticks[1].plan_lines
    world.github.set_state(REPO, 12, "open")
    assert world.tick(LATER + CLOSED_RECHECK_INTERVAL - timedelta(minutes=1)).dispatched == ()
    assert len(reads) == 1
    assert world.tick(LATER + CLOSED_RECHECK_INTERVAL).dispatched == (RUN_1,)
    assert len(reads) == 2


def test_the_closed_recheck_interval_is_a_keyword_of_the_tick(world):
    _closed_after_intake(world)
    reads = _counted_reads(world)
    interval = timedelta(minutes=5)
    for minutes in (0, 4, 5):
        world.tick(LATER + timedelta(minutes=minutes), closed_recheck_interval=interval)
    assert len(reads) == 2


def test_state_reads_failing_the_limit_in_a_row_hand_the_case_to_the_owner_once(world):
    """S8 #3: a read that always fails (a deleted or transferred issue) is not a problem line
    every tick forever: the case goes to the owner, who is told once."""
    world.issue()
    hold(world.ledger, f"subject:{SLUG}", mode="block", now=T0)
    world.tick()
    world.ledger.clear_hold(f"subject:{SLUG}")
    reads = []

    def refuse(ref, **kwargs):
        reads.append(ref.id)
        raise ChannelError("no issue #12 that the gh account can see", kind="not_found")

    world.github.read = refuse
    for attempt in range(STATE_READ_FAILURE_LIMIT - 1):
        report = world.tick(LATER + timedelta(minutes=2 * attempt))
        assert report.problems and world.case().state == "intake"
    assert _titled(world, "cannot be read") == 0

    world.tick(LATER + timedelta(minutes=2 * STATE_READ_FAILURE_LIMIT))

    assert world.case().state == "needs-owner"
    ((_, body, _),) = [note for note in world.notes if "cannot be read" in note[0]]
    assert "cause: not_found" in body and "gh account" not in body
    for minutes in (10, 20, 30):
        world.tick(LATER + timedelta(minutes=minutes))
    assert len(reads) == STATE_READ_FAILURE_LIMIT
    assert _titled(world, "cannot be read") == 1


def test_a_state_read_that_succeeds_starts_the_failure_count_again(world):
    """S8 #3: the limit counts failures in a row. S9 #3: failures for good, which alone count."""
    _closed_after_intake(world)
    read = world.github.read
    failing = [True]

    def flaky(ref, **kwargs):
        if failing[0]:
            raise ChannelError("no issue #12 that the gh account can see", kind="not_found")
        return read(ref, **kwargs)

    world.github.read = flaky
    world.tick(LATER)
    world.tick(LATER + timedelta(minutes=2))
    failing[0] = False
    world.tick(LATER + timedelta(minutes=4))  # read, and closed
    failing[0] = True
    world.tick(LATER + timedelta(minutes=4) + CLOSED_RECHECK_INTERVAL)
    world.tick(LATER + timedelta(minutes=6) + CLOSED_RECHECK_INTERVAL)

    assert world.case().state == "intake"
    assert _titled(world, "cannot be read") == 0


# ---- S9: the fixes from the adversarial review of the S8 fixes ----

#: A gap no tick ran through: a reboot, or a laptop asleep.
LONG_GAP = timedelta(hours=3)
#: How long a run that ended inside its wall clock ran, or wrote its stream for.
RAN_FOR = timedelta(minutes=5)
#: How long a test gives a signal it checks was never sent to take effect: a signal is
#: delivered at once, so this is margin.
SIGNAL_SETTLE_S = 0.5
#: How long a test waits, at most, for a process it started to exit once asked.
BYSTANDER_WAIT_S = 10.0
#: More state reads failing for a while than the limit counts to.
TRANSIENT_READ_FAILURES = STATE_READ_FAILURE_LIMIT + 2
#: A process that lives until its stdin closes, so it ends cleanly on every platform.
_WAITS_FOR_STDIN = "import sys\nsys.stdin.read()\n"


class PidProcessor(EchoProcessor):
    """Starts runs that keep going, their process recorded as ``pid``: runs a tick started before a reboot."""

    def __init__(self, pid, **kwargs):
        super().__init__(**kwargs)
        self.pid = pid

    def start(self, job):
        return replace(super().start(job), status="running", ended_at=None, pid=self.pid)

    def status(self, run, *, persist=True):
        return replace(run, status="running", ended_at=None)


class EndedEarly(EchoProcessor):
    """An EchoProcessor whose runs ended RAN_FOR after the start the tick recorded."""

    def status(self, run, *, persist=True):
        return replace(run, status="finished", ended_at=run.started_at + RAN_FOR)


def _bystander() -> subprocess.Popen:
    """A process the test starts that is no run's: what a recorded pid can name after a reboot.

    On POSIX it leads its own process group, as a run does, so a cancel sent to its pid would
    reach it.
    """
    group = {} if sys.platform == "win32" else {"start_new_session": True}
    return subprocess.Popen([sys.executable, "-c", _WAITS_FOR_STDIN], stdin=subprocess.PIPE, **group)


def _end(process: subprocess.Popen) -> None:
    process.stdin.close()
    try:
        process.wait(timeout=BYSTANDER_WAIT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def test_a_pid_another_process_holds_by_now_is_never_signalled_and_its_run_is_collected(world):
    """S9 #1: a run first seen past its wall clock after a long gap. Its recorded pid now names a
    process that started long after the run did. The tick sends that process nothing, and collects
    the run, whose own process is gone, as crashed: it stopped writing well inside its wall clock."""
    assert abs(datetime.now(timezone.utc) - NOW) > PID_START_TOLERANCE  # the bystander starts apart from the run
    bystander = _bystander()
    try:
        world.processor = PidProcessor(bystander.pid, results={CASE_1: ASKED})
        world.issue()
        assert world.tick().dispatched == (RUN_1,)
        runs_dir = world.tmp_path / "state" / "runs"
        stream = runs_dir / RUN_1 / STREAM_FILE
        stream.parent.mkdir(parents=True)
        stream.write_text(json.dumps({"type": "system", "subtype": "init"}) + "\n")
        last_write = (NOW + RAN_FOR).timestamp()
        os.utime(stream, (last_write, last_write))
        world.processor = ClaudeHeadless(runs_dir=runs_dir, auth_check=None)  # a later tick is another process

        report = world.tick(NOW + LONG_GAP)

        assert report.collected == (RUN_1,)
        case = world.case()
        assert (case.state, _collected_error(case)) == ("needs-owner", "crashed")
        assert _titled(world, "crashed") == 1
        record = json.loads((runs_dir / RUN_1 / RECORD_FILE).read_text())
        assert "cancel_requested_at" not in record and "cancel_signal" not in record
        with pytest.raises(subprocess.TimeoutExpired):
            bystander.wait(timeout=SIGNAL_SETTLE_S)  # still alive: nothing was sent to it
    finally:
        _end(bystander)


def test_a_hung_run_whose_process_a_later_tick_verifies_is_still_cancelled(world):
    """S9 #1: the process a run's pid names, started when the run was spawned, is the run's: a
    later tick, which did not spawn it, still stops it past its wall clock."""
    world.subject = _subject(world.workspace, budget=BudgetPolicy(timeout_minutes=30))
    runs_dir = world.tmp_path / "state" / "runs"
    claude = fake_claude(world.tmp_path / "claude", scenario="hang", sleep_s=HANG_S)
    spawner = ClaudeHeadless(claude_bin=claude, runs_dir=runs_dir, auth_check=None)
    world.processor = spawner
    world.issue()
    try:
        assert world.tick().dispatched == (RUN_1,)
        world.processor = ClaudeHeadless(claude_bin=claude, runs_dir=runs_dir, auth_check=None)
        cancelled_at = NOW + timedelta(minutes=31)

        late = world.tick(cancelled_at)

        assert late.collected == ()
        sent = "terminate" if sys.platform == "win32" else "SIGTERM"
        assert json.loads((runs_dir / RUN_1 / RECORD_FILE).read_text())["cancel_signal"] == sent
        run = world.ledger.get_run(RUN_1)
        assert run.cancel_sent_at == cancelled_at
        _wait_stopped(spawner, run)  # the spawner reaps what the cancel stopped

        done = world.tick(cancelled_at + timedelta(minutes=2))

        assert done.collected == (RUN_1,)
        case = world.case()
        assert (case.state, _collected_error(case)) == ("needs-owner", "timed_out")
    finally:
        _stop_runs(world, processor=spawner)


def test_a_run_whose_start_cannot_be_read_is_never_signalled_and_is_given_up_at_the_deadline(world, monkeypatch):
    """S9 #1: a live pid whose start cannot be read may be another process's, so it is sent
    nothing. Its cancel is recorded all the same, and the run keeps its checkout until the
    lost-run deadline after that cancel, when it is given up."""
    world.subject = _subject(world.workspace, budget=BudgetPolicy(timeout_minutes=30))
    runs_dir = world.tmp_path / "state" / "runs"
    record_path = runs_dir / RUN_1 / RECORD_FILE
    claude = fake_claude(world.tmp_path / "claude", scenario="hang", sleep_s=HANG_S)
    spawner = ClaudeHeadless(claude_bin=claude, runs_dir=runs_dir, auth_check=None)
    world.processor = spawner
    checkout = SharedCheckout(world.workspace, lock_dir=world.tmp_path / "state" / "locks")
    world.issue()
    try:
        assert world.tick().dispatched == (RUN_1,)
        monkeypatch.setattr(workspace_module, "process_started_at", lambda pid: None)
        world.processor = ClaudeHeadless(runs_dir=runs_dir, auth_check=None)
        cancelled_at = NOW + timedelta(minutes=31)

        late = world.tick(cancelled_at)

        assert late.collected == ()
        assert world.ledger.get_run(RUN_1).cancel_sent_at == cancelled_at
        assert "cancel_signal" not in json.loads(record_path.read_text())
        assert world.tick(cancelled_at + LOST_RUN_DEADLINE - timedelta(minutes=1)).collected == ()
        assert checkout.holder()["run_id"] == RUN_1

        given_up = world.tick(cancelled_at + LOST_RUN_DEADLINE)

        assert given_up.collected == (RUN_1,)
        assert any("lost-run deadline" in line for line in given_up.plan_lines)
        case = world.case()
        assert (case.state, _collected_error(case)) == ("needs-owner", "timed_out")
        assert "cancel_signal" not in json.loads(record_path.read_text())
        assert pid_is_alive(world.ledger.get_run(RUN_1).pid)  # never signalled: it still hangs
    finally:
        _stop_runs(world, processor=spawner)


@pytest.mark.parametrize(
    "error",
    [
        ChannelError("could not reach GitHub", kind="network", retryable=True),
        ChannelError("gh is not logged in", kind="auth"),
        RuntimeError("the adapter broke"),
    ],
    ids=["network", "auth", "unexpected"],
)
def test_state_reads_failing_for_a_while_never_hand_the_case_to_the_owner(world, error):
    """S9 #3: minutes offline must not send every ready GitHub case to the owner. Only a read that
    failed for good (the issue not found, or not permitted) counts toward the limit."""
    world.issue()
    hold(world.ledger, f"subject:{SLUG}", mode="block", now=T0)
    world.tick()
    world.ledger.clear_hold(f"subject:{SLUG}")
    read = world.github.read

    def failing(ref, **kwargs):
        if "#" in ref.id:
            raise error
        return read(ref, **kwargs)

    world.github.read = failing
    ticks = [world.tick(LATER + timedelta(minutes=2 * index)) for index in range(TRANSIENT_READ_FAILURES)]

    assert all(report.problems and report.dispatched == () for report in ticks)
    assert world.case().state == "intake"
    assert world.ledger.get_issue_check(CASE_1).failures == 0
    assert _titled(world, "cannot be read") == 0
    del world.github.read
    assert world.tick(LATER + timedelta(minutes=2 * TRANSIENT_READ_FAILURES)).dispatched == (RUN_1,)


def test_a_run_that_ended_inside_its_wall_clock_keeps_its_outcomes_however_late_it_is_collected(world):
    """S9 #4: whether a run timed out is judged at its end, not at the tick that first collects it."""
    world.processor = EndedEarly(results={CASE_1: ASKED})
    world.issue()
    world.tick()

    report = world.tick(NOW + LONG_GAP)

    assert report.collected == (RUN_1,)
    case = world.case()
    assert (case.state, _collected_error(case)) == ("needs-partner", None)
    assert [ref.encoded for ref, _ in world.github.sent] == [ISSUE_12]
    assert _titled(world, "timed_out") == 0


def test_a_filter_with_no_name_is_named_by_its_type_so_what_it_is_bound_to_stays_out_of_the_notice(world):
    """S9 #5: a filter's repr holds what it was bound to, a local path say, and the name of the
    filter that diverted a message reaches the operator's notification."""
    rules_path = "/Us" + "ers/someone/liaise/outbound-rules.toml"

    def refuse_by_rules(rules, outbound, ctx):
        return Divert("refused by the rules")

    world.processor = EchoProcessor(results={CASE_1: _reply_with("Fixed.")})
    world.issue()
    world.tick()
    world.tick(LATER, outbound_filters=(functools.partial(refuse_by_rules, rules_path),))

    ((_, body, _),) = [note for note in world.notes if "waits for you" in note[0]]
    assert "cause: partial" in body
    assert all(rules_path not in title and rules_path not in body for title, body, _ in world.notes)
