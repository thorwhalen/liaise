"""Tests for liaise.projection: one state label per GitHub issue, and dry runs that touch nothing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from liaise.github import FakeGitHub, Issue
from liaise.model import CASE_STATES, Case
from liaise.projection import github_issue, github_repos, project_labels, setup_labels
from liaise.subjects import Policy, Subject

T0 = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
REPO = "example/app"


def _subject(**fields) -> Subject:
    return Subject(
        slug="pat",
        bindings=("github:example/app?labels=partner:pat",),
        policy=Policy(people={"github:pat": "pat"}, roles={"pat": "partner"}),
        **fields,
    )


def _issue(number=12, *, labels=()) -> Issue:
    return Issue(
        repo=REPO,
        number=number,
        title="Export",
        author="pat",
        body="",
        created_at=T0,
        updated_at=T0,
        state="open",
        labels=tuple(labels),
    )


def _case(state, *conversations) -> Case:
    return Case(
        id="pat-1",
        subject="pat",
        conversations=conversations or ("github:example/app#12",),
        reporter="pat",
        state=state,
        created_at=T0,
        updated_at=T0,
    )


def _state_labels(labels, prefix="liaise:"):
    return [label for label in labels if label.startswith(prefix) and label[len(prefix) :] in CASE_STATES]


class _Untouchable:
    """A labeler that fails the test on any use at all."""

    def __getattr__(self, name):
        raise AssertionError(f"a dry run used labeler.{name}")


@pytest.mark.parametrize(
    "before",
    [
        ("partner:pat", "liaise:intake", "bug"),
        ("partner:pat", "liaise:intake", "liaise:needs-owner"),  # an invariant already broken
        ("partner:pat",),
        ("partner:pat", "liaise:working"),
    ],
)
def test_exactly_one_state_label_is_left_and_it_is_the_current_state(before):
    fake = FakeGitHub([_issue(labels=before)])
    lines = project_labels(_case("working"), _subject(), labeler=fake)

    labels = fake.get_issue(REPO, 12).labels
    assert _state_labels(labels) == ["liaise:working"]
    assert [label for label in labels if label not in _state_labels(labels)] == [
        label for label in before if not label.startswith("liaise:")
    ]
    assert lines == ["labelled github:example/app#12 liaise:working"]


def test_a_dry_run_mutates_nothing():
    fake = FakeGitHub([_issue(labels=("partner:pat", "liaise:intake"))])
    before = fake.get_issue(REPO, 12)
    lines = project_labels(_case("working"), _subject(), labeler=fake, dry_run=True)

    assert fake.get_issue(REPO, 12) == before
    assert lines == [
        "would label github:example/app#12 liaise:working, removing any other liaise: state label"
    ]
    assert project_labels(_case("working"), _subject(), labeler=_Untouchable(), dry_run=True) == lines


def test_only_github_issue_conversations_are_projected():
    fake = FakeGitHub([_issue(labels=("liaise:intake",)), _issue(13, labels=("liaise:intake",))])
    case = _case("deployed", "webinbox:example-site#r1", "github:example/app#13", "github:example/app")
    lines = project_labels(case, _subject(), labeler=fake)

    assert lines == ["labelled github:example/app#13 liaise:deployed"]
    assert fake.get_issue(REPO, 12).labels == ("liaise:intake",)
    assert fake.get_issue(REPO, 13).labels == ("liaise:deployed",)


def test_the_subjects_label_prefix_is_used():
    fake = FakeGitHub([_issue(labels=("helper:intake", "liaise:intake"))])
    project_labels(_case("paused"), _subject(label_prefix="helper:"), labeler=fake)
    assert fake.get_issue(REPO, 12).labels == ("liaise:intake", "helper:paused")


def test_github_issue_parses_issue_refs_only():
    assert github_issue("github:example/app#12") == ("example/app", 12)
    assert github_issue("github:example/app") is None
    assert github_issue("webinbox:example-site") is None
    assert github_issue("fake:example/app#12") is None


# ---- setup_labels: the labels a subject needs, in each repository it binds ----


def _claiming_subject(*bindings, **fields) -> Subject:
    return Subject(
        slug="example-app",
        bindings=bindings or ("github:example/app?labels=partner:pat",),
        policy=Policy(
            people={"github:pat": "pat"},
            roles={"pat": "partner"},
            relays=("github:example-bot",),
            claim_labels={"partner:pat": "pat"},
        ),
        **fields,
    )


def test_setup_creates_the_claim_labels_and_every_state_label_in_each_bound_repository():
    """Ported from 0.0.x test_state: every state label, with a plain description, plus the
    routing label. 0.0.x also created `discovered`; the 0.1 rules forbid opening issues."""
    subject = _claiming_subject(
        "github:example/app?labels=partner:pat",
        "github:example/site#3",
        "github:example/app?author=pat",
        "webinbox:example-site",
        "github:example/*",
    )
    fake = FakeGitHub()
    lines = setup_labels(fake, subject)

    assert github_repos(subject) == ["example/app", "example/site"]
    for repo in ("example/app", "example/site"):
        created = fake.labels_created(repo)
        assert set(created) == {"partner:pat", *(f"liaise:{state}" for state in CASE_STATES)}
        assert all(created.values())  # each says what it is for
        assert "pat" in created["partner:pat"]
    assert [line.split(":")[0] for line in lines] == ["example/app", "example/site"]


def test_setup_is_idempotent_and_uses_the_subjects_label_prefix():
    subject = _claiming_subject(label_prefix="helper:")
    fake = FakeGitHub()
    setup_labels(fake, subject)
    first = fake.labels_created(REPO)
    setup_labels(fake, subject)
    assert fake.labels_created(REPO) == first
    assert "helper:needs-owner" in first and "liaise:needs-owner" not in first


def test_setup_for_a_subject_that_binds_no_github_repository_creates_nothing():
    lines = setup_labels(_Untouchable(), _claiming_subject("webinbox:example-site"))
    assert lines == ["example-app binds no GitHub repository: no labels to create"]
