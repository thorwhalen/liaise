"""The GitHub seam: one protocol, two implementations.

`liaise` never holds a token. All real access goes through the `gh` CLI, whose
auth belongs to the machine (:class:`GhCli`). Every other module in this
package talks to :class:`GitHub`, never to `gh` or `subprocess` directly, so
tests use :class:`FakeGitHub` — in-memory, no network, no real repo.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Iterable, Optional, Protocol, Sequence

#: JSON fields `gh issue list`/`gh issue view` are asked for. Kept as one SSOT
#: so the parser and the `gh` invocation never drift apart.
_ISSUE_FIELDS = "number,title,author,body,createdAt,updatedAt,state,labels,comments"


class GitHubError(Exception):
    """Raised when the `gh` CLI fails — its stderr is the message."""


@dataclass(frozen=True)
class Comment:
    """One issue comment."""

    author: str
    body: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Issue:
    """One GitHub issue, with its comments."""

    repo: str
    number: int
    title: str
    author: str
    body: str
    created_at: datetime
    updated_at: datetime
    state: str
    labels: tuple[str, ...] = field(default_factory=tuple)
    comments: tuple[Comment, ...] = field(default_factory=tuple)

    @property
    def url(self) -> str:
        """The issue's GitHub URL."""
        return f"https://github.com/{self.repo}/issues/{self.number}"


class GitHub(Protocol):
    """What `liaise` needs from GitHub. Implemented by :class:`GhCli` and :class:`FakeGitHub`."""

    def list_issues(
        self,
        repo: str,
        *,
        label: Optional[str] = None,
        author: Optional[str] = None,
        state: str = "open",
    ) -> list[Issue]:
        """List issues in `repo`, optionally filtered by label and/or author."""
        ...

    def get_issue(self, repo: str, number: int) -> Issue:
        """Read one issue, with its comments."""
        ...

    def add_labels(self, repo: str, number: int, labels: Sequence[str]) -> None:
        """Add one or more labels to an issue. No-op for a label already present."""
        ...

    def remove_labels(self, repo: str, number: int, labels: Sequence[str]) -> None:
        """Remove one or more labels from an issue. No-op for a label already absent."""
        ...

    def create_label(
        self, repo: str, name: str, *, color: str = "ededed", description: str = ""
    ) -> None:
        """Create a label if it does not already exist. Idempotent."""
        ...

    def post_comment(self, repo: str, number: int, body: str) -> None:
        """Post a comment on an issue."""
        ...

    def ensure_last_comment_mentions(
        self, repo: str, number: int, mention: str
    ) -> bool:
        """Repair this identity's own last comment on `number` to carry `mention`.

        If the last comment posted by liaise's own GitHub identity on this
        issue does not already contain `mention`, prepends it (body content
        otherwise untouched) and returns True. Returns False when the last
        such comment already contains `mention`, or when this identity has
        posted no comment on the issue at all. A rule the dispatched agent
        forgets must still hold (#20).
        """
        ...


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _issue_from_json(repo: str, raw: dict) -> Issue:
    return Issue(
        repo=repo,
        number=raw["number"],
        title=raw["title"],
        author=(raw.get("author") or {}).get("login", ""),
        body=raw.get("body", ""),
        created_at=_parse_dt(raw["createdAt"]),
        updated_at=_parse_dt(raw["updatedAt"]),
        state=raw.get("state", "open").lower(),
        labels=tuple(l["name"] for l in raw.get("labels", [])),
        comments=tuple(
            Comment(
                author=(c.get("author") or {}).get("login", ""),
                body=c.get("body", ""),
                created_at=_parse_dt(c["createdAt"]),
                # `gh issue view --json comments` does not emit `updatedAt` for
                # a comment (verified against real `gh` output) — only
                # `createdAt` and `includesCreatedEdit` (whether it was ever
                # edited, not when). Fall back to `createdAt` rather than
                # KeyError on every issue that has ever been commented on.
                updated_at=_parse_dt(c["updatedAt"])
                if c.get("updatedAt")
                else _parse_dt(c["createdAt"]),
            )
            for c in raw.get("comments", [])
        ),
    )


class GhCli:
    """The default :class:`GitHub`: every call shells out to the `gh` CLI.

    Auth belongs to whatever machine `gh` is configured on. This class never
    reads, stores or passes a token.
    """

    def __init__(self, *, gh_bin: str = "gh"):
        self.gh_bin = gh_bin

    def _run(self, *args: str) -> str:
        proc = subprocess.run([self.gh_bin, *args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise GitHubError(proc.stderr.strip() or proc.stdout.strip())
        return proc.stdout

    def list_issues(
        self,
        repo: str,
        *,
        label: Optional[str] = None,
        author: Optional[str] = None,
        state: str = "open",
    ) -> list[Issue]:
        args = [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            state,
            "--json",
            _ISSUE_FIELDS,
            "--limit",
            "1000",
        ]
        if label:
            args += ["--label", label]
        if author:
            args += ["--author", author]
        raw = json.loads(self._run(*args))
        return [_issue_from_json(repo, r) for r in raw]

    def get_issue(self, repo: str, number: int) -> Issue:
        raw = json.loads(
            self._run(
                "issue",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                _ISSUE_FIELDS,
            )
        )
        return _issue_from_json(repo, raw)

    def add_labels(self, repo: str, number: int, labels: Sequence[str]) -> None:
        if not labels:
            return
        self._run(
            "issue",
            "edit",
            str(number),
            "--repo",
            repo,
            *[flag for label in labels for flag in ("--add-label", label)],
        )

    def remove_labels(self, repo: str, number: int, labels: Sequence[str]) -> None:
        if not labels:
            return
        self._run(
            "issue",
            "edit",
            str(number),
            "--repo",
            repo,
            *[flag for label in labels for flag in ("--remove-label", label)],
        )

    def create_label(
        self, repo: str, name: str, *, color: str = "ededed", description: str = ""
    ) -> None:
        proc = subprocess.run(
            [
                self.gh_bin,
                "label",
                "create",
                name,
                "--repo",
                repo,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise GitHubError(proc.stderr.strip() or proc.stdout.strip())

    def post_comment(self, repo: str, number: int, body: str) -> None:
        self._run("issue", "comment", str(number), "--repo", repo, "--body", body)

    def _whoami(self) -> str:
        return self._run("api", "user", "--jq", ".login").strip()

    def ensure_last_comment_mentions(
        self, repo: str, number: int, mention: str
    ) -> bool:
        issue = self.get_issue(repo, number)
        me = self._whoami()
        mine = [c for c in issue.comments if c.author == me]
        if not mine:
            return False
        last = mine[-1]
        if mention in last.body:
            return False
        self._run(
            "issue",
            "comment",
            str(number),
            "--repo",
            repo,
            "--edit-last",
            "--body",
            f"{mention} {last.body}",
        )
        return True


class FakeGitHub:
    """In-memory :class:`GitHub`, for tests. No network, no real repo, no token.

    Every other test in this package should use this rather than :class:`GhCli`.
    """

    #: The fixed identity every comment posted through this fake carries —
    #: stands in for "whatever GitHub identity `gh` is authenticated as" in
    #: tests, so :meth:`ensure_last_comment_mentions` has something to match.
    SELF_AUTHOR = "liaise-bot"

    def __init__(self, issues: Optional[Iterable[Issue]] = None):
        self._issues: dict[tuple[str, int], Issue] = {
            (i.repo, i.number): i for i in (issues or [])
        }
        self._labels: dict[str, dict[str, str]] = {}  # repo -> {name: description}

    def seed(self, issue: Issue) -> None:
        """Add or replace an issue, for test setup."""
        self._issues[(issue.repo, issue.number)] = issue

    def list_issues(
        self,
        repo: str,
        *,
        label: Optional[str] = None,
        author: Optional[str] = None,
        state: str = "open",
    ) -> list[Issue]:
        result = [i for i in self._issues.values() if i.repo == repo]
        if state != "all":
            result = [i for i in result if i.state == state]
        if label is not None:
            result = [i for i in result if label in i.labels]
        if author is not None:
            result = [i for i in result if i.author == author]
        return sorted(result, key=lambda i: i.number)

    def get_issue(self, repo: str, number: int) -> Issue:
        try:
            return self._issues[(repo, number)]
        except KeyError:
            raise GitHubError(f"no such issue: {repo}#{number}") from None

    def add_labels(self, repo: str, number: int, labels: Sequence[str]) -> None:
        issue = self.get_issue(repo, number)
        new_labels = tuple(dict.fromkeys((*issue.labels, *labels)))
        self._issues[(repo, number)] = replace(issue, labels=new_labels)

    def remove_labels(self, repo: str, number: int, labels: Sequence[str]) -> None:
        issue = self.get_issue(repo, number)
        new_labels = tuple(l for l in issue.labels if l not in labels)
        self._issues[(repo, number)] = replace(issue, labels=new_labels)

    def create_label(
        self, repo: str, name: str, *, color: str = "ededed", description: str = ""
    ) -> None:
        self._labels.setdefault(repo, {})[name] = description

    def labels_created(self, repo: str) -> dict[str, str]:
        """Test helper: labels created (via :meth:`create_label`) for `repo`."""
        return dict(self._labels.get(repo, {}))

    def post_comment(self, repo: str, number: int, body: str) -> None:
        issue = self.get_issue(repo, number)
        now = datetime.now(timezone.utc)
        comment = Comment(
            author=self.SELF_AUTHOR, body=body, created_at=now, updated_at=now
        )
        self._issues[(repo, number)] = replace(
            issue, comments=(*issue.comments, comment)
        )

    def ensure_last_comment_mentions(
        self, repo: str, number: int, mention: str
    ) -> bool:
        issue = self.get_issue(repo, number)
        comments = list(issue.comments)
        for i in range(len(comments) - 1, -1, -1):
            if comments[i].author != self.SELF_AUTHOR:
                continue
            if mention in comments[i].body:
                return False
            comments[i] = replace(
                comments[i],
                body=f"{mention} {comments[i].body}",
                updated_at=datetime.now(timezone.utc),
            )
            self._issues[(repo, number)] = replace(issue, comments=tuple(comments))
            return True
        return False
