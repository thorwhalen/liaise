"""Tests for the GitHub seam: FakeGitHub's behavior, and that GhCli parses `gh`'s
JSON the same way. No network, no real repo: GhCli here shells out to a fake
`gh` script, so this is still offline and free.
"""

from __future__ import annotations

import json
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from liaise.github import Comment, FakeGitHub, GhCli, GitHubError, Issue

REPO = "example/app"


def _issue(number=1, *, author="pat", labels=(), comments=(), state="open"):
    return Issue(
        repo=REPO,
        number=number,
        title=f"issue {number}",
        author=author,
        body="something is broken",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        state=state,
        labels=labels,
        comments=comments,
    )


# ---- FakeGitHub: behavioral tests every other module's tests rely on ----


def test_fake_list_issues_filters_by_label_and_author():
    fake = FakeGitHub([
        _issue(1, author="pat", labels=("partner:pat",)),
        _issue(2, author="owner", labels=()),
        _issue(3, author="pat", labels=("partner:pat", "liaise:intake")),
    ])
    assert [i.number for i in fake.list_issues(REPO, label="partner:pat")] == [1, 3]
    assert [i.number for i in fake.list_issues(REPO, author="pat")] == [1, 3]
    assert [i.number for i in fake.list_issues(REPO, author="owner")] == [2]


def test_fake_list_issues_filters_by_state():
    fake = FakeGitHub([_issue(1, state="open"), _issue(2, state="closed")])
    assert [i.number for i in fake.list_issues(REPO)] == [1]
    assert [i.number for i in fake.list_issues(REPO, state="closed")] == [2]
    assert {i.number for i in fake.list_issues(REPO, state="all")} == {1, 2}


def test_fake_get_issue_roundtrips_comments_and_edit_timestamps():
    comment = Comment(
        author="pat",
        body="also this",
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 3, tzinfo=timezone.utc),  # edited later
    )
    fake = FakeGitHub([_issue(1, comments=(comment,))])
    got = fake.get_issue(REPO, 1)
    assert got.comments == (comment,)
    assert got.comments[0].updated_at > got.comments[0].created_at


def test_fake_get_issue_missing_raises():
    fake = FakeGitHub()
    with pytest.raises(GitHubError):
        fake.get_issue(REPO, 999)


def test_fake_add_and_remove_labels_idempotent():
    fake = FakeGitHub([_issue(1, labels=("liaise:intake",))])
    fake.add_labels(REPO, 1, ["partner:pat"])
    fake.add_labels(REPO, 1, ["partner:pat"])  # idempotent
    assert fake.get_issue(REPO, 1).labels == ("liaise:intake", "partner:pat")

    fake.remove_labels(REPO, 1, ["liaise:intake"])
    fake.remove_labels(REPO, 1, ["liaise:intake"])  # idempotent
    assert fake.get_issue(REPO, 1).labels == ("partner:pat",)


def test_fake_create_label_recorded():
    fake = FakeGitHub()
    fake.create_label(REPO, "liaise:working", description="a dispatch is running")
    assert fake.labels_created(REPO) == {"liaise:working": "a dispatch is running"}


def test_fake_post_comment_appends():
    fake = FakeGitHub([_issue(1)])
    fake.post_comment(REPO, 1, "hello")
    comments = fake.get_issue(REPO, 1).comments
    assert len(comments) == 1
    assert comments[0].body == "hello"


# ---- GhCli: parses `gh`'s JSON shape the same way FakeGitHub's model expects ----

_FAKE_GH_ISSUE_VIEW = {
    "number": 7,
    "title": "button is broken",
    "author": {"login": "pat"},
    "body": "the button does nothing",
    "createdAt": "2026-01-01T00:00:00Z",
    "updatedAt": "2026-01-02T00:00:00Z",
    "state": "OPEN",
    "labels": [{"name": "partner:pat"}, {"name": "liaise:intake"}],
    "comments": [
        {
            "author": {"login": "pat"},
            "body": "also the color",
            "createdAt": "2026-01-01T12:00:00Z",
            "updatedAt": "2026-01-01T12:05:00Z",
        }
    ],
}


@pytest.fixture
def fake_gh_bin(tmp_path: Path) -> Path:
    """A fake `gh` executable script that returns canned JSON for `issue view`."""
    script = tmp_path / "gh"
    payload = json.dumps(_FAKE_GH_ISSUE_VIEW)
    script.write_text(
        f"""#!{sys.executable}
import sys
if "view" in sys.argv:
    print({payload!r})
    sys.exit(0)
sys.exit(1)
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_ghcli_parses_issue_view_json(fake_gh_bin: Path):
    gh = GhCli(gh_bin=str(fake_gh_bin))
    issue = gh.get_issue(REPO, 7)
    assert issue.number == 7
    assert issue.author == "pat"
    assert issue.state == "open"
    assert issue.labels == ("partner:pat", "liaise:intake")
    assert issue.comments[0].author == "pat"
    assert issue.comments[0].updated_at > issue.comments[0].created_at
    assert issue.url == f"https://github.com/{REPO}/issues/7"


def test_ghcli_raises_githuberror_on_failure(tmp_path: Path):
    script = tmp_path / "gh"
    script.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('boom')\nsys.exit(1)\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    gh = GhCli(gh_bin=str(script))
    with pytest.raises(GitHubError, match="boom"):
        gh.get_issue(REPO, 1)
