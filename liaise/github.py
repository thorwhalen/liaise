"""The GitHub seam: one protocol, two implementations.

`liaise` never holds a token. All real access goes through the `gh` CLI, whose
auth belongs to the machine (:class:`GhCli`). Every other module in this
package talks to :class:`GitHub`, never to `gh` or `subprocess` directly, so
tests use :class:`FakeGitHub` — in-memory, no network, no real repo.

Issues are read and labelled through ``gh issue``; pull requests (:class:`Pull`, for
:mod:`liaise.review`) through ``gh pr``: listed, read with their head commit, checks and
mergeability, diffed, reviewed (``post_review``) and merged (``merge_pull``, bound to the
head commit it was approved at). A pull request is an issue to GitHub, so its labels go
through the same label verbs, with ``pull=True``.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Protocol, Sequence

#: JSON fields `gh issue list`/`gh issue view` are asked for. Kept as one SSOT
#: so the parser and the `gh` invocation never drift apart.
_ISSUE_FIELDS = "number,title,author,body,createdAt,updatedAt,state,labels,comments"
#: JSON fields `gh pr list`/`gh pr view` are asked for, likewise.
_PULL_FIELDS = (
    "number,title,author,body,createdAt,updatedAt,state,labels,url,"
    "headRefOid,baseRefName,isDraft,mergeable,statusCheckRollup"
)
#: The most open pull requests one ``gh pr list`` asks for.
PULL_LIST_LIMIT = 200
#: What a pull request's checks add up to (:attr:`Pull.checks`): all green, still running,
#: at least one failed, or no checks at all.
CHECKS_SUCCESS, CHECKS_PENDING, CHECKS_FAILURE, CHECKS_NONE = CHECK_STATES = (
    "success",
    "pending",
    "failure",
    "none",
)
#: A review's event, as the GitHub API names it.
REVIEW_EVENTS = ("APPROVE", "REQUEST_CHANGES", "COMMENT")
#: How ``gh pr review`` spells each review event.
_REVIEW_FLAGS = {
    "APPROVE": "--approve",
    "REQUEST_CHANGES": "--request-changes",
    "COMMENT": "--comment",
}
#: The merge methods ``gh pr merge`` offers.
MERGE_METHODS = ("squash", "merge", "rebase")
#: A check run's conclusion, or a status context's state, that counts as a failure.
_FAILED_CONCLUSIONS = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "ERROR", "STARTUP_FAILURE"}
)
#: ``gh``'s word for a pull request that is merged, in ``state``.
MERGED_STATE = "merged"


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


@dataclass(frozen=True)
class Pull:
    """One pull request, as a review needs it (see :mod:`liaise.review`).

    ``head_sha`` is the commit the review is of; ``base`` the branch it targets;
    ``mergeable`` GitHub's answer (None while it has not computed one); ``checks`` one of
    :data:`CHECK_STATES`; ``state`` ``open``, ``closed`` or ``merged``.
    """

    repo: str
    number: int
    title: str
    author: str
    body: str
    head_sha: str
    base: str
    created_at: datetime
    updated_at: datetime
    state: str = "open"
    is_draft: bool = False
    mergeable: Optional[bool] = None
    checks: str = CHECKS_NONE
    labels: tuple[str, ...] = field(default_factory=tuple)
    url: str = ""

    def __post_init__(self) -> None:
        if self.checks not in CHECK_STATES:
            raise ValueError(
                f"checks {self.checks!r} is not one of: {', '.join(CHECK_STATES)}"
            )

    @property
    def ref(self) -> str:
        """The encoded conversation reference liaise keys the review case on."""
        return f"github:{self.repo}#{self.number}"


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

    def add_labels(
        self, repo: str, number: int, labels: Sequence[str], *, pull: bool = False
    ) -> None:
        """Add one or more labels to an issue, or with ``pull`` a pull request. No-op for a label already present."""
        ...

    def remove_labels(
        self, repo: str, number: int, labels: Sequence[str], *, pull: bool = False
    ) -> None:
        """Remove one or more labels from an issue, or with ``pull`` a pull request. No-op for a label already absent."""
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

    def list_pulls(
        self, repo: str, *, author: Optional[str] = None, state: str = "open"
    ) -> list[Pull]:
        """The pull requests of ``repo`` in ``state`` (``open``, ``closed``, ``merged``, ``all``), by ``author`` when given."""
        ...

    def get_pull(self, repo: str, number: int) -> Pull:
        """Read one pull request, with its head commit, checks and mergeability as of now."""
        ...

    def pull_diff(self, repo: str, number: int) -> str:
        """The pull request's unified diff against its base, as GitHub computes it."""
        ...

    def post_review(
        self, repo: str, number: int, body: str, *, event: str
    ) -> None:
        """Post a review with ``body`` and ``event`` (one of :data:`REVIEW_EVENTS`) on a pull request."""
        ...

    def merge_pull(
        self, repo: str, number: int, *, method: str = "squash", match_head_sha: str
    ) -> None:
        """Merge a pull request by ``method``, only while its head is still ``match_head_sha``.

        Raises :class:`GitHubError` when the head moved, the pull request cannot be
        merged, or GitHub refuses.
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


def checks_state(rollup: Any) -> str:
    """One of :data:`CHECK_STATES` for a ``statusCheckRollup`` as ``gh pr view`` gives it.

    A check run counts by its ``conclusion`` once completed; a commit status by its
    ``state``. Any failure makes the whole ``failure``; otherwise anything not finished
    makes it ``pending``; a skipped or neutral check is as good as a success.

    >>> checks_state([])
    'none'
    >>> checks_state([{"status": "COMPLETED", "conclusion": "SUCCESS"}])
    'success'
    >>> checks_state([{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"state": "PENDING"}])
    'pending'
    >>> checks_state([{"status": "IN_PROGRESS"}, {"state": "FAILURE"}])
    'failure'
    """
    items = [item for item in (rollup or []) if isinstance(item, dict)]
    if not items:
        return CHECKS_NONE
    verdicts = []
    for item in items:
        if "state" in item:  # a commit status context
            verdicts.append(str(item.get("state") or "").upper())
        elif str(item.get("status") or "").upper() == "COMPLETED":
            verdicts.append(str(item.get("conclusion") or "").upper())
        else:
            verdicts.append("PENDING")
    if any(verdict in _FAILED_CONCLUSIONS for verdict in verdicts):
        return CHECKS_FAILURE
    if any(verdict in ("PENDING", "EXPECTED", "QUEUED", "") for verdict in verdicts):
        return CHECKS_PENDING
    return CHECKS_SUCCESS


def _mergeable(value: Any) -> Optional[bool]:
    text = str(value or "").upper()
    return {"MERGEABLE": True, "CONFLICTING": False}.get(text)


def _pull_from_json(repo: str, raw: dict) -> Pull:
    return Pull(
        repo=repo,
        number=raw["number"],
        title=raw.get("title", ""),
        author=(raw.get("author") or {}).get("login", ""),
        body=raw.get("body") or "",
        head_sha=raw.get("headRefOid", ""),
        base=raw.get("baseRefName", ""),
        created_at=_parse_dt(raw["createdAt"]),
        updated_at=_parse_dt(raw["updatedAt"]),
        state=str(raw.get("state", "open")).lower(),
        is_draft=bool(raw.get("isDraft", False)),
        mergeable=_mergeable(raw.get("mergeable")),
        checks=checks_state(raw.get("statusCheckRollup")),
        labels=tuple(l["name"] for l in raw.get("labels", [])),
        url=raw.get("url", ""),
    )


class GhCli:
    """The default :class:`GitHub`: every call shells out to the `gh` CLI.

    Auth belongs to whatever machine `gh` is configured on. This class never
    reads, stores or passes a token.
    """

    def __init__(self, *, gh_bin: str = "gh"):
        self.gh_bin = gh_bin

    def _run(self, *args: str, input: Optional[str] = None) -> str:
        proc = subprocess.run(
            [self.gh_bin, *args], capture_output=True, text=True, input=input
        )
        if proc.returncode != 0:
            raise GitHubError(proc.stderr.strip() or proc.stdout.strip())
        return proc.stdout

    @staticmethod
    def _kind(pull: bool) -> str:
        return "pr" if pull else "issue"

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

    def add_labels(
        self, repo: str, number: int, labels: Sequence[str], *, pull: bool = False
    ) -> None:
        if not labels:
            return
        self._run(
            self._kind(pull),
            "edit",
            str(number),
            "--repo",
            repo,
            *[flag for label in labels for flag in ("--add-label", label)],
        )

    def remove_labels(
        self, repo: str, number: int, labels: Sequence[str], *, pull: bool = False
    ) -> None:
        if not labels:
            return
        self._run(
            self._kind(pull),
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

    # ---- pull requests ----

    def list_pulls(
        self, repo: str, *, author: Optional[str] = None, state: str = "open"
    ) -> list[Pull]:
        args = [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            state,
            "--json",
            _PULL_FIELDS,
            "--limit",
            str(PULL_LIST_LIMIT),
        ]
        if author:
            args += ["--author", author]
        raw = json.loads(self._run(*args))
        return [_pull_from_json(repo, r) for r in raw]

    def get_pull(self, repo: str, number: int) -> Pull:
        raw = json.loads(
            self._run("pr", "view", str(number), "--repo", repo, "--json", _PULL_FIELDS)
        )
        return _pull_from_json(repo, raw)

    def pull_diff(self, repo: str, number: int) -> str:
        return self._run("pr", "diff", str(number), "--repo", repo)

    def post_review(
        self, repo: str, number: int, body: str, *, event: str
    ) -> None:
        if event not in _REVIEW_FLAGS:
            raise ValueError(
                f"review event {event!r} is not one of: {', '.join(REVIEW_EVENTS)}"
            )
        # The body goes in on standard input, never on the command line.
        self._run(
            "pr",
            "review",
            str(number),
            "--repo",
            repo,
            _REVIEW_FLAGS[event],
            "--body-file",
            "-",
            input=body,
        )

    def merge_pull(
        self, repo: str, number: int, *, method: str = "squash", match_head_sha: str
    ) -> None:
        if method not in MERGE_METHODS:
            raise ValueError(
                f"merge method {method!r} is not one of: {', '.join(MERGE_METHODS)}"
            )
        self._run(
            "pr",
            "merge",
            str(number),
            "--repo",
            repo,
            f"--{method}",
            "--match-head-commit",
            match_head_sha,
        )


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
        self._pulls: dict[tuple[str, int], Pull] = {}
        self._diffs: dict[tuple[str, int], str] = {}
        #: Every review posted: ``(repo, number, body, event)``, in order.
        self.reviews: list[tuple[str, int, str, str]] = []
        #: Every merge made: ``(repo, number, method, match_head_sha)``, in order.
        self.merges: list[tuple[str, int, str, str]] = []
        #: What the next merge fails with, if anything (a test sets it).
        self.merge_error: Optional[str] = None

    def seed(self, issue: Issue) -> None:
        """Add or replace an issue, for test setup."""
        self._issues[(issue.repo, issue.number)] = issue

    def seed_pull(self, pull: Pull, *, diff: Optional[str] = None) -> None:
        """Add or replace a pull request, and its diff when given, for test setup."""
        self._pulls[(pull.repo, pull.number)] = pull
        if diff is not None:
            self._diffs[(pull.repo, pull.number)] = diff

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

    def add_labels(
        self, repo: str, number: int, labels: Sequence[str], *, pull: bool = False
    ) -> None:
        if pull:
            found = self.get_pull(repo, number)
            new_labels = tuple(dict.fromkeys((*found.labels, *labels)))
            self._pulls[(repo, number)] = replace(found, labels=new_labels)
            return
        issue = self.get_issue(repo, number)
        new_labels = tuple(dict.fromkeys((*issue.labels, *labels)))
        self._issues[(repo, number)] = replace(issue, labels=new_labels)

    def remove_labels(
        self, repo: str, number: int, labels: Sequence[str], *, pull: bool = False
    ) -> None:
        if pull:
            found = self.get_pull(repo, number)
            new_labels = tuple(l for l in found.labels if l not in labels)
            self._pulls[(repo, number)] = replace(found, labels=new_labels)
            return
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

    # ---- pull requests ----

    def list_pulls(
        self, repo: str, *, author: Optional[str] = None, state: str = "open"
    ) -> list[Pull]:
        result = [p for p in self._pulls.values() if p.repo == repo]
        if state != "all":
            result = [p for p in result if p.state == state]
        if author is not None:
            result = [p for p in result if p.author.casefold() == author.casefold()]
        return sorted(result, key=lambda p: p.number)

    def get_pull(self, repo: str, number: int) -> Pull:
        try:
            return self._pulls[(repo, number)]
        except KeyError:
            raise GitHubError(f"no such pull request: {repo}#{number}") from None

    def pull_diff(self, repo: str, number: int) -> str:
        self.get_pull(repo, number)
        return self._diffs.get((repo, number), "")

    def post_review(
        self, repo: str, number: int, body: str, *, event: str
    ) -> None:
        if event not in REVIEW_EVENTS:
            raise ValueError(
                f"review event {event!r} is not one of: {', '.join(REVIEW_EVENTS)}"
            )
        self.get_pull(repo, number)
        self.reviews.append((repo, number, body, event))

    def merge_pull(
        self, repo: str, number: int, *, method: str = "squash", match_head_sha: str
    ) -> None:
        found = self.get_pull(repo, number)
        if self.merge_error is not None:
            raise GitHubError(self.merge_error)
        if found.head_sha != match_head_sha:
            raise GitHubError(
                f"head of {repo}#{number} is {found.head_sha}, not {match_head_sha}"
            )
        if found.state != "open" or found.mergeable is False:
            raise GitHubError(f"{repo}#{number} cannot be merged")
        self.merges.append((repo, number, method, match_head_sha))
        self._pulls[(repo, number)] = replace(found, state=MERGED_STATE)
