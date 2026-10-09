"""Tests for liaise.review: partner pull requests reviewed, posted, labelled and merged over fakes.

The same shape as test_tick's World: one subject with a [review] table, a FakeGitHub
seeded with pull requests (the labeler, and the reviewer's eyes on GitHub), a fake
correspond channel for the audience, an EchoProcessor whose runs return a scripted
verdict as structured output, a dict store and an explicit clock. Nothing reads a real
repository, runs claude or gh, or posts anywhere.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from liaise import tick as tick_module
from liaise.cases import set_case_state
from liaise.config import ConfigError, GlobalConfig
from liaise.github import FakeGitHub, Pull
from liaise.ledger import Ledger
from liaise.model import PR_STATES, PULL_KIND, RunResult
from liaise.notify import (
    NOTICE_DAILY_CAP,
    NOTICE_DIVERTED,
    NOTICE_ERROR,
    NOTICE_MERGE_FAILED,
    NOTICE_PR_MERGED,
    NOTICE_REVIEW_POSTED,
)
from liaise.processor import EchoProcessor
from liaise.projection import setup_labels
from liaise.review import (
    REVIEW_FOOTER,
    REVIEW_SCHEMA,
    TOO_LARGE_SUMMARY,
    Finding,
    Review,
    merge_blockers,
    parse_review,
    release_review_draft,
    review_body,
    review_drafts,
    review_list_lines,
    review_show_lines,
)
from liaise.subjects import (
    BudgetPolicy,
    Delivery,
    Policy,
    ReviewPolicy,
    Subject,
    Workspace,
    load_subject,
)
from liaise.testing import FakeGitHubChannel, demo_registry
from liaise.tick import run_once

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=1)
LATER = NOW + timedelta(minutes=5)
SLUG = "example-app"
REPO = "example/app"
LIB = "example/lib"
BINDING = "github:example/app?labels=partner:pat"
PR_7 = "github:example/app#7"
CASE_1 = "example-app-1"
SHA_A = "a" * 40
SHA_B = "b" * 40
RUN_SUFFIX = "0a1b2c3d"
DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"

CHANGES = RunResult(
    run_id="",
    structured={
        "verdict": "changes",
        "summary": "Nearly there: the export still drops the last row when it is empty.",
        "findings": [
            {"file": "x.py", "line": 1, "severity": "block", "note": "an empty list raises"}
        ],
        "for_owner": "The tests do not cover the empty case at all.",
    },
    summary="reviewed",
)
APPROVED = RunResult(
    run_id="",
    structured={"verdict": "approve", "summary": "Looks right, and the tests cover it."},
    summary="reviewed",
)
DECLINED = RunResult(
    run_id="",
    structured={
        "verdict": "decline",
        "summary": "This changes how billing works, which only the maintainer can decide.",
        "for_owner": "It touches billing.",
    },
)


def _run_id(case_id: str, number: int) -> str:
    return f"{case_id}-r{number}-{RUN_SUFFIX}"


RUN_1 = _run_id(CASE_1, 1)


@pytest.fixture(autouse=True)
def fixed_run_suffix(monkeypatch):
    monkeypatch.setattr(
        tick_module, "uuid4", lambda: SimpleNamespace(hex=RUN_SUFFIX.ljust(32, "0"))
    )


@pytest.fixture(autouse=True)
def no_real_acquaint(monkeypatch):
    monkeypatch.setitem(sys.modules, "acquaint", None)


def _subject(workspace: Path, *, reply_mode="direct", review=None, budget=None) -> Subject:
    return Subject(
        slug=SLUG,
        bindings=(BINDING,),
        policy=Policy(
            people={"github:pat": "pat", "github:sam": "sam"},
            roles={"pat": "partner", "sam": "observer"},
            relays=("github:example-bot",),
            claim_labels={"partner:pat": "pat"},
            default_reply_mode=reply_mode,
            budget=budget or BudgetPolicy(),
        ),
        workspace=Workspace(path=str(workspace)),
        delivery=Delivery(kind="pr_only"),
        review=review if review is not None else ReviewPolicy(authors=("pat",)),
    )


def _pull(number=7, *, author="pat", sha=SHA_A, repo=REPO, **fields) -> Pull:
    options = dict(
        title="Keep the last row",
        body="Fixes the export.",
        base="main",
        created_at=T0,
        updated_at=T0,
        checks="success",
        mergeable=True,
        url=f"https://github.com/{repo}/pull/{number}",
    )
    options.update(fields)
    return Pull(repo=repo, number=number, author=author, head_sha=sha, **options)


@dataclass
class World:
    tmp_path: Path
    workspace: Path
    sessions: Path
    subject: Subject
    github: FakeGitHubChannel = field(default_factory=FakeGitHubChannel)
    labeler: FakeGitHub = field(default_factory=FakeGitHub)
    processor: EchoProcessor = field(
        default_factory=lambda: EchoProcessor(results={CASE_1: CHANGES})
    )
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

    def pull(self, number=7, *, diff=DIFF, **fields) -> Pull:
        found = _pull(number, **fields)
        self.labeler.seed_pull(found, diff=diff)
        return found

    def labels(self, number=7, repo=REPO):
        return self.labeler.get_pull(repo, number).labels

    def case(self, case_id=CASE_1):
        return self.ledger.get_case(case_id)

    def events(self, case_id=CASE_1):
        return [
            (e.kind, e.detail.get("event") or e.detail.get("verdict") or e.detail.get("to"))
            for e in self.case(case_id).entries
        ]

    def bodies(self):
        return [body for _, body, _ in self.notes]


@pytest.fixture
def world(tmp_path) -> World:
    workspace = tmp_path / "code" / "example-app"
    workspace.mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    return World(tmp_path=tmp_path, workspace=workspace, sessions=sessions, subject=_subject(workspace))


# ---- the loop: open, review once per head, post, label ----


def test_a_partner_pull_request_is_reviewed_once_and_the_verdict_posted(world):
    world.pull()
    first = world.tick()

    assert first.problems == ()
    assert first.dispatched == (RUN_1,)
    case = world.case()
    assert (case.kind, case.state, case.reporter, case.conversations) == (PULL_KIND, "pr-reviewing", "pat", (PR_7,))
    (job,) = world.processor.jobs
    assert (job.run_id, job.case_id, job.session_id) == (RUN_1, CASE_1, None)
    assert (job.cwd, job.json_schema) == (str(world.workspace.resolve()), REVIEW_SCHEMA)
    assert "+new" in job.prompt and "Keep the last row" in job.prompt and "@pat" in job.prompt
    assert "Assume the change is wrong" in job.prompt
    assert "Do not post a review" in job.prompt
    assert world.labels() == ("liaise:pr-reviewing",)
    assert world.ledger.daily_count(SLUG, NOW.date()) == 1
    assert world.labeler.reviews == []

    second = world.tick(LATER)

    assert second.problems == ()
    assert second.collected == (RUN_1,)
    ((repo, number, body, event),) = world.labeler.reviews
    assert (repo, number, event) == (REPO, 7, "REQUEST_CHANGES")
    assert body.startswith("@pat Nearly there")
    assert "- x.py:1 (block): an empty list raises" in body
    assert body.endswith(REVIEW_FOOTER)
    assert "The tests do not cover" not in body  # for_owner never reaches the pull request
    case = world.case()
    assert case.state == "pr-changes"
    assert world.labels() == ("liaise:pr-changes",)
    (review,) = [e for e in case.entries if e.kind == "review"]
    assert (review.detail["verdict"], review.detail["head_sha"], review.detail["run_id"]) == ("changes", SHA_A, RUN_1)
    (note,) = [e for e in case.entries if e.kind == "note"]
    assert note.text == "The tests do not cover the empty case at all."
    titles = [title for title, _, _ in world.notes]
    assert titles == [f"liaise: {CASE_1} reviewed"]
    assert "cause: changes, with a note for you" in world.bodies()[0]
    assert "The tests do not cover" not in world.bodies()[0]

    # the same head commit is never reviewed twice
    third = world.tick(LATER + timedelta(hours=1))
    assert third.dispatched == () and len(world.labeler.reviews) == 1
    assert any("reviewed" in line for line in third.plan_lines)


def test_a_new_push_gets_a_new_review_from_a_fresh_session(world):
    world.pull()
    world.tick()
    world.tick(LATER)
    world.pull(sha=SHA_B)  # the partner pushed again
    world.processor = EchoProcessor(results={CASE_1: APPROVED})

    fourth = world.tick(LATER + timedelta(minutes=10))
    run_2 = _run_id(CASE_1, 2)
    assert fourth.dispatched == (run_2,)
    assert world.case().state == "pr-reviewing"
    (job,) = world.processor.jobs
    assert job.session_id is None and SHA_B in job.prompt

    fifth = world.tick(LATER + timedelta(minutes=15))
    assert fifth.collected == (run_2,)
    assert [event for _, _, _, event in world.labeler.reviews] == ["REQUEST_CHANGES", "APPROVE"]
    assert world.case().state == "pr-approved"
    assert world.labels() == ("liaise:pr-approved",)
    assert world.labeler.merges == []  # merge is off


def test_a_decline_is_a_comment_and_the_owner_hears_why(world):
    world.processor = EchoProcessor(results={CASE_1: DECLINED})
    world.pull()
    world.tick()
    world.tick(LATER)
    ((_, _, body, event),) = world.labeler.reviews
    assert event == "COMMENT" and "billing" in body
    assert world.case().state == "pr-declined"
    assert world.labels() == ("liaise:pr-declined",)


def test_pull_requests_by_others_drafts_and_other_repositories_are_left_alone(world):
    world.pull(7, author="sam")  # an observer: not a reviewed author
    world.pull(8, author="stranger")
    world.pull(9, is_draft=True)
    world.pull(10, repo=LIB)  # not a reviewed repository
    report = world.tick()

    assert report.dispatched == ()
    assert list(world.ledger.cases()) == [] or [c.conversations for c in world.ledger.cases()] == [("github:example/app#9",)]
    assert any("a draft, not reviewed" in line for line in report.plan_lines)
    assert world.labels(7) == () and world.labels(8) == () and world.labels(10, LIB) == ()


def test_extra_repositories_and_person_ids_in_the_review_table(world):
    world.subject = _subject(world.workspace, review=ReviewPolicy(authors=("pat",), repos=(LIB,)))
    world.pull(3, repo=LIB)
    report = world.tick()
    assert report.dispatched == (RUN_1,)
    (job,) = world.processor.jobs
    assert job.cwd.endswith("example%2Flib")  # no checkout of that repository: a scratch directory
    assert "No checkout of this repository is available" in job.prompt
    assert world.labels(3, LIB) == ("liaise:pr-reviewing",)


# ---- merging ----


def test_squash_merge_waits_for_the_veto_window_then_merges_the_approved_head(world):
    world.subject = _subject(world.workspace, review=ReviewPolicy(authors=("pat",), merge="squash", veto_minutes=60))
    world.processor = EchoProcessor(results={CASE_1: APPROVED})
    world.pull()
    world.tick()
    approved_at = LATER
    world.tick(approved_at)
    assert world.case().state == "pr-approved" and world.labeler.merges == []

    soon = world.tick(approved_at + timedelta(minutes=30))
    assert world.labeler.merges == []
    assert any("veto window: 30m to go" in line for line in soon.plan_lines)

    done = world.tick(approved_at + timedelta(minutes=61))
    assert world.labeler.merges == [(REPO, 7, "squash", SHA_A)]
    assert world.case().state == "pr-merged"
    assert world.labels() == ("liaise:pr-merged",)
    assert done.problems == ()
    titles = [title for title, _, _ in world.notes]
    assert titles == [f"liaise: {CASE_1} reviewed", f"liaise: {CASE_1} merged"]


@pytest.mark.parametrize(
    "change, expected",
    [
        (dict(labels=("liaise:hold",)), "the liaise:hold label is on it"),
        (dict(checks="pending"), "checks: pending"),
        (dict(checks="failure"), "checks: failure"),
        (dict(is_draft=True), "a draft, not reviewed"),
        (dict(mergeable=False), "cannot be merged"),
        (dict(mergeable=None), "has not said"),
    ],
)
def test_what_keeps_an_approved_pull_request_from_merging(world, change, expected):
    world.subject = _subject(world.workspace, review=ReviewPolicy(authors=("pat",), merge="squash", veto_minutes=0))
    world.processor = EchoProcessor(results={CASE_1: APPROVED})
    world.pull()
    world.tick()
    world.tick(LATER)
    world.pull(**change)  # what GitHub says now

    report = world.tick(LATER + timedelta(minutes=5))
    assert world.labeler.merges == []
    assert world.case().state == "pr-approved" or change.get("is_draft")
    assert any(expected in line for line in report.plan_lines)


def test_a_push_after_approval_is_not_merged_but_reviewed_again(world):
    world.subject = _subject(world.workspace, review=ReviewPolicy(authors=("pat",), merge="squash", veto_minutes=0))
    world.processor = EchoProcessor(results={CASE_1: APPROVED})
    world.pull()
    world.tick()
    world.tick(LATER)
    world.pull(sha=SHA_B)

    report = world.tick(LATER + timedelta(minutes=5))
    assert world.labeler.merges == []
    assert report.dispatched == (_run_id(CASE_1, 2),)
    assert world.case().state == "pr-reviewing"


def test_a_repository_with_no_checks_merges_and_require_checks_false_ignores_them(world):
    world.subject = _subject(world.workspace, review=ReviewPolicy(authors=("pat",), merge="squash", veto_minutes=0, require_checks=False))
    world.processor = EchoProcessor(results={CASE_1: APPROVED})
    world.pull(checks="failure")
    world.tick()
    world.tick(LATER)
    world.tick(LATER + timedelta(minutes=1))
    assert world.labeler.merges == [(REPO, 7, "squash", SHA_A)]


def test_a_failed_merge_is_a_notice_and_is_not_retried_for_that_head(world):
    world.subject = _subject(world.workspace, review=ReviewPolicy(authors=("pat",), merge="squash", veto_minutes=0))
    world.processor = EchoProcessor(results={CASE_1: APPROVED})
    world.pull()
    world.tick()
    world.tick(LATER)
    world.labeler.merge_error = "Pull request is not mergeable: branch protection"

    failed = world.tick(LATER + timedelta(minutes=1))
    assert world.labeler.merges == []
    assert any("merging failed" in problem for problem in failed.problems)
    assert [title for title, _, _ in world.notes][-1] == f"liaise: {CASE_1} did not merge"
    assert "branch protection" not in "".join(world.bodies())

    world.labeler.merge_error = None
    again = world.tick(LATER + timedelta(minutes=2))
    assert world.labeler.merges == []
    assert any("a merge of this head already failed" in line for line in again.plan_lines)
    assert world.case().state == "pr-approved"


def test_a_pull_request_merged_elsewhere_moves_to_merged(world):
    world.processor = EchoProcessor(results={CASE_1: APPROVED})
    world.pull()
    world.tick()
    world.tick(LATER)
    world.pull(state="merged")
    world.tick(LATER + timedelta(minutes=1))
    assert world.case().state == "pr-merged"
    assert world.labeler.merges == []


def test_merge_blockers_lists_every_reason_at_once():
    policy = ReviewPolicy(authors=("pat",), merge="off")
    case = Ledger({}).new_case(SLUG, PR_7, reporter="pat", at=T0, kind=PULL_KIND)
    reasons = merge_blockers(_pull(is_draft=True, checks="pending"), case, policy, now=NOW, hold="liaise:hold")
    assert reasons[:3] == ["merge is off for this subject", "not approved (pr-reviewing)", "no review recorded"]
    assert "it is a draft" in reasons and "checks: pending" in reasons


# ---- budgets, sizes, errors ----


def test_the_daily_cap_is_shared_with_the_issue_cases_and_the_owner_told_once(world):
    world.subject = _subject(world.workspace, budget=BudgetPolicy(daily_dispatches=1, concurrent=2))
    world.pull(7)
    world.pull(8)
    report = world.tick()

    assert report.dispatched == (RUN_1,)
    assert any("over the daily cap (1/1)" in line for line in report.plan_lines)
    assert [title for title, _, _ in world.notes] == [f"liaise: {SLUG} reached its daily cap"]
    assert world.case("example-app-2").state == "pr-reviewing"
    assert world.labeler.reviews == []

    world.tick(LATER)  # still capped: no second notification today
    assert len([t for t, _, _ in world.notes if "daily cap" in t]) == 1


def test_a_diff_over_max_diff_lines_asks_to_split_without_a_run(world):
    world.subject = _subject(world.workspace, review=ReviewPolicy(authors=("pat",), max_diff_lines=3))
    world.pull()
    report = world.tick()

    assert report.dispatched == () and world.processor.jobs == []
    ((_, _, body, event),) = world.labeler.reviews
    assert event == "REQUEST_CHANGES"
    assert body.startswith("@pat " + TOO_LARGE_SUMMARY.format(lines=6, limit=3))
    assert world.case().state == "pr-changes"
    assert world.ledger.daily_count(SLUG, NOW.date()) == 0
    (review,) = [e for e in world.case().entries if e.kind == "review"]
    assert review.detail["run_id"] is None


def test_a_crashed_review_is_the_owners_and_that_head_is_not_reviewed_again(world):
    world.processor = EchoProcessor(results={CASE_1: RunResult(run_id="", error="crashed")})
    world.pull()
    world.tick()
    second = world.tick(LATER)
    assert second.collected == (RUN_1,)
    assert world.case().state == "pr-reviewing"
    assert [title for title, _, _ in world.notes] == [f"liaise: {CASE_1} crashed"]
    third = world.tick(LATER + timedelta(minutes=5))
    assert third.dispatched == ()
    assert any("ended in crashed; waiting for a new push" in line for line in third.plan_lines)
    assert world.labeler.reviews == []


def test_a_rate_limited_review_is_deferred_then_tried_again_without_counting(world):
    world.processor = EchoProcessor(results={CASE_1: RunResult(run_id="", error="rate_limited")})
    world.pull()
    world.tick()
    world.tick(LATER)
    assert world.ledger.daily_count(SLUG, NOW.date()) == 0
    assert world.case().defer_until is not None
    deferred = world.tick(LATER + timedelta(seconds=30))
    assert deferred.dispatched == ()
    world.processor = EchoProcessor(results={CASE_1: APPROVED})
    retried = world.tick(LATER + timedelta(minutes=5))
    assert retried.dispatched == (_run_id(CASE_1, 2),)


def test_a_result_that_is_not_a_verdict_is_needs_human(world):
    world.processor = EchoProcessor(results={CASE_1: RunResult(run_id="", structured={"verdict": "maybe"})})
    world.pull()
    world.tick()
    second = world.tick(LATER)
    assert any("the review result is not valid" in problem for problem in second.problems)
    assert [title for title, _, _ in world.notes] == [f"liaise: {CASE_1} needs_human"]
    assert world.labeler.reviews == []


# ---- the gate ----


def test_in_draft_mode_the_verdict_waits_for_the_owner_who_posts_it(world):
    world.subject = _subject(world.workspace, reply_mode="draft")
    world.pull()
    world.tick()
    report = world.tick(LATER)

    assert world.labeler.reviews == []
    case = world.case()
    assert case.state == "pr-reviewing"
    ((index, draft),) = review_drafts(case)
    assert (index, draft["event"], draft["verdict"], draft["head_sha"]) == (0, "REQUEST_CHANGES", "changes", SHA_A)
    assert "draft reply mode" in draft["reason"]
    assert (report.diverted[0].reason, [t for t, _, _ in world.notes]) == (draft["reason"], [f"liaise: a draft for {CASE_1} waits for you"])

    # the owner reads it and releases it: judged with their approval of what they saw
    shown, approval = release_review_draft(
        world.subject, case, draft, ledger=world.ledger, github=world.labeler, now=LATER,
        registry=demo_registry(github=world.github), approve_shown=True,
    )
    assert shown.sent and approval is not None and world.labeler.reviews == []  # judged as a release would be; nothing posted
    posted, _ = release_review_draft(
        world.subject, case, draft, ledger=world.ledger, github=world.labeler, now=LATER,
        registry=demo_registry(github=world.github), approval=approval,
    )
    assert posted.sent
    ((_, _, body, event),) = world.labeler.reviews
    assert event == "REQUEST_CHANGES" and body.startswith("@pat Nearly there")
    case = world.case()
    assert case.state == "pr-changes" and case.drafts == ()


def test_a_posting_github_refuses_keeps_the_verdict_as_a_draft(world):
    class Refusing(FakeGitHub):
        def post_review(self, repo, number, body, *, event):
            raise RuntimeError("502")

    world.labeler = Refusing()
    world.pull()
    world.tick()
    report = world.tick(LATER)
    assert any("not posted" in line for line in report.plan_lines)
    assert world.case().state == "pr-reviewing"
    assert len(review_drafts(world.case())) == 1
    assert [t for t, _, _ in world.notes] == [f"liaise: a message for {CASE_1} was not sent"]


def test_a_dry_run_plans_the_review_and_changes_nothing(world):
    world.pull()
    snapshot = dict(world.store)
    report = world.tick(dry_run=True)
    assert report.dry_run and report.dispatched == (RUN_1,)
    assert any("would dispatch a review of head aaaaaaaa" in line for line in report.plan_lines)
    assert world.store == snapshot and world.processor.jobs == [] and world.labels() == ()


# ---- intake, labels, states ----


def test_a_labelled_pull_request_opens_no_issue_case_at_intake(world):
    world.github.add_issue(REPO, 7, author="pat", title="Keep the last row", body="x", labels=("partner:pat",), created_at=T0, kind="pull_request")
    world.pull()
    report = world.tick()
    assert any(line.startswith(f"intake {SLUG}: 0 new events (1 ignored)") for line in report.plan_lines)
    case = world.case()
    assert case.kind == PULL_KIND and case.state == "pr-reviewing"
    assert len(list(world.ledger.cases())) == 1


def test_projecting_a_pull_label_never_touches_issue_labels_and_setup_creates_them(world):
    world.pull(labels=("liaise:working", "bug"))
    world.tick()
    assert world.labels() == ("liaise:working", "bug", "liaise:pr-reviewing")
    created = setup_labels(world.labeler, world.subject)
    assert all(f"liaise:{state}" in world.labeler.labels_created(REPO) for state in PR_STATES)
    assert "liaise:hold" in world.labeler.labels_created(REPO)


def test_the_operator_moves_a_review_case_only_within_its_own_vocabulary(world):
    world.pull()
    world.tick()
    with pytest.raises(ValueError, match="pull case, whose states are"):
        set_case_state(world.ledger, CASE_1, "intake", now=LATER)
    with pytest.raises(ValueError, match="in flight"):
        set_case_state(world.ledger, CASE_1, "pr-declined", now=LATER)
    world.tick(LATER)
    assert set_case_state(world.ledger, CASE_1, "pr-declined", now=LATER).state == "pr-declined"


# ---- the pure parts ----


def test_parse_review_and_body():
    review = parse_review({"verdict": "approve", "summary": " Fine. ", "findings": [], "for_owner": ""})
    assert review == Review("approve", "Fine.")
    assert review_body(review, footer="") == "Fine."
    with pytest.raises(ValueError, match="findings\\[0\\].severity"):
        parse_review({"verdict": "changes", "summary": "x", "findings": [{"file": "a", "severity": "huge", "note": "n"}]})
    with pytest.raises(ValueError, match="expected an object"):
        parse_review(["not", "an", "object"])
    assert Finding("a.py", "nit", "n").where == "a.py"


def test_the_review_table_loads_with_its_defaults_and_refuses_bad_values(tmp_path):
    path = tmp_path / "example-app.toml"
    base = (
        'bindings = ["github:example/app?labels=partner:pat"]\n'
        "[policy]\n"
        'people = { "github:pat" = "pat", "github:Pat-Alt" = "pat", "github:sam" = "sam" }\n'
        'roles = { pat = "partner", sam = "observer" }\n'
    )
    path.write_text(base)
    assert load_subject(path).review is None

    path.write_text(base + "[review]\n")
    review = load_subject(path).review
    assert review == ReviewPolicy(authors=("pat", "Pat-Alt"))
    assert review.reviews("PAT") and not review.reviews("sam")

    path.write_text(base + '[review]\nauthors = ["sam", "ext-login"]\nrepos = ["example/lib"]\nmerge = "squash"\nveto_minutes = 5\nmax_diff_lines = 10\nrequire_checks = false\n')
    review = load_subject(path).review
    assert review.authors == ("sam", "ext-login") and review.repos == ("example/lib",)
    assert (review.merge, review.veto_minutes, review.max_diff_lines, review.require_checks) == ("squash", 5, 10, False)

    for bad, text in (
        ('merge = "rebase"', "review.merge"),
        ("veto_minutes = -1", "review.veto_minutes"),
        ("max_diff_lines = 0", "review.max_diff_lines"),
        ('repos = ["not-a-repo"]', "review.repos"),
        ('authors = ["nobody-with-a-role"]\nrepos = 3', "review.repos"),
    ):
        path.write_text(base + f"[review]\n{bad}\n")
        with pytest.raises(ConfigError, match=text):
            load_subject(path)
    path.write_text(base.replace('"github:pat" = "pat", "github:Pat-Alt" = "pat", ', "") + "[review]\n")
    with pytest.raises(ConfigError, match="names nobody"):
        load_subject(path)


def test_review_list_and_show_lines(world):
    world.pull()
    world.pull(8, is_draft=True, sha=SHA_B)
    lines = review_list_lines({SLUG: world.subject}, world.ledger, world.labeler)
    assert lines[0].startswith(f"subject {SLUG}: reviews pull requests by pat in {REPO} (merge: off)")
    assert f"  {PR_7} by @pat: Keep the last row [not yet taken in, checks success, head aaaaaaaa]" in lines
    assert any("#8" in line and "draft" in line for line in lines)
    world.tick()
    world.tick(LATER)
    lines = review_list_lines({SLUG: world.subject}, world.ledger, world.labeler)
    assert any(f"{PR_7} by @pat" in line and "[pr-changes, checks success" in line for line in lines)
    shown = review_show_lines(world.ledger, PR_7)
    assert shown[0] == f"reviews of {PR_7} ({CASE_1}, pr-changes): 1"
    assert any("head aaaaaaaa: changes" in line for line in shown)
    assert any("- x.py:1 (block): an empty list raises" in line for line in shown)
    assert "    for you:" in shown
    with pytest.raises(ValueError, match="no review case"):
        review_show_lines(world.ledger, "github:example/app#99")


# ---- the command line ----


def _config_root(tmp_path: Path, world: World, *, reply_mode="direct") -> Path:
    root = tmp_path / "config"
    (root / "subjects").mkdir(parents=True)
    (root / "config.toml").write_text(
        f'owner_login = "owner"\nstate_dir = "{(tmp_path / "state").as_posix()}"\n'
    )
    (root / "subjects" / f"{SLUG}.toml").write_text(
        f'bindings = ["{BINDING}"]\n'
        f'workspace = {{ path = "{world.workspace.as_posix()}" }}\n'
        'delivery = { kind = "pr_only" }\n'
        "[policy]\n"
        f'default_reply_mode = "{reply_mode}"\n'
        'people = { "github:pat" = "pat" }\n'
        'roles = { pat = "partner" }\n'
        "[review]\n"
    )
    return root


def test_review_list_show_and_post_commands(tmp_path, world):
    from liaise import cli

    world.subject = _subject(world.workspace, reply_mode="draft")
    root = _config_root(tmp_path, world, reply_mode="draft")
    world.pull()
    assert "not yet taken in" in cli.review_list(root=str(root), labeler=world.labeler, store=world.store)
    world.tick()
    world.tick(LATER)
    assert world.case().state == "pr-reviewing" and world.labeler.reviews == []

    shown = cli.review_show(REPO, 7, root=str(root), store=world.store)
    assert "verdicts held for you: 1" in shown and f"case: {CASE_1}" in shown

    registry = demo_registry(github=world.github)
    dry = cli.review_post(REPO, 7, root=str(root), registry=registry, labeler=world.labeler, store=world.store, now=LATER, dry_run=True)
    assert "dry run: nothing posted" in dry and "@pat Nearly there" in dry
    assert world.labeler.reviews == [] and world.case().state == "pr-reviewing"

    declined = cli.review_post(REPO, 7, root=str(root), registry=registry, labeler=world.labeler, store=world.store, now=LATER, confirm=lambda preview: False)
    assert declined == "not posted" and world.labeler.reviews == []

    posted = cli.review_post(REPO, 7, root=str(root), registry=registry, labeler=world.labeler, store=world.store, now=LATER, confirm=lambda preview: "REQUEST_CHANGES" in preview)
    assert posted.startswith(f"posted REQUEST_CHANGES on {PR_7}; {CASE_1} is now pr-changes")
    ((_, _, body, event),) = world.labeler.reviews
    assert event == "REQUEST_CHANGES" and body.startswith("@pat")
    assert world.case().drafts == ()

    import cw

    with pytest.raises(cw.CommandError, match="holds no verdict"):
        cli.review_post(REPO, 7, root=str(root), registry=registry, labeler=world.labeler, store=world.store, now=LATER)
    assert "liaise review list" in str(pytest.raises(cw.CommandError, cli.review_show, REPO, 99, root=str(root), store=world.store).value)
