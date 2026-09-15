"""Tests for an inert subject (#30): ``active = false`` loads and shows, and no tick acts on it.

The tick tests reuse test_tick's World over fakes: FakeGitHubChannel, FakeGitHub, an
EchoProcessor, a dict store and paths under tmp_path, so nothing reads real configuration,
runs claude or gh, posts or labels anything.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import timedelta

import cw
import pytest

from liaise import cli
from liaise.cases import set_case_state
from liaise.config import ConfigError
from liaise.model import RunRecord
from liaise.processor import EchoProcessor
from liaise.subjects import load_subject
from liaise.testing import demo_registry
from liaise.tests.test_tick import CASE_1, LATER, NOW, SLUG, T0, World, _subject
from liaise.tick import run_once, status_lines

INERT_TOML = """
active = false
bindings = ["github:example/app?labels=partner:pat"]

[policy]
people = { "github:pat" = "pat" }
roles = { pat = "partner" }
"""


@pytest.fixture(autouse=True)
def no_real_acquaint(monkeypatch):
    monkeypatch.setitem(sys.modules, "acquaint", None)


@pytest.fixture
def world(tmp_path) -> World:
    workspace = tmp_path / "code" / "example-app"
    workspace.mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    inert = replace(_subject(workspace), active=False)
    return World(tmp_path=tmp_path, workspace=workspace, sessions=sessions, subject=inert)


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "config"
    (root / "subjects").mkdir(parents=True)
    (root / "config.toml").write_text(f'owner_login = "owner"\nstate_dir = "{(tmp_path / "state").as_posix()}"\n')
    (root / "subjects" / f"{SLUG}.toml").write_text(INERT_TOML)
    return root


# ---- the subject file ----


def test_active_defaults_to_true_and_reads_false(tmp_path):
    active = tmp_path / "active.toml"
    active.write_text(INERT_TOML.replace("active = false\n", ""))
    inert = tmp_path / "inert.toml"
    inert.write_text(INERT_TOML)

    assert (load_subject(active).active, load_subject(inert).active) == (True, False)


@pytest.mark.parametrize("value", ['"no"', "0", '"false"'])
def test_active_must_be_true_or_false(tmp_path, value):
    path = tmp_path / "example-app.toml"
    path.write_text(INERT_TOML.replace("active = false", f"active = {value}"))

    with pytest.raises(ConfigError, match="active must be true or false, as in active = false"):
        load_subject(path)


# ---- the tick ----


def test_an_inactive_subject_polls_opens_starts_and_labels_nothing(world):
    world.issue()
    labels = world.labels()

    first = world.tick()
    second = world.tick(LATER)

    assert list(world.ledger.cases()) == []
    assert world.labels() == labels
    assert world.github.sent == [] and world.notes == []
    assert (first.dispatched, second.dispatched) == ((), ())
    assert f"subject {SLUG}: inactive (active = false), not ticked" in first.plan_lines


def test_the_cases_of_a_subject_made_inactive_keep_their_labels(world):
    world.subject = replace(world.subject, active=True)
    world.issue()
    world.tick(T0 + timedelta(minutes=1))  # inside the quiet window: the case opens, no run starts
    labels = world.labels()
    set_case_state(world.ledger, CASE_1, "needs-owner", now=NOW)  # a state the labels would follow

    world.subject = replace(world.subject, active=False)
    world.tick(LATER)

    assert world.labels() == labels
    assert world.case().state == "needs-owner"


def test_naming_an_inactive_subject_is_refused_outside_a_dry_run_and_planned_in_one(world):
    world.issue()

    with pytest.raises(ConfigError, match=f"subject '{SLUG}' is inactive \\(active = false\\), so no tick acts on it"):
        world.tick(only=SLUG)
    report = world.tick(only=SLUG, dry_run=True)

    assert f"subject {SLUG}: inactive (active = false); planned, as a dry run" in report.plan_lines
    assert any("example-app-1" in line for line in report.plan_lines)  # the case it would open
    assert world.store == {} and list(world.ledger.cases()) == []


def test_the_other_subjects_are_ticked_beside_an_inactive_one(world):
    world.subject = replace(world.subject, active=True)
    other = replace(world.subject, slug="example-other", bindings=("github:example/other?labels=partner:pat",), active=False)
    world.issue()

    report = run_once(
        {SLUG: world.subject, "example-other": other},
        world.store,
        global_config=world.config,
        registry=demo_registry(github=world.github),
        processor=world.processor,
        labeler=world.labeler,
        notify_fn=world.notify,
        sessions_dir=world.sessions,
        now=NOW,
    )

    assert [case.subject for case in world.ledger.cases()] == [SLUG]
    assert "subject example-other: inactive (active = false), not ticked" in report.plan_lines


def test_a_run_left_in_flight_is_not_collected_while_its_subject_is_inactive(world):
    run = RunRecord(run_id=f"{CASE_1}-r1", case_id=CASE_1, subject=SLUG, mode="fresh", status="running", started_at=NOW)
    world.ledger.save_run(run)

    report = world.tick(LATER)

    assert any("is inactive (active = false), so it is not collected" in problem for problem in report.problems)
    assert world.ledger.get_run(run.run_id).status == "running"


def test_status_marks_an_inactive_subject(world):
    lines = status_lines({SLUG: world.subject}, world.store, global_config=world.config, now=NOW)

    assert f"subject {SLUG}: 0 case(s), 0/6 dispatches today (inactive: no tick acts on it)" in lines


# ---- the command line ----


class _Labeler:
    """A labeler that records every label it is asked to create."""

    def __init__(self):
        self.created = []

    def create_label(self, repo, name, *, color, description):
        self.created.append((repo, name))


def test_setup_refuses_an_inactive_subject_and_creates_no_label(root):
    labeler = _Labeler()

    with pytest.raises(cw.CommandError, match=f"subject '{SLUG}' is inactive \\(active = false\\), so liaise setup creates no labels"):
        cli.setup(SLUG, root=str(root), labeler=labeler)

    assert labeler.created == []


def test_subject_list_and_show_mark_an_inactive_subject(root):
    assert "[inactive: active = false]" in cli.subject_list(root=str(root))
    assert "  active: False" in cli.subject_show(SLUG, root=str(root)).splitlines()


def test_run_names_an_inactive_subject_only_in_a_dry_run(root, tmp_path):
    (tmp_path / "sessions").mkdir()
    fakes = dict(
        registry=demo_registry(),
        processor=EchoProcessor(),
        labeler=_Labeler(),
        notify_fn=lambda *args, **kwargs: None,
        sessions_dir=str(tmp_path / "sessions"),
        now=NOW,
        store={},
    )

    with pytest.raises(cw.CommandError, match="is inactive"):
        cli.run(root=str(root), once=True, subject=SLUG, **fakes)
    planned = cli.run(root=str(root), dry_run=True, subject=SLUG, **fakes)

    assert f"subject {SLUG}: inactive (active = false); planned, as a dry run" in planned.splitlines()
