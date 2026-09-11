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
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from liaise.config import ConfigError, GlobalConfig
from liaise.github import FakeGitHub, Issue
from liaise.holds import hold
from liaise.ledger import Ledger
from liaise.model import Health, Outcome, RunRecord, RunResult
from liaise.outcomes import OUTCOME_SCHEMA
from liaise.processor import EchoProcessor
from liaise.subjects import BudgetPolicy, Delivery, Policy, Subject, Workspace
from liaise.testing import FakeGitHubChannel, demo_registry
from liaise.tests.conftest import write_executable_script
from liaise.tick import BUDGET_MESSAGE, TRY_IT_MESSAGE, run_once, status_lines
from liaise.workspace import SharedCheckout

T0 = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=1)
LATER = NOW + timedelta(minutes=5)
SLUG = "example-app"
REPO = "example/app"
BINDING = "github:example/app?labels=partner:pat"
ISSUE_12 = "github:example/app#12"
CASE_1 = "example-app-1"
RUN_1 = "example-app-1-r1"

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

    def status(self, run):
        if run.run_id in self.stopping:
            return replace(run, status="finished", ended_at=run.started_at)
        return replace(run, status="running", ended_at=None)

    def cancel(self, run, *, mode="graceful"):
        current = self.status(run)
        if current.status == "running":
            self.cancels.append((run.run_id, mode))
            self.stopping.add(run.run_id)
        return current

    def collect(self, run, *, timed_out=False):
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
    for check in ("no hold", "pat may request_work", "within budget", "preflight ok", "workspace free"):
        assert check in planned
    assert "  would dispatch example-app-2 as run example-app-2-r1 (fresh)" in lines
    assert any("would label github:example/app#13 liaise:working" in line for line in lines)
    assert report.dispatched == ("example-app-2-r1",)

    assert world.store == snapshot
    assert world.github.sent == []
    assert len(world.processor.jobs) == 1
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
    still = world.tick(LATER + timedelta(minutes=2))
    assert still.dispatched == ()
    assert world.ledger.get_hold("processor") is not None
    assert len(world.notes) == notified  # the operator heard once

    world.processor = EchoProcessor()
    recovered = world.tick(LATER + timedelta(minutes=4))
    assert world.ledger.get_hold("processor") is None
    assert recovered.dispatched == (f"{CASE_1}-r2",)
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
