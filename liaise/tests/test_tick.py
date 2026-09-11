"""Tests for liaise.tick: run_once end to end over fakes, and status_lines.

Every channel is a fake from liaise.testing, heard and written through correspond. The
processor is an EchoProcessor, or a subclass whose runs keep going. The labeler is
FakeGitHub, the store a dict, the clock explicit, and every path is under tmp_path, so no
test reads Claude Code's real session records, runs claude or gh, or sends anything.
acquaint is made unimportable, so no test reads real people records.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from correspond.errors import ChannelError

from liaise import tick as tick_module
from liaise.cases import set_case_state
from liaise.config import ConfigError, GlobalConfig
from liaise.github import FakeGitHub, Issue
from liaise.holds import hold
from liaise.ledger import Ledger
from liaise.model import Health, LedgerEntry, Outcome, RunRecord, RunResult
from liaise.outcomes import OUTCOME_SCHEMA
from liaise.processor import RECORD_FILE, ClaudeHeadless, EchoProcessor
from liaise.subjects import BudgetPolicy, Delivery, Policy, ProcessorConfig, Subject, Workspace
from liaise.testing import FakeGitHubChannel, demo_registry
from liaise.tests._fake_claude import fake_claude
from liaise.tests.conftest import write_executable_script
from liaise.tick import (
    AUTH_PROBE_INTERVAL,
    BUDGET_MESSAGE,
    LOST_RUN_DEADLINE,
    NUDGE_MESSAGE,
    RUN_ID_SUFFIX_DIGITS,
    SEE_STATUS,
    TRY_IT_MESSAGE,
    RunLockHeld,
    last_run_age,
    run_lock_path,
    run_once,
    run_stamps,
    status_lines,
)
from liaise.workspace import SharedCheckout

T0 = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
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


def _subject(workspace: Path, *, reply_mode="direct", delivery=None, budget=None) -> Subject:
    return Subject(
        slug=SLUG,
        bindings=(BINDING,),
        policy=Policy(
            people={"github:pat": "pat"},
            roles={"pat": "partner"},
            relays=("github:example-bot",),
            claim_labels={"partner:pat": "pat"},
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

    def issue(self, number=12, *, author="pat", minutes=0, labels=("partner:pat",)):
        created = T0 + timedelta(minutes=minutes)
        body = "The export drops the last row."
        self.github.add_issue(
            REPO, number, author=author, title="Export", body=body, labels=labels, created_at=created
        )
        self.labeler.seed(
            Issue(REPO, number, "Export", author, body, created, created, "open", labels=tuple(labels))
        )

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
    assert not (world.tmp_path / "state" / "run.lock").exists()


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
    """0.0.x L-3: one tick at a time. The lock holds this test's own pid, which is alive."""
    lock = _lock(world, os.getpid())
    world.issue()
    with pytest.raises(RunLockHeld, match="already in progress"):
        world.tick()
    assert (world.processor.jobs, world.store) == ([], {})
    assert lock.read_text() == str(os.getpid())


def test_a_run_lock_left_by_a_dead_process_is_reclaimed(world):
    lock = _lock(world, DEAD_PID)
    world.issue()
    assert world.tick().dispatched == (RUN_1,)
    assert not lock.exists()  # released once the tick ended


def test_a_dry_run_neither_takes_nor_minds_the_run_lock(world):
    lock = _lock(world, os.getpid())
    world.issue()
    assert world.tick(dry_run=True).dispatched == (RUN_1,)  # a live holder does not stop it
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
    assert not lock_path.exists()


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
    assert SEE_STATUS in body and "liaise case set-state" in body
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
    """#2: past its wall clock and LOST_RUN_DEADLINE, a run that ignores every cancel is
    finished as timed_out, whatever its pid says, and its case goes to the owner."""
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
    assert world.tick(LATER + timedelta(minutes=10)).dispatched == (RUN_1,)
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
    assert TOKEN_SHAPED not in body and "log in" not in body
    for part in (CASE_1, "pat", ISSUE_12, "leak scan: token", SEE_STATUS):
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
    assert TOKEN_SHAPED not in body and "log in" not in body
    for part in (CASE_1, "pat", ISSUE_12, "send failed", SEE_STATUS):
        assert part in body


def test_the_run_lock_is_created_exclusively(world, monkeypatch):
    """#11: finding the run lock free and taking it are one step."""
    lock = run_lock_path(world.config.state_dir)
    flags = []
    real_open = os.open

    def spy(path, flag, *args, **kwargs):
        if Path(path) == lock:
            flags.append(flag)
        return real_open(path, flag, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    world.tick()
    assert flags and all(flag & os.O_CREAT and flag & os.O_EXCL for flag in flags)


def test_a_tick_leaves_a_run_lock_another_process_took_over(world, monkeypatch):
    """#11: releasing the run lock removes it only while it holds this process's pid."""
    lock = run_lock_path(world.config.state_dir)

    def taken_over(self):
        lock.write_text(str(DEAD_PID))  # another tick reclaimed the lock mid-tick

    monkeypatch.setattr(tick_module._Tick, "project", taken_over)
    world.tick()
    assert lock.read_text() == str(DEAD_PID)


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
