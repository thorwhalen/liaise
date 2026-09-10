"""Tests for the GitHub seam: FakeGitHub's behavior, and that GhCli parses `gh`'s
JSON the same way. No network, no real repo: GhCli here shells out to a fake
`gh` script, so this is still offline and free.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from liaise.github import Comment, FakeGitHub, GhCli, GitHubError, Issue
from liaise.tests.conftest import write_executable_script

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
    # Real `gh issue view --json comments` shape (H-1): no `updatedAt` key on
    # a comment at all — only `createdAt` and `includesCreatedEdit` (whether
    # it was ever edited, not when). A fixture inventing `updatedAt` here is
    # exactly what let a real-world KeyError through the original test.
    "comments": [
        {
            "author": {"login": "pat"},
            "authorAssociation": "NONE",
            "body": "also the color",
            "createdAt": "2026-01-01T12:00:00Z",
            "includesCreatedEdit": False,
            "id": "IC_kwtest",
            "isMinimized": False,
            "minimizedReason": "",
            "reactionGroups": [],
            "url": "https://github.com/example/app/issues/7#issuecomment-1",
            "viewerDidAuthor": False,
        }
    ],
}


@pytest.fixture
def fake_gh_bin(tmp_path: Path) -> Path:
    """A fake `gh` executable script that returns canned JSON for `issue view`."""
    payload = json.dumps(_FAKE_GH_ISSUE_VIEW)
    return write_executable_script(
        tmp_path / "gh",
        f'import sys\nif "view" in sys.argv:\n    print({payload!r})\n    sys.exit(0)\nsys.exit(1)\n',
    )


def test_ghcli_parses_issue_view_json(fake_gh_bin: Path):
    gh = GhCli(gh_bin=str(fake_gh_bin))
    issue = gh.get_issue(REPO, 7)
    assert issue.number == 7
    assert issue.author == "pat"
    assert issue.state == "open"
    assert issue.labels == ("partner:pat", "liaise:intake")
    assert issue.comments[0].author == "pat"
    # H-1 regression: real `gh` output has no `updatedAt` on a comment, so the
    # parser falls back to `createdAt` rather than KeyError.
    assert issue.comments[0].updated_at == issue.comments[0].created_at
    assert issue.url == f"https://github.com/{REPO}/issues/7"


def test_ghcli_parses_a_comment_with_no_updated_at_key_at_all(tmp_path: Path):
    """H-1, isolated: the exact real-world shape (`updatedAt` absent from the
    comment) must not raise. Reverting the `c.get("updatedAt")` fallback in
    `_issue_from_json` back to `c["updatedAt"]` makes this raise KeyError.
    """
    payload = dict(_FAKE_GH_ISSUE_VIEW)
    payload["comments"] = [
        {k: v for k, v in _FAKE_GH_ISSUE_VIEW["comments"][0].items() if k != "updatedAt"}
    ]
    assert "updatedAt" not in payload["comments"][0]

    script = write_executable_script(
        tmp_path / "gh",
        f"import sys\nif 'view' in sys.argv:\n    print({json.dumps(payload)!r})\n    sys.exit(0)\nsys.exit(1)\n",
    )

    gh = GhCli(gh_bin=str(script))
    issue = gh.get_issue(REPO, 7)  # must not raise
    assert issue.comments[0].updated_at == issue.comments[0].created_at


def test_ghcli_raises_githuberror_on_failure(tmp_path: Path):
    script = write_executable_script(
        tmp_path / "gh", "import sys\nsys.stderr.write('boom')\nsys.exit(1)\n"
    )
    gh = GhCli(gh_bin=str(script))
    with pytest.raises(GitHubError, match="boom"):
        gh.get_issue(REPO, 1)
