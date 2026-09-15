"""Tests for waiting labels (#31): which person a case waits on, as a label beside the state label.

Every labeler is FakeGitHub or a stand-in that fails on use, and the tick test reuses
test_tick's World over fakes, so nothing labels a real repository.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from liaise.config import ConfigError
from liaise.github import FakeGitHub, Issue
from liaise.model import CASE_STATES, Case
from liaise.projection import WAITING_STATES, project_labels, setup_labels, waiting_label
from liaise.subjects import Policy, Subject, load_subject
from liaise.tests.test_tick import CASE_1, LATER, World, _subject

T0 = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
REPO = "example/app"
ISSUE = "github:example/app#12"
LABELS = {"pat": "needs-pat", "sam": "needs-sam"}

SUBJECT_TOML = """
bindings = ["github:example/app?labels=partner:pat"]

[policy]
people = {{ "github:pat" = "pat" }}
roles = {{ pat = "partner", sam = "observer" }}
claim_labels = {{ "partner:pat" = "pat" }}
{waiting}
"""


@pytest.fixture(autouse=True)
def no_real_acquaint(monkeypatch):
    monkeypatch.setitem(sys.modules, "acquaint", None)


def _subject_with(waiting_labels=None, **fields) -> Subject:
    return Subject(
        slug="example-app",
        bindings=("github:example/app?labels=partner:pat",),
        policy=Policy(
            people={"github:pat": "pat"},
            roles={"pat": "partner", "sam": "observer"},
            claim_labels={"partner:pat": "pat"},
            waiting_labels=waiting_labels or {},
        ),
        **fields,
    )


def _issue(labels=()) -> Issue:
    return Issue(REPO, 12, "Export", "pat", "", T0, T0, "open", labels=tuple(labels))


def _case(state, *, reporter="pat") -> Case:
    return Case(
        id="example-app-1",
        subject="example-app",
        conversations=(ISSUE,),
        reporter=reporter,
        state=state,
        created_at=T0,
        updated_at=T0,
    )


class _Untouchable:
    """A labeler that fails the test on any use at all."""

    def __getattr__(self, name):
        raise AssertionError(f"a dry run used labeler.{name}")


# ---- projection ----


def test_a_case_waiting_on_its_reporter_carries_their_label_beside_the_state_label():
    fake = FakeGitHub([_issue(labels=("partner:pat", "liaise:working", "needs-sam", "bug"))])

    lines = project_labels(_case("needs-partner"), _subject_with(LABELS), labeler=fake)

    assert set(fake.get_issue(REPO, 12).labels) == {"partner:pat", "bug", "liaise:needs-partner", "needs-pat"}
    assert lines == [f"labelled {ISSUE} liaise:needs-partner and needs-pat"]


@pytest.mark.parametrize("state", [state for state in CASE_STATES if state not in WAITING_STATES])
def test_a_case_that_waits_on_no_person_carries_no_waiting_label(state):
    fake = FakeGitHub([_issue(labels=("needs-pat", "needs-sam", "partner:pat"))])

    project_labels(_case(state), _subject_with(LABELS), labeler=fake)

    assert set(fake.get_issue(REPO, 12).labels) == {"partner:pat", f"liaise:{state}"}


def test_without_waiting_labels_a_label_set_by_hand_is_left_alone():
    fake = FakeGitHub([_issue(labels=("needs-pat",))])

    lines = project_labels(_case("needs-partner"), _subject_with(), labeler=fake)

    assert set(fake.get_issue(REPO, 12).labels) == {"needs-pat", "liaise:needs-partner"}
    assert lines == [f"labelled {ISSUE} liaise:needs-partner"]


def test_a_reporter_the_policy_gives_no_label_has_none():
    subject = _subject_with({"sam": "needs-sam"})
    assert waiting_label(_case("needs-partner"), subject) is None
    assert waiting_label(_case("needs-partner", reporter="sam"), subject) == "needs-sam"


def test_a_dry_run_says_what_it_would_label_and_touches_nothing():
    lines = project_labels(_case("needs-partner"), _subject_with(LABELS), labeler=_Untouchable(), dry_run=True)

    assert lines == [
        f"would label {ISSUE} liaise:needs-partner and needs-pat, removing any other liaise: state label "
        f"and waiting label"
    ]


def test_a_stale_waiting_label_comes_off_but_never_a_claim_label():
    fake = FakeGitHub([_issue(labels=("old-waiting", "partner:pat"))])

    project_labels(_case("needs-partner"), _subject_with(LABELS), labeler=fake, stale=["old-waiting", "partner:pat"])

    assert set(fake.get_issue(REPO, 12).labels) == {"partner:pat", "liaise:needs-partner", "needs-pat"}


def test_setup_creates_the_waiting_labels_with_the_state_they_go_with():
    fake = FakeGitHub()

    lines = setup_labels(fake, _subject_with(LABELS))

    created = fake.labels_created(REPO)
    assert {"needs-pat", "needs-sam"} <= set(created) and "sam" in created["needs-sam"]
    assert lines == [f"{REPO}: created 1 claim label(s), 2 waiting label(s) and 7 state labels (liaise:<state>)"]


# ---- the subject file ----


def _load(tmp_path, waiting, *, top=""):
    path = tmp_path / "example-app.toml"
    path.write_text(top + SUBJECT_TOML.format(waiting=waiting))
    return load_subject(path)


def test_waiting_labels_are_off_by_default_a_table_or_true_for_everyone_with_a_role(tmp_path):
    assert _load(tmp_path, "").policy.waiting_labels == {}
    assert _load(tmp_path, "waiting_labels = false").policy.waiting_labels == {}
    assert _load(tmp_path, 'waiting_labels = { pat = "waits-on-pat" }').policy.waiting_labels == {"pat": "waits-on-pat"}
    assert _load(tmp_path, "waiting_labels = true").policy.waiting_labels == {"pat": "needs-pat", "sam": "needs-sam"}


@pytest.mark.parametrize(
    "waiting, top, message",
    [
        ('waiting_labels = "yes"', "", "must be true, false or a table of person to label"),
        ('waiting_labels = { cy = "needs-cy" }', "", "has a label for 'cy', who has no entry in policy.roles"),
        ('waiting_labels = { pat = "partner:pat" }', "", "which is also a claim label"),
        ('waiting_labels = { pat = "needs-x", sam = "needs-x" }', "", "the same label 'needs-x'"),
        ('waiting_labels = { pat = "liaise:needs-owner" }', "", "which is a state label"),
        ('waiting_labels = { pat = "helper:paused" }', 'label_prefix = "helper:"\n', "which is a state label"),
        ('waiting_labels = { pat = " " }', "", "gives 'pat' an empty label"),
    ],
)
def test_waiting_labels_that_could_not_work_are_refused(tmp_path, waiting, top, message):
    with pytest.raises(ConfigError, match=message):
        _load(tmp_path, waiting, top=top)


# ---- the tick ----


@pytest.fixture
def world(tmp_path) -> World:
    workspace = tmp_path / "code" / "example-app"
    workspace.mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    return World(tmp_path=tmp_path, workspace=workspace, sessions=sessions, subject=_subject(workspace))


def test_the_tick_relabels_a_waiting_case_once_its_subject_turns_waiting_labels_on(world):
    world.issue()
    world.tick()
    world.tick(LATER)  # the run's question went to the partner
    assert world.case().state == "needs-partner" and "needs-pat" not in world.labels()

    policy = replace(world.subject.policy, waiting_labels={"pat": "needs-pat"})
    world.subject = replace(world.subject, policy=policy)
    world.tick(LATER + timedelta(minutes=1))

    assert {"liaise:needs-partner", "needs-pat"} <= set(world.labels())
    projections = [entry for entry in world.case(CASE_1).entries if entry.kind == "projection"]
    assert projections[-1].detail == {"state": "needs-partner", "waiting": "needs-pat"}
    world.tick(LATER + timedelta(minutes=2))
    assert len([entry for entry in world.case(CASE_1).entries if entry.kind == "projection"]) == len(projections)


def test_a_renamed_or_turned_off_waiting_label_is_taken_off_the_issue(world):
    world.issue()
    world.tick()
    world.tick(LATER)
    labels = {"pat": "needs-pat"}
    for minutes, waiting in ((1, {"pat": "needs-pat"}), (2, {"pat": "waits-on-pat"}), (3, {})):
        world.subject = replace(world.subject, policy=replace(world.subject.policy, waiting_labels=waiting))
        world.tick(LATER + timedelta(minutes=minutes))
        labels = set(world.labels())
        assert "liaise:needs-partner" in labels
        assert labels & {"needs-pat", "waits-on-pat"} == set(waiting.values())
    last = [entry for entry in world.case(CASE_1).entries if entry.kind == "projection"][-1]
    assert last.detail == {"state": "needs-partner"}
