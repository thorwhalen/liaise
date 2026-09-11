"""The one-command smoke test: ``liaise run --once --dry-run`` over a fictional configuration.

This is liaise 0.1's definition of working. One in-process call to :func:`liaise.cli.run`,
with the arguments the command line gives it, reads a config root under ``tmp_path``: one
subject bound to a fake GitHub repository and a fake web inbox. The plan it prints shows:

1. intake taking in a relay-filed issue and a signed web-inbox report, each a new case;
2. a run that finished before this tick, collected, with its outcomes through the
   outbound gate: a reply holding a local path is diverted by the leak scan, and a
   question passes with the partner's mention added;
3. the first ready case planned for dispatch, past holds, authorization, budget,
   preflight and the workspace check.

The tick reconciles runs before it starts new ones, so a finished run frees its slot and
its checkout first. That is why the gate's lines come before the planned dispatch.

Nothing is written or sent: the store, the channels, the labels, the processor and every
file under ``tmp_path`` are checked unchanged. Every seam is a fake from liaise.testing,
and acquaint is made unimportable, so no real people records are read.

Run it alone with ``python -m pytest liaise/tests/test_smoke.py``. It must pass after
every change.
"""

from __future__ import annotations

import copy
import functools
import inspect
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from correspond.channels.webinbox import WebInbox

from liaise import cli
from liaise.gate import DFLT_OUTBOUND_FILTERS
from liaise.github import FakeGitHub, Issue
from liaise.ledger import Ledger
from liaise.model import LedgerEntry, Outcome, RunRecord, RunResult
from liaise.processor import EchoProcessor
from liaise.testing import FakeGitHubChannel, add_webinbox_report, demo_registry
from liaise.tick import run_once

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
SLUG = "example-app"
REPO = "example/app"
SITE = "example-site"
#: What the store is seeded with: a case on issue 2, working on a run that has finished.
SEEDED_CASE = "example-app-1"
SEEDED_RUN = "example-app-1-r1"
SEEDED_ISSUE = "github:example/app#2"
#: A local path the reply must not publish, built by concatenation so this file holds none.
LEAKED_PATH = "/" + "Users" + "/pat/search-notes.md"
REPLY = Outcome(kind="reply", text=f"Accents match now. My notes: {LEAKED_PATH}")
ASK = Outcome(
    kind="ask",
    text="One question first.",
    questions=("Should names match too? (default: yes)",),
)

CONFIG_TOML = 'owner_login = "owner"\nstate_dir = "{state_dir}"\n'
SUBJECT_TOML = """
bindings = ["github:example/app?labels=partner:pat", "webinbox:example-site"]
workspace = {{ path = "{workspace}" }}

[policy]
default_reply_mode = "direct"
people = {{ "github:pat" = "pat", "webinbox:pat" = "pat" }}
roles = {{ pat = "partner" }}
relays = ["github:example-bot"]
claim_labels = {{ "partner:pat" = "pat" }}
"""


@pytest.fixture(autouse=True)
def no_real_acquaint(monkeypatch):
    monkeypatch.setitem(sys.modules, "acquaint", None)


@dataclass
class Smoke:
    """The config root, and every fake the command is handed."""

    root: Path
    sessions: Path
    github: FakeGitHubChannel
    webinbox: WebInbox
    labeler: FakeGitHub
    processor: EchoProcessor
    store: dict
    notified: list = field(default_factory=list)

    def notify(self, title, body, *, priority="default"):
        self.notified.append(title)

    def run(self) -> str:
        """``liaise run --once --dry-run --root <root>``, its seams filled with the fakes."""
        return cli.run(
            root=str(self.root),
            once=True,
            dry_run=True,
            registry=demo_registry(github=self.github, webinbox=self.webinbox),
            processor=self.processor,
            labeler=self.labeler,
            store=self.store,
            notify_fn=self.notify,
            sessions_dir=str(self.sessions),
            now=NOW,
        )


def _issue(number: int, *labels: str) -> Issue:
    created = NOW - timedelta(hours=4)
    return Issue(REPO, number, "Search", "example-bot", "", created, created, "open", labels=labels)


def _seed_a_finished_run(store: dict) -> None:
    """A case on issue 2, working on a run the tick has not collected yet.

    The ledger says the run is ``running`` until a tick collects it, whatever the processor
    says; the EchoProcessor says it has finished.
    """
    ledger = Ledger(store)
    opened, started = NOW - timedelta(hours=4), NOW - timedelta(minutes=20)
    case = ledger.new_case(SLUG, SEEDED_ISSUE, reporter="pat", at=opened)
    ledger.append(
        case.id,
        LedgerEntry(
            at=opened,
            kind="message",
            actor="pat",
            grade="platform",
            permission="report",
            text="Search results skip accented names.",
        ),
    )
    ledger.transition(case.id, "working", at=started, actor="liaise", reason=f"dispatched run {SEEDED_RUN} (fresh)")
    ledger.append(
        case.id,
        LedgerEntry(
            at=started,
            kind="run",
            actor="liaise",
            detail={"event": "started", "run_id": SEEDED_RUN, "mode": "fresh", "day": started.date().isoformat()},
        ),
    )
    ledger.save_run(
        RunRecord(
            run_id=SEEDED_RUN,
            case_id=case.id,
            subject=SLUG,
            mode="fresh",
            status="running",
            started_at=started,
            heartbeat_at=started,
            session_id="sess-example-1",
        )
    )
    ledger.increment_daily(SLUG, started.date())


@pytest.fixture
def smoke(tmp_path) -> Smoke:
    root = tmp_path / "config"
    (root / "subjects").mkdir(parents=True)
    workspace = tmp_path / "code" / SLUG
    workspace.mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (root / "config.toml").write_text(CONFIG_TOML.format(state_dir=(tmp_path / "state").as_posix()))
    (root / "subjects" / f"{SLUG}.toml").write_text(SUBJECT_TOML.format(workspace=workspace.as_posix()))

    github = FakeGitHubChannel(clock=lambda: NOW)
    github.add_issue(
        REPO,
        1,
        author="example-bot",
        title="Search",
        body="Search ignores accents in city names.",
        labels=["partner:pat"],
        created_at=NOW - timedelta(hours=3),
    )
    webinbox = WebInbox(store={}, blobs={})
    add_webinbox_report(
        webinbox, SITE, text="The contact form never answers.", received_at=NOW - timedelta(hours=2), user="pat", name="Pat"
    )
    store: dict = {}
    _seed_a_finished_run(store)
    return Smoke(
        root=root,
        sessions=sessions,
        github=github,
        webinbox=webinbox,
        labeler=FakeGitHub([_issue(1, "partner:pat"), _issue(2, "partner:pat", "liaise:working")]),
        processor=EchoProcessor(default=RunResult(run_id="", outcomes=(REPLY, ASK), summary="Accents; one question.")),
        store=store,
    )


def _files(root: Path) -> dict:
    """Every path under ``root``, with a file's bytes: what a write or a new directory changes."""
    return {path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None for path in root.rglob("*")}


def _starting_with(lines: list[str], prefix: str) -> int:
    (index,) = [i for i, line in enumerate(lines) if line.startswith(prefix)]
    return index


def test_run_once_dry_run_plans_intake_the_gate_and_a_dispatch_and_changes_nothing(smoke, tmp_path):
    store_before = copy.deepcopy(smoke.store)
    inbox_before = copy.deepcopy(smoke.webinbox.store)
    issues_before = [smoke.labeler.get_issue(REPO, number) for number in (1, 2)]
    files_before = _files(tmp_path)

    output = smoke.run()
    lines = output.splitlines()

    # 1. intake: the relay-filed issue and the signed report, each a new case
    intake = lines.index(f"intake {SLUG}: 2 new events [dry run]")
    issue_event = lines[_starting_with(lines, f"  github:{REPO}#1 from github:example-bot: opened {SLUG}-2 ")]
    assert "pat via relay-label" in issue_event
    report_event = lines[_starting_with(lines, f"  webinbox:{SITE}#")]
    assert f"from webinbox:pat: opened {SLUG}-3 " in report_event and "pat via handle" in report_event
    assert f"  case {SLUG}-2: created, state intake, reporter pat" in lines
    assert f"  case {SLUG}-3: created, state intake, reporter pat" in lines

    # 2. the finished run, collected, and its outcomes through the gate
    collected = lines.index(f"  run {SEEDED_RUN} ({SEEDED_CASE}): collected, no error")
    diverted = lines.index(f"  gate reply to {SEEDED_ISSUE}: diverted (leak scan: local path), kept as a draft")
    assert lines[diverted + 1].startswith("    note: leak scan: local path at character ")
    assert f"  would notify the operator: liaise: a draft for {SEEDED_CASE} waits for you" in lines
    passed = _starting_with(lines, f"  gate ask to {SEEDED_ISSUE}: would send: @pat One question first. 1. Should names")
    assert "    note: added the mention @pat" in lines[passed + 1 : passed + 4]
    assert f"  case {SEEDED_CASE}: working -> needs-partner (ask)" in lines

    # 3. the planned dispatch, past every check
    planned = lines[_starting_with(lines, f"  case {SLUG}-2 (intake): ready")]
    for check in ("no hold", "pat may request_work", "within budget", "preflight ok", "workspace free"):
        assert check in planned
    dispatch = lines.index(f"  would dispatch {SLUG}-2 as run {SLUG}-2-r1 (fresh)")
    assert f"  case {SLUG}-3 (intake): ready, but 1 run(s) in flight (concurrent cap 1)" in lines

    assert intake < collected < diverted < passed < dispatch
    assert "problem:" not in output

    # ...and nothing was written, sent, labelled, started or notified
    assert smoke.store == store_before
    assert smoke.webinbox.store == inbox_before
    assert smoke.github.sent == []
    assert [smoke.labeler.get_issue(REPO, number) for number in (1, 2)] == issues_before
    assert smoke.labeler.labels_created(REPO) == {}
    assert smoke.processor.jobs == []
    assert smoke.notified == []
    assert _files(tmp_path) == files_before
    assert not list(tmp_path.rglob("record.json"))


def test_the_diversion_and_the_mention_come_from_the_gate(smoke, monkeypatch):
    """The first test's divert and mention are the gate's doing: the tick's filters default to
    DFLT_OUTBOUND_FILTERS, and with no filters the leaky reply would go out as is."""
    assert inspect.signature(run_once).parameters["outbound_filters"].default is DFLT_OUTBOUND_FILTERS
    monkeypatch.setattr(cli, "run_once", functools.partial(run_once, outbound_filters=()))

    lines = smoke.run().splitlines()

    assert not any("diverted" in line for line in lines)
    _starting_with(lines, f"  gate reply to {SEEDED_ISSUE}: would send: Accents match now. My notes: {LEAKED_PATH}")
    _starting_with(lines, f"  gate ask to {SEEDED_ISSUE}: would send: One question first.")
    assert smoke.github.sent == []
