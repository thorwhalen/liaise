"""Reviewing partner pull requests: the ``review`` step of the tick, and ``liaise review``.

A partner has read access, so their change arrives as a pull request from a fork. Each
one a subject's ``[review]`` table covers (:class:`~liaise.subjects.ReviewPolicy`) gets a
case of its own, ``kind == "pull"``, keyed on the pull request's reference and moving
through :data:`~liaise.model.PR_STATES`. The review itself is a processor run on the
owner's machine, under the owner's subscription: the same seam a case's work runs through,
with a prompt of its own (:func:`compose_review_prompt`) and a structured result of its own
(:data:`REVIEW_SCHEMA`). The run posts nothing. **liaise posts the verdict**, through the
outbound gate, as a pull-request review (``APPROVE``, ``REQUEST_CHANGES``, or a ``COMMENT``
for a decline), keeps the state label, and tells the owner.

**One review per head commit.** A review is of a commit, so each run is recorded with
the head SHA it reviewed, and the same SHA is never reviewed twice. A new push gets a new
review, from a fresh session. A run that failed for a reason that counts against the cap
is not retried for that SHA either: the owner is told, and the next push starts over.

**Merging.** With ``merge = "squash"``, an approved pull request is squash-merged on a
later tick, once every one of :func:`merge_blockers` is clear: the checks are green (or
the repository has none), it is not a draft, GitHub finds it mergeable, its head is still
the commit that was approved, ``veto_minutes`` have passed since the approval, and nobody
set the hold label (``liaise:hold``). The merge is bound to that head commit. A merge that
fails is recorded and the owner told; nothing retries it for that commit.

**What the tick does here**, after it has started the ready cases of each subject
(:class:`ReviewStep`): list the open pull requests of each reviewed repository, open a
case for each new one by a reviewed author, dispatch a review of each head commit not yet
reviewed, within the subject's daily and concurrent budgets (the same counter the issue
cases use), merge what may be merged, and notice a pull request merged elsewhere. A
finished review run is collected by the tick's reconcile step, as every run is, and handed
to :meth:`ReviewStep.collected`.

**Posting.** The verdict's body (:func:`review_body`) goes through
:func:`liaise.gate.run_gate` like every message liaise sends: the policy, the writing card,
deslop, and the mention. A verdict the gate holds back stays on the case as a draft
(``outcome: review``), the case in ``pr-reviewing``, and the owner posts it with ``liaise
review post`` after reading it (:func:`release_review_draft`). ``for_owner`` never reaches
the pull request: it is a ``note`` on the case, and the owner's notification says one is
there.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional, Union
from urllib.parse import quote

from liaise.config import ConfigError
from liaise.errors import ERROR_ACTIONS
from liaise.errors import defer_until as defer_for_error
from liaise.gate import (
    DFLT_OUTBOUND_FILTERS,
    GateContext,
    GateDecision,
    Outbound,
    OutboundFilter,
    approval_for,
    run_gate,
)
from liaise.github import (
    CHECKS_NONE,
    CHECKS_SUCCESS,
    MERGED_STATE,
    GitHub,
    GitHubError,
    Pull,
)
from liaise.holds import DFLT_SET_BY, blocking_hold, scopes_for
from liaise.ledger import Ledger
from liaise.model import (
    PR_STATES,
    PULL_KIND,
    Approval,
    Case,
    LedgerEntry,
    RunRecord,
    RunResult,
)
from liaise.notify import (
    NOTICE_DIVERTED,
    NOTICE_MERGE_FAILED,
    NOTICE_PR_MERGED,
    NOTICE_REVIEW_POSTED,
    NOTICE_RUN_LOST,
    NOTICE_SEND_FAILED,
)
from liaise.outcomes import make_draft
from liaise.policy import Provenance
from liaise.processor import FRESH, RUNNING, Job
from liaise.projection import github_issue, github_repos, hold_label
from liaise.prompt import _join_sections, _packaged_text
from liaise.release import audience_of, error_text
from liaise.subjects import (
    MERGE_SQUASH,
    REVIEW_AUTHOR_PERMISSION,
    ReviewPolicy,
    Subject,
)

REVIEWING, APPROVED, CHANGES_REQUESTED, DECLINED, MERGED = PR_STATES
#: A review run's verdict, in its structured result.
VERDICTS = ("approve", "changes", "decline")
APPROVE, CHANGES, DECLINE = VERDICTS
#: How serious a finding is: the change cannot land as it is; it should be fixed first; or
#: it is optional.
SEVERITIES = ("block", "should", "nit")
#: The review event each verdict is posted as, and the state it moves the case to. A
#: decline is a comment: the owner decides, and the pull request is not blocked by liaise.
VERDICT_EVENTS = MappingProxyType(
    {APPROVE: "APPROVE", CHANGES: "REQUEST_CHANGES", DECLINE: "COMMENT"}
)
VERDICT_STATES = MappingProxyType(
    {APPROVE: APPROVED, CHANGES: CHANGES_REQUESTED, DECLINE: DECLINED}
)
#: The ``purpose`` of a posted verdict, as the gate and the ledger see it.
REVIEW_PURPOSE = "review"
#: The packaged rules every review prompt starts with, in ``liaise/data``.
REVIEW_RULES_RESOURCE = "review_rules.md"
#: The scratch directory, under ``state_dir``, a review runs in when the subject has no
#: checkout of the pull request's repository.
DFLT_REVIEW_SUBDIR = "review"
#: How many times a head commit is dispatched for review at most: once, and once more when
#: the first run was lost before it could be collected.
MAX_ATTEMPTS_PER_SHA = 2
#: The ``detail["event"]`` of the ``run`` entries a review case records.
RUN_STARTED = "started"
RUN_COLLECTED = "collected"
RUN_LOST = "lost"
RUN_MERGED = "merged"
RUN_MERGE_FAILED = "merge_failed"
#: ...and the pull request found closed without a merge, once, so it is not read again.
PULL_CLOSED = "pull_closed"
#: The ``detail["event"]`` of the ``message`` entry a review case opens with.
PULL_OPENED = "pull.opened"
#: What the partner reads when their pull request is too large to review at once. Posted
#: as ``changes``, with no run.
TOO_LARGE_SUMMARY = (
    "This change is {lines} lines of diff, more than the {limit} a review here reads at "
    "once. Please split it into smaller pull requests, each doing one thing; each will "
    "be reviewed on its own."
)
#: The last line of every posted verdict.
REVIEW_FOOTER = (
    "This review was written by the maintainer's review assistant; the maintainer sees "
    "it too, and has the last word."
)
#: How a finding is listed in the posted body.
FINDING_LINE = "- {where} ({severity}): {note}"
#: The actor of the ledger entries the review step writes.
REVIEW_ACTOR = "liaise"
#: Who the operator is recorded as when they post a held verdict.
OPERATOR_ACTOR = DFLT_SET_BY

#: The JSON Schema of a review run's structured result, passed to ``claude --json-schema``.
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": list(VERDICTS),
            "description": (
                "approve: nothing blocks it and nothing should change. changes: the "
                "author can fix what you found. decline: it should not be made, or the "
                "maintainer must decide."
            ),
        },
        "summary": {
            "type": "string",
            "description": (
                "For the author, in plain language: what the change does well, what "
                "must change, and what to do next. It is posted as written."
            ),
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "note": {"type": "string"},
                },
                "required": ["file", "severity", "note"],
                "additionalProperties": False,
            },
            "description": "Each thing found, with where it is and what would fix it.",
        },
        "for_owner": {
            "type": "string",
            "description": (
                "For the maintainer only, never posted: why you declined, what you "
                "could not check, anything else you saw."
            ),
        },
    },
    "required": ["verdict", "summary"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Finding:
    """One thing a review found: where, how serious, and what would fix it."""

    file: str
    severity: str
    note: str
    line: Optional[int] = None

    @property
    def where(self) -> str:
        """``file:line``, or the file alone."""
        return f"{self.file}:{self.line}" if self.line is not None else self.file


@dataclass(frozen=True)
class Review:
    """A review run's verdict, as :func:`parse_review` reads it from the structured result."""

    verdict: str
    summary: str
    findings: tuple[Finding, ...] = ()
    for_owner: str = ""

    @property
    def event(self) -> str:
        """The GitHub review event this verdict is posted as."""
        return VERDICT_EVENTS[self.verdict]

    @property
    def state(self) -> str:
        """The review state this verdict moves the case to."""
        return VERDICT_STATES[self.verdict]

    def to_dict(self) -> dict[str, Any]:
        """This review as JSON-ready data, as a ``review`` entry's detail keeps it."""
        return {
            "verdict": self.verdict,
            "summary": self.summary,
            "findings": [
                {
                    "file": f.file,
                    "line": f.line,
                    "severity": f.severity,
                    "note": f.note,
                }
                for f in self.findings
            ],
            "for_owner": self.for_owner,
        }


# ---- pure helpers ----


def parse_review(structured: Any) -> Review:
    """The :class:`Review` a run's structured result holds, or ``ValueError`` listing every problem.

    >>> parse_review({"verdict": "changes", "summary": "Nearly.",
    ...     "findings": [{"file": "a.py", "line": 3, "severity": "should", "note": "x"}]})
    Review(verdict='changes', summary='Nearly.', findings=(Finding(file='a.py', severity='should', note='x', line=3),), for_owner='')
    >>> parse_review({"verdict": "maybe"})
    Traceback (most recent call last):
    ...
    ValueError: the review result is not valid: verdict 'maybe' is not one of: approve, changes, decline; summary is missing
    """
    problems: list[str] = []
    if not isinstance(structured, Mapping):
        raise ValueError(
            f"the review result is not valid: expected an object with verdict and "
            f"summary, got {type(structured).__name__}"
        )
    verdict = structured.get("verdict")
    if verdict not in VERDICTS:
        problems.append(f"verdict {verdict!r} is not one of: {', '.join(VERDICTS)}")
    summary = structured.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        problems.append("summary is missing")
    findings: list[Finding] = []
    raw_findings = structured.get("findings") or []
    if not isinstance(raw_findings, list):
        problems.append("findings is not a list")
        raw_findings = []
    for index, item in enumerate(raw_findings):
        where = f"findings[{index}]"
        if not isinstance(item, Mapping):
            problems.append(f"{where} is not an object")
            continue
        severity = item.get("severity")
        if severity not in SEVERITIES:
            problems.append(
                f"{where}.severity {severity!r} is not one of: {', '.join(SEVERITIES)}"
            )
        line = item.get("line")
        if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
            problems.append(f"{where}.line is not a whole number")
            line = None
        findings.append(
            Finding(
                file=str(item.get("file") or ""),
                severity=str(severity),
                note=str(item.get("note") or ""),
                line=line,
            )
        )
    for_owner = structured.get("for_owner") or ""
    if not isinstance(for_owner, str):
        problems.append("for_owner is not text")
        for_owner = str(for_owner)
    if problems:
        raise ValueError(f"the review result is not valid: {'; '.join(problems)}")
    return Review(
        verdict=verdict,
        summary=summary.strip(),
        findings=tuple(findings),
        for_owner=for_owner.strip(),
    )


def review_body(review: Review, *, footer: str = REVIEW_FOOTER) -> str:
    """The text posted on the pull request: the summary, the findings, and ``footer``.

    >>> print(review_body(Review("changes", "Nearly there.",
    ...     (Finding("a.py", "should", "handle an empty list", line=3),)), footer="(f)"))
    Nearly there.
    <BLANKLINE>
    Findings:
    <BLANKLINE>
    - a.py:3 (should): handle an empty list
    <BLANKLINE>
    (f)
    """
    parts = [review.summary]
    if review.findings:
        listed = "\n".join(
            FINDING_LINE.format(where=f.where, severity=f.severity, note=f.note)
            for f in review.findings
        )
        parts.append(f"Findings:\n\n{listed}")
    if footer:
        parts.append(footer)
    return "\n\n".join(parts)


def review_repos(subject: Subject) -> list[str]:
    """The repositories ``subject`` reviews pull requests in: the bound ones, then ``review.repos``.

    Each once, compared without regard to case, in that order; none without a
    ``[review]`` table.

    >>> from liaise.subjects import Policy
    >>> subject = Subject("app", ("github:example/app?labels=partner:pat",),
    ...     Policy(people={}, roles={}),
    ...     review=ReviewPolicy(authors=("pat",), repos=("example/app", "example/lib")))
    >>> review_repos(subject)
    ['example/app', 'example/lib']
    """
    if subject.review is None:
        return []
    repos: dict[str, str] = {}
    for repo in (*github_repos(subject), *subject.review.repos):
        repos.setdefault(repo.casefold(), repo)
    return list(repos.values())


def wanted_pulls(subject: Subject, github: GitHub) -> list[Pull]:
    """The open pull requests ``subject`` reviews, by its reviewed authors, repository by repository.

    Each repository is listed once (``GitHubError`` propagates). Drafts are included:
    the caller decides what a draft gets (a line, no review).
    """
    policy = subject.review
    if policy is None:
        return []
    found = []
    for repo in review_repos(subject):
        for pull in github.list_pulls(repo, state="open"):
            if policy.reviews(pull.author):
                found.append(pull)
    return found


def diff_lines(diff: str) -> int:
    """How many lines ``diff`` has, as ``max_diff_lines`` counts them.

    >>> diff_lines(""), diff_lines("a\\nb\\n"), diff_lines("a\\nb")
    (0, 2, 2)
    """
    return len(diff.splitlines())


def review_entries(case: Case) -> list[LedgerEntry]:
    """The case's ``review`` entries, oldest first: one verdict per reviewed head commit."""
    return [entry for entry in case.entries if entry.kind == "review"]


def review_for(case: Case, head_sha: str) -> Optional[LedgerEntry]:
    """The ``review`` entry that judged ``head_sha``, or None when it has not been reviewed."""
    found = [e for e in review_entries(case) if e.detail.get("head_sha") == head_sha]
    return found[-1] if found else None


def latest_review(case: Case) -> Optional[LedgerEntry]:
    """The case's latest ``review`` entry, or None before any."""
    found = review_entries(case)
    return found[-1] if found else None


def attempts_for(case: Case, head_sha: str) -> list[LedgerEntry]:
    """The ``run`` entries that started a review of ``head_sha``, oldest first."""
    return [
        e
        for e in case.entries
        if e.kind == "run"
        and e.detail.get("event") == RUN_STARTED
        and e.detail.get("head_sha") == head_sha
    ]


def run_events(case: Case, run_id: str) -> list[LedgerEntry]:
    """The ``run`` entries about ``run_id``, oldest first."""
    return [
        e for e in case.entries if e.kind == "run" and e.detail.get("run_id") == run_id
    ]


def head_sha_of_run(case: Case, run_id: str) -> Optional[str]:
    """The head commit the run ``run_id`` was dispatched to review, from its start entry."""
    return next(
        (
            e.detail.get("head_sha")
            for e in run_events(case, run_id)
            if e.detail.get("event") == RUN_STARTED
        ),
        None,
    )


def merge_blockers(
    pull: Pull,
    case: Case,
    policy: ReviewPolicy,
    *,
    now: datetime,
    hold: str,
) -> list[str]:
    """Why liaise may not merge ``pull`` now; empty when it may.

    In words the plan prints: merging off, not approved, the head moved since the
    approval, a draft, not open, conflicts or mergeability unknown, checks not green
    (unless the repository has none), the hold label, the veto window still open, or a
    merge of this head that already failed.
    """
    reasons = []
    if policy.merge != MERGE_SQUASH:
        reasons.append("merge is off for this subject")
    if case.state != APPROVED:
        reasons.append(f"not approved ({case.state})")
    approval = latest_review(case)
    approved_sha = approval.detail.get("head_sha") if approval else None
    if approval is None:
        reasons.append("no review recorded")
    elif approved_sha != pull.head_sha:
        reasons.append("the head moved since the approval; a new review is owed")
    if pull.state != "open":
        reasons.append(f"the pull request is {pull.state}")
    if pull.is_draft:
        reasons.append("it is a draft")
    if pull.mergeable is False:
        reasons.append("GitHub says it cannot be merged (conflicts)")
    elif pull.mergeable is None:
        reasons.append("GitHub has not said whether it can be merged yet")
    if policy.require_checks and pull.checks not in (CHECKS_SUCCESS, CHECKS_NONE):
        reasons.append(f"checks: {pull.checks}")
    if hold in pull.labels:
        reasons.append(f"the {hold} label is on it")
    if approval is not None:
        veto = timedelta(minutes=policy.veto_minutes)
        remaining = approval.at + veto - now
        if remaining > timedelta(0):
            minutes = int(remaining.total_seconds() // 60) + 1
            reasons.append(f"veto window: {minutes}m to go")
    if any(
        e.kind == "run"
        and e.detail.get("event") == RUN_MERGE_FAILED
        and e.detail.get("head_sha") == pull.head_sha
        for e in case.entries
    ):
        reasons.append("a merge of this head already failed; see liaise review show")
    return reasons


def pull_login(case: Case) -> str:
    """The GitHub login that opened the case's pull request, from its opening entry."""
    opened = next(
        (
            e.detail.get("author")
            for e in case.entries
            if e.kind == "message" and e.detail.get("event") == PULL_OPENED
        ),
        None,
    )
    return str(opened or case.reporter)


def closed_recorded(case: Case) -> bool:
    """Whether the case's pull request was last found closed, or merged, so it is not read again."""
    events = [e.detail.get("event") for e in case.entries if e.kind == "run"]
    return bool(events) and events[-1] in (PULL_CLOSED, RUN_MERGED)


def review_provenance(subject: Subject, login: str) -> Provenance:
    """Whether a review of a pull request by ``login`` read anything the subject does not trust.

    A pull request is trusted like a message: when its author resolves to a person whose
    role grants :data:`~liaise.subjects.REVIEW_AUTHOR_PERMISSION` at the platform grade
    GitHub gives, the review read nothing untrusted. Otherwise it is tainted, and the
    policy's taint rule applies to the verdict (see ``policy.tainted_runs``).
    """
    person = subject.person_for_login(login)
    role = subject.policy.roles.get(person) if person is not None else None
    trusted = (
        role is not None
        and REVIEW_AUTHOR_PERMISSION in subject.permissions_for(role)
        and subject.accepts(REVIEW_AUTHOR_PERMISSION, "platform")
    )
    if trusted:
        return Provenance.clean(
            f"a pull request by {person}, whose role {role} grants "
            f"{REVIEW_AUTHOR_PERMISSION}"
        )
    who = person or f"github:{login}"
    return Provenance.tainted_by(
        f"a pull request by {who}, whose role ({role or 'none'}) does not grant "
        f"{REVIEW_AUTHOR_PERMISSION}"
    )


def _read_brief(path: str, *, where: str, setting: str) -> str:
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(
            f"{where}: cannot read the brief {path!r} ({error.strerror or error}). "
            f"Write the brief there, or point `{setting}` at a file that exists."
        ) from None


def compose_review_prompt(
    subject: Subject,
    pull: Pull,
    diff: str,
    *,
    checkout: Optional[str] = None,
    truncated_from: Optional[int] = None,
) -> str:
    """The whole prompt of a review run on ``pull``, in a fixed section order.

    The packaged review rules (:data:`REVIEW_RULES_RESOURCE`), the subject's brief for
    the author (:meth:`~liaise.subjects.Subject.brief_for`) and the review brief
    (``review.brief``) when there are any, the pull request (title, author, base, head,
    body), the diff, the result to end with, and the budget. ``checkout`` is the path of
    the subject's checkout of the repository when it has one, else the run is told it
    works from the diff alone. ``truncated_from`` is the diff's full line count when
    ``diff`` was cut. Raises :class:`~liaise.config.ConfigError` for a brief that cannot
    be read.
    """
    where = subject.source or subject.slug
    policy = subject.review or ReviewPolicy()
    person = subject.person_for_login(pull.author)
    briefs = ["## Briefs"]
    brief = subject.brief_for(person)
    if brief:
        setting = f"policy.briefs.{person}" if person in subject.policy.briefs else "brief"
        briefs += ["", _read_brief(brief, where=where, setting=setting).strip()]
    if policy.brief:
        briefs += [
            "",
            "### Review brief",
            "",
            _read_brief(policy.brief, where=where, setting="review.brief").strip(),
        ]
    if len(briefs) == 1:
        briefs += ["", "No brief is configured."]
    about = [
        "## The pull request",
        "",
        f"- repository: {pull.repo}",
        f"- number: {pull.number} ({pull.url or pull.ref})",
        f"- title: {pull.title}",
        f"- author: @{pull.author}" + (f" ({person})" if person else ""),
        f"- base: {pull.base}",
        f"- head commit: {pull.head_sha}",
        "",
        "Its description, as the author wrote it:",
        "",
        pull.body.strip() or "(none)",
    ]
    if checkout:
        about += [
            "",
            f"A checkout of this repository is at `{checkout}`, the current directory. "
            "Read it, search it and run its tests in a temporary worktree of the pull "
            "request's head; never change its branch or its files.",
        ]
    else:
        about += [
            "",
            "No checkout of this repository is available here. Review from the diff "
            "below and what `gh` lets you read of the repository.",
        ]
    diff_section = ["## The diff", ""]
    if truncated_from is not None:
        diff_section += [
            f"The diff is {truncated_from} lines; only the first "
            f"{policy.max_diff_lines} are here. Read the rest with "
            f"`gh pr diff {pull.number} -R {pull.repo}` if you need it.",
            "",
        ]
    diff_section += ["```diff", diff.rstrip("\n"), "```"]
    result = [
        "## The result",
        "",
        "End this run with a structured result matching the JSON schema you were given: "
        "`verdict` (approve, changes or decline), `summary` for the author, `findings` "
        "(file, line, severity, note) and `for_owner`. If these rules and that schema "
        "ever disagree, the schema wins. The summary and the findings are posted on the "
        "pull request as written, so write them for the author; `for_owner` is never "
        "posted.",
    ]
    budget = subject.policy.budget
    limits = [
        "## Budget",
        "",
        f"- timeout: {budget.timeout_minutes} minutes, enforced from outside: past it "
        "the run is stopped and the maintainer told, whatever you were doing.",
        f"- turn cap: {budget.max_turns} turns, not enforced from outside: watch it "
        "yourself, and before you would pass it, end with the verdict you can stand by "
        "and say in `for_owner` what you did not get to.",
    ]
    return _join_sections(
        [
            _packaged_text(REVIEW_RULES_RESOURCE),
            "\n".join(briefs),
            "\n".join(about),
            "\n".join(diff_section),
            "\n".join(result),
            "\n".join(limits),
        ]
    )


def scratch_dir(state_dir: Union[str, os.PathLike], repo: str) -> Path:
    """Where a review of ``repo`` runs when the subject has no checkout of it: under ``state_dir``.

    >>> scratch_dir("/s", "example/app").as_posix()
    '/s/review/example%2Fapp'
    """
    return Path(state_dir) / DFLT_REVIEW_SUBDIR / quote(repo, safe="")


# ---- the tick's review step ----


class ReviewStep:
    """The review step of one tick, over the tick's own plumbing.

    ``tick`` is the :class:`liaise.tick._Tick` the step runs in: its ledger, labeler
    (the :class:`~liaise.github.GitHub`), processor, clock, dry-run flag, plan lines,
    notifications, entries and transitions, error handling and workspace. The step
    adds no state of its own beyond what the ledger keeps.
    """

    def __init__(self, tick: Any):
        self.tick = tick

    # ---- what the tick calls ----

    def run(self, slug: str) -> None:
        """Review ``slug``'s pull requests: open, dispatch, merge, and notice merges elsewhere."""
        tick = self.tick
        subject = tick.subjects[slug]
        policy = subject.review
        if policy is None:
            return
        seen: set[str] = set()
        pulls: list[Pull] = []
        for repo in review_repos(subject):
            try:
                found = tick.labeler.list_pulls(repo, state="open")
            except Exception as error:  # one repository's failure is not the step's
                tick.problem(f"review {slug}: listing {repo} failed: {error_text(error)}")
                continue
            pulls += [pull for pull in found if policy.reviews(pull.author)]
        tick.say(f"review {slug}: {len(pulls)} open pull request(s) by reviewed authors")
        for pull in sorted(pulls, key=lambda p: (p.repo, p.number)):
            seen.add(pull.ref)
            try:
                self._consider(subject, policy, pull)
            except Exception as error:  # one pull request's failure is not the step's
                tick.problem(f"reviewing {pull.ref} failed: {error_text(error)}")
        for case in sorted(
            tick.ledger.cases(subject=slug, kind=PULL_KIND), key=lambda c: c.id
        ):
            if (
                case.state == MERGED
                or case.conversations[0] in seen
                or closed_recorded(case)
            ):
                continue
            try:
                self._notice_merged_elsewhere(subject, case)
            except Exception as error:
                tick.problem(f"reading {case.conversations[0]} failed: {error_text(error)}")

    def collected(
        self, subject: Subject, case: Case, run: RunRecord, result: RunResult
    ) -> None:
        """What a finished review run means for its case: a verdict posted, or an error."""
        tick = self.tick
        head_sha = head_sha_of_run(case, run.run_id)
        error = result.error
        review: Optional[Review] = None
        if error is None:
            try:
                review = parse_review(result.structured)
            except ValueError as problem:
                tick.problem(f"run {run.run_id}: {problem}")
                error = "needs_human"
        detail = {
            "event": RUN_COLLECTED,
            "run_id": run.run_id,
            "head_sha": head_sha,
            "error": error,
            "cost_usd": result.cost_usd,
        }
        tick._entry(case.id, "run", text=result.summary or None, detail=detail)
        tick.say(
            f"  run {run.run_id} ({case.id}): review collected, {error or 'no error'}"
        )
        if error is not None:
            attempt = max(len(attempts_for(case, head_sha or "")) - 1, 0)
            defer = (
                defer_for_error(
                    error, now=tick.now, attempt=attempt, rate_limit=result.rate_limit
                )
                if error in ERROR_ACTIONS
                else None
            )
            tick._apply_error(
                subject,
                case.id,
                error,
                source=f"run {run.run_id}",
                defer=defer,
                uncount_run=run.run_id,
            )
            return
        assert review is not None
        self._record_and_post(
            subject,
            tick._case(case.id),
            review,
            head_sha=head_sha or "",
            run_id=run.run_id,
            provenance=review_provenance(subject, pull_login(case)),
        )

    # ---- one pull request ----

    def _consider(self, subject: Subject, policy: ReviewPolicy, pull: Pull) -> None:
        tick = self.tick
        case = tick.ledger.case_for_conversation(pull.ref)
        if case is None:
            case = self._open(subject, pull)
        elif case.kind != PULL_KIND:
            tick.problem(
                f"{pull.ref} belongs to {case.id}, an issue case opened before pull "
                f"requests were reviewed; it is not reviewed"
            )
            return
        elif case.subject != subject.slug:
            tick.problem(f"{pull.ref} belongs to {case.id}, of {case.subject}")
            return
        label = f"  pull {pull.ref} ({case.id}, {case.state})"
        if pull.is_draft:
            tick.say(f"{label}: a draft, not reviewed until it is ready")
            return
        if review_for(case, pull.head_sha) is not None:
            tick.say(f"{label}: head {pull.head_sha[:8]} reviewed")
            if case.state == APPROVED:
                self._maybe_merge(subject, policy, case, pull)
            return
        self._dispatch_if_due(subject, policy, case, pull, label=label)

    def _open(self, subject: Subject, pull: Pull) -> Case:
        tick = self.tick
        reporter = subject.person_for_login(pull.author) or pull.author
        case = tick.ledger.new_case(
            subject.slug, pull.ref, reporter=reporter, at=pull.created_at, kind=PULL_KIND
        )
        entry = LedgerEntry(
            at=pull.created_at,
            kind="message",
            actor=reporter,
            grade="platform",
            text=pull.title,
            detail={
                "event": PULL_OPENED,
                "author": pull.author,
                "url": pull.url,
                "head_sha": pull.head_sha,
                "role": "author",
            },
        )
        tick.ledger.append(case.id, entry)
        tick.touched[case.id] = None
        tick.say(f"  pull {pull.ref}: case {case.id} opened, reporter {reporter}")
        return tick._case(case.id)

    def _dispatch_if_due(
        self,
        subject: Subject,
        policy: ReviewPolicy,
        case: Case,
        pull: Pull,
        *,
        label: str,
    ) -> None:
        tick = self.tick
        sha = pull.head_sha
        in_flight = [r for r in tick.ledger.runs(status=RUNNING) if r.case_id == case.id]
        if in_flight:
            tick.say(f"{label}: run {in_flight[0].run_id} is still in flight")
            return
        attempts = attempts_for(case, sha)
        if attempts:
            last_id = str(attempts[-1].detail.get("run_id") or "")
            events = run_events(case, last_id)
            collected = next(
                (e for e in events if e.detail.get("event") == RUN_COLLECTED), None
            )
            if collected is not None:
                # One review per head commit: only an error that does not count against
                # the cap (a rate limit, an outage, a lost login) earns another run.
                error = collected.detail.get("error")
                action = ERROR_ACTIONS.get(error) if error else None
                if action is None or action.counts:
                    tick.say(
                        f"{label}: the review of head {sha[:8]} ended in "
                        f"{error or 'no verdict'}; waiting for a new push"
                    )
                    return
            else:
                if not any(e.detail.get("event") == RUN_LOST for e in events):
                    self._run_lost(subject, case, last_id, sha)
                    case = tick._case(case.id)
                if len(attempts) >= MAX_ATTEMPTS_PER_SHA:
                    tick.say(
                        f"{label}: {len(attempts)} run(s) on head {sha[:8]} were lost; "
                        f"waiting for a new push"
                    )
                    return
        if case.defer_until is not None and case.defer_until > tick.now:
            tick.say(f"{label}: deferred until {case.defer_until.isoformat()}")
            return
        scopes = scopes_for(
            subject=subject.slug,
            person=case.reporter,
            repo=pull.repo,
            checkout=subject.workspace.path or None,
            processor=True,
        )
        hold = blocking_hold(tick.ledger, scopes, for_="start")
        if hold is not None:
            tick.say(f"{label}: held by {hold.scope} ({hold.mode}) {hold.reason}".rstrip())
            return
        try:
            diff = tick.labeler.pull_diff(pull.repo, pull.number)
        except GitHubError as error:
            tick.problem(f"{pull.ref}: reading the diff failed: {error_text(error)}")
            return
        lines = diff_lines(diff)
        if lines > policy.max_diff_lines:
            tick.say(
                f"{label}: {lines} lines of diff, over max_diff_lines "
                f"({policy.max_diff_lines}); asking to split, no run"
            )
            too_large = Review(
                verdict=CHANGES,
                summary=TOO_LARGE_SUMMARY.format(
                    lines=lines, limit=policy.max_diff_lines
                ),
            )
            self._record_and_post(
                subject,
                case,
                too_large,
                head_sha=sha,
                run_id=None,
                provenance=Provenance.clean("a fixed text liaise wrote; no run"),
            )
            return
        budget = subject.policy.budget
        today = tick.ledger.daily_count(subject.slug, tick.now.date())
        if today >= budget.daily_dispatches:
            tick.say(f"{label}: over the daily cap ({today}/{budget.daily_dispatches})")
            tick._notify_daily_cap(subject, case, count=today)
            return
        running = sum(
            1 for r in tick.ledger.runs(status=RUNNING) if r.subject == subject.slug
        )
        if running >= budget.concurrent:
            tick.say(
                f"{label}: ready, but {running} run(s) in flight "
                f"(concurrent cap {budget.concurrent})"
            )
            return
        bound = {repo.casefold() for repo in github_repos(subject)}
        checkout = (
            tick._workspace(subject) if pull.repo.casefold() in bound else None
        )
        if checkout is not None:
            conflict = checkout.conflict()
            if conflict is not None:
                tick.say(f"{label}: workspace_conflict: {conflict}")
                return
        cwd = (
            str(checkout.path)
            if checkout is not None
            else str(scratch_dir(tick.state_dir, pull.repo))
        )
        run_id = tick._next_run_id(case)
        try:
            prompt = compose_review_prompt(
                subject, pull, diff, checkout=str(checkout.path) if checkout else None
            )
        except Exception as error:  # a brief that cannot be read
            tick._start_failed(
                subject, case, run_id, f"composing the prompt: {error_text(error)}"
            )
            return
        job = Job(
            run_id=run_id,
            case_id=case.id,
            subject=subject.slug,
            prompt=prompt,
            cwd=cwd,
            permission_mode=subject.processor.permission_mode,
            timeout_minutes=budget.timeout_minutes,
            json_schema=REVIEW_SCHEMA,
            session_id=None,
        )
        if tick.dry_run:
            tick.say(f"{label}: would dispatch a review of head {sha[:8]} as run {run_id}")
            self._record_start(subject, case, job, sha)
            return
        if checkout is None:
            Path(cwd).mkdir(parents=True, exist_ok=True)
        if not tick._preflight_passes(
            subject, case, job, label=label, auto_processor=False
        ):
            return
        if checkout is not None:
            lock = tick._lock_conflict(checkout, run_id)
            if lock is not None or not checkout.acquire(
                run_id=run_id, pid=os.getpid(), now=tick.now
            ):
                tick.say(f"{label}: workspace_conflict: {lock or 'the checkout is locked'}")
                return
        record, failure = tick._call("start", job)
        if failure is None and not isinstance(record, RunRecord):
            failure = f"processor returned {type(record).__name__}, not a RunRecord"
        if failure is not None:
            if checkout is not None:
                checkout.release(run_id=run_id)
            tick._start_failed(subject, case, run_id, failure)
            return
        if checkout is not None and record.pid:
            checkout.acquire(run_id=run_id, pid=record.pid, now=tick.now)
        tick.say(f"{label}: dispatched a review of head {sha[:8]} as run {run_id}")
        self._record_start(subject, case, job, sha, record=record)

    def _record_start(
        self,
        subject: Subject,
        case: Case,
        job: Job,
        sha: str,
        *,
        record: Optional[RunRecord] = None,
    ) -> None:
        tick = self.tick
        day = tick.now.date().isoformat()
        started = replace(
            record
            if record is not None
            else RunRecord(
                run_id=job.run_id,
                case_id=case.id,
                subject=subject.slug,
                mode=FRESH,
                status=RUNNING,
                started_at=tick.now,
            ),
            status=RUNNING,
            started_at=tick.now,
        )
        tick.ledger.save_run(started)
        tick._entry(
            case.id,
            "run",
            detail={
                "event": RUN_STARTED,
                "run_id": job.run_id,
                "mode": FRESH,
                "day": day,
                "head_sha": sha,
            },
        )
        if case.defer_until is not None:
            tick._save(replace(tick._case(case.id), defer_until=None))
        tick._transition(case.id, REVIEWING, f"reviewing head {sha[:8]} ({job.run_id})")
        tick.ledger.increment_daily(subject.slug, day)
        tick.dispatched.append(job.run_id)

    def _run_lost(self, subject: Subject, case: Case, run_id: str, sha: str) -> None:
        tick = self.tick
        tick._entry(
            case.id,
            "run",
            detail={"event": RUN_LOST, "run_id": run_id, "head_sha": sha},
        )
        tick.say(f"  case {case.id}: run {run_id} lost (no run in flight, no result)")
        tick._notice(NOTICE_RUN_LOST, subject=subject.slug, case_ids=(case.id,))

    # ---- the verdict ----

    def _record_and_post(
        self,
        subject: Subject,
        case: Case,
        review: Review,
        *,
        head_sha: str,
        run_id: Optional[str],
        provenance: Provenance,
    ) -> None:
        tick = self.tick
        tick._entry(
            case.id,
            "review",
            text=review.summary,
            detail={**review.to_dict(), "head_sha": head_sha, "run_id": run_id},
        )
        if review.for_owner:
            tick._entry(
                case.id,
                "note",
                text=review.for_owner,
                detail={"head_sha": head_sha, "run_id": run_id, "from": "review"},
            )
        tick.say(f"  review {case.id}: {review.verdict} on head {head_sha[:8]}")
        posted = post_verdict(
            subject,
            tick._case(case.id),
            review,
            head_sha=head_sha,
            ledger=tick.ledger,
            github=tick.labeler,
            now=tick.now,
            provenance=provenance,
            registry=tick.registry,
            outbound_filters=tick.outbound_filters,
            fingerprint_key=tick.fingerprint_key,
            dry_run=tick.dry_run,
            actor=REVIEW_ACTOR,
        )
        tick.touched[case.id] = None
        for line in posted.lines:
            tick.say(f"  {line}")
        if posted.notice is not None:
            tick._notice(
                posted.notice,
                subject=subject.slug,
                case_ids=(case.id,),
                cause=posted.cause,
            )
        if posted.sent:
            tick.sent.append(posted.outbound)
            tick._transition(case.id, review.state, f"review posted: {review.verdict}")
            noted = ", with a note for you" if review.for_owner else ""
            tick._notice(
                NOTICE_REVIEW_POSTED,
                subject=subject.slug,
                case_ids=(case.id,),
                cause=f"{review.verdict}{noted}",
            )
        elif posted.outbound is not None:
            tick._record_diversion(posted.outbound, posted.cause or "")

    # ---- merging ----

    def _maybe_merge(
        self, subject: Subject, policy: ReviewPolicy, case: Case, pull: Pull
    ) -> None:
        tick = self.tick
        if policy.merge != MERGE_SQUASH:
            return
        label = f"  merge {pull.ref} ({case.id})"
        try:
            fresh = tick.labeler.get_pull(pull.repo, pull.number)
        except GitHubError as error:
            tick.problem(f"{pull.ref}: reading it before merging failed: {error_text(error)}")
            return
        blockers = merge_blockers(
            fresh, case, policy, now=tick.now, hold=hold_label(subject)
        )
        if blockers:
            tick.say(f"{label}: not yet: {'; '.join(blockers)}")
            return
        if tick.dry_run:
            tick.say(f"{label}: would squash-merge head {fresh.head_sha[:8]}")
            return
        try:
            tick.labeler.merge_pull(
                pull.repo, pull.number, method=policy.merge, match_head_sha=fresh.head_sha
            )
        except Exception as error:
            why = error_text(error)
            tick._entry(
                case.id,
                "run",
                text=why,
                detail={"event": RUN_MERGE_FAILED, "head_sha": fresh.head_sha},
            )
            tick.problem(f"{pull.ref}: merging failed: {why}")
            tick._notice(
                NOTICE_MERGE_FAILED,
                subject=subject.slug,
                case_ids=(case.id,),
                cause=type(error).__name__,
            )
            return
        tick._entry(
            case.id,
            "run",
            detail={"event": RUN_MERGED, "head_sha": fresh.head_sha, "method": policy.merge},
        )
        tick.say(f"{label}: squash-merged head {fresh.head_sha[:8]}")
        tick._transition(case.id, MERGED, f"merged head {fresh.head_sha[:8]}")
        tick._notice(NOTICE_PR_MERGED, subject=subject.slug, case_ids=(case.id,))

    def _notice_merged_elsewhere(self, subject: Subject, case: Case) -> None:
        tick = self.tick
        issue = github_issue(case.conversations[0])
        if issue is None:
            return
        repo, number = issue
        pull = tick.labeler.get_pull(repo, number)
        if pull.state == MERGED_STATE:
            tick.say(f"  pull {pull.ref} ({case.id}): merged outside liaise")
            tick._entry(
                case.id,
                "run",
                detail={"event": RUN_MERGED, "head_sha": pull.head_sha, "elsewhere": True},
            )
            tick._transition(case.id, MERGED, "merged outside liaise")
        elif pull.state != "open":
            tick.say(f"  pull {pull.ref} ({case.id}): closed without a merge")
            tick._entry(
                case.id, "run", detail={"event": PULL_CLOSED, "head_sha": pull.head_sha}
            )


@dataclass(frozen=True)
class Posted:
    """What :func:`post_verdict` did: sent, held as a draft, or failed; and what to say."""

    sent: bool
    outbound: Optional[Outbound] = None
    decision: Optional[GateDecision] = None
    lines: tuple[str, ...] = ()
    notice: Optional[str] = None
    cause: Optional[str] = None


def post_verdict(
    subject: Subject,
    case: Case,
    review: Review,
    *,
    head_sha: str,
    ledger: Ledger,
    github: GitHub,
    now: datetime,
    provenance: Provenance,
    registry: Optional[Mapping[str, Any]] = None,
    outbound_filters: Iterable[OutboundFilter] = DFLT_OUTBOUND_FILTERS,
    fingerprint_key: Any = None,
    dry_run: bool = False,
    actor: str = REVIEW_ACTOR,
    approval: Optional[Approval] = None,
) -> Posted:
    """Put ``review``'s body through the gate and, when it passes, post it as a pull-request review.

    The message goes to the case's reporter on the pull request, with ``purpose``
    ``review``; the audience is asked of the channel now. A verdict the gate holds back
    is kept on the case as a draft carrying its ``verdict``, ``event`` and ``head_sha``
    (``liaise review post`` releases it), with a ``gate`` entry and :data:`NOTICE_DIVERTED`
    to say. One that passes is posted through ``github.post_review`` with the gate's text
    (the mention added) and the verdict's event, and recorded as sent; a post GitHub
    refuses becomes a draft too. A dry run judges and posts nothing. ``approval`` is the
    operator's, when they release a held verdict. It records on ``ledger`` and sends
    nothing to the operator: the caller does, from ``notice`` and ``cause``.
    """
    ref = case.conversations[0]
    outbound = Outbound(
        ref=ref,
        channel=ref.partition(":")[0],
        recipient=case.reporter,
        purpose=REVIEW_PURPOSE,
        text=review_body(review),
        case_id=case.id,
    )
    context = GateContext(
        subject=subject,
        case=case,
        now=now,
        provenance=provenance,
        approval=approval,
        audience=audience_of(outbound, registry=registry),
        fingerprint_key=fingerprint_key,
    )
    decision = run_gate(outbound, context, outbound_filters=outbound_filters)
    detail = {
        "purpose": REVIEW_PURPOSE,
        "ref": ref,
        "event": review.event,
        "head_sha": head_sha,
        "notes": list(decision.notes),
        **decision.record(),
    }
    notes = tuple(f"  note: {note}" for note in decision.notes)
    head = f"gate {REVIEW_PURPOSE} to {ref}"

    def held(reason: str, *, extra: Mapping[str, Any]) -> Posted:
        if not dry_run:
            draft = make_draft(
                at=now,
                outcome=REVIEW_PURPOSE,
                recipient=case.reporter,
                ref=ref,
                text=outbound.text,
                reason=reason,
                notes=decision.notes,
                gate=decision.summary(),
            )
            draft.update(
                {"verdict": review.verdict, "event": review.event, "head_sha": head_sha}
            )
            current = ledger.get_case(case.id) or case
            ledger.save_case(replace(current, drafts=(*current.drafts, draft)))
            ledger.append(
                case.id,
                LedgerEntry(
                    at=now,
                    kind="gate",
                    actor=actor,
                    text=outbound.text,
                    detail={**detail, **extra},
                ),
            )
        words = _held_words(reason, head, notes, extra)
        return Posted(False, outbound=outbound, decision=decision, **words)

    if decision.send is None:
        reason = decision.diverted or "held by the gate"
        return held(reason, extra={"decision": "divert", "reason": reason})
    passed = decision.send
    if not dry_run:
        issue = github_issue(ref)
        try:
            if issue is None:
                raise GitHubError(f"{ref} is not a pull request reference")
            github.post_review(issue[0], issue[1], passed.text, event=review.event)
        except Exception as error:
            why = error_text(error)
            posted = held(
                f"posting the review failed: {why}",
                extra={"decision": "send", "error": why},
            )
            return replace(posted, notice=NOTICE_SEND_FAILED, cause=type(error).__name__)
    if not dry_run:
        ledger.append(
            case.id,
            LedgerEntry(
                at=now,
                kind="gate",
                actor=actor,
                text=passed.text,
                detail={**detail, "decision": "send", **({"by": actor} if approval else {})},
            ),
        )
    verb = "would post" if dry_run else "posted"
    return Posted(
        True,
        outbound=passed,
        decision=decision,
        lines=(f"{head}: {verb} {review.event}", *notes),
    )


def _held_words(
    reason: str, head: str, notes: tuple[str, ...], extra: Mapping[str, Any]
) -> dict[str, Any]:
    if extra.get("decision") == "divert":
        return {
            "lines": (f"{head}: diverted ({reason}), kept as a draft", *notes),
            "notice": NOTICE_DIVERTED,
            "cause": reason,
        }
    return {"lines": (f"{head}: not posted ({reason}), kept as a draft", *notes)}


# ---- the operator's commands ----


def review_drafts(case: Case) -> list[tuple[int, Mapping[str, Any]]]:
    """The case's drafts that are held verdicts, each with its index among the drafts."""
    return [
        (index, draft)
        for index, draft in enumerate(case.drafts)
        if draft.get("outcome") == REVIEW_PURPOSE
    ]


def release_review_draft(
    subject: Subject,
    case: Case,
    draft: Mapping[str, Any],
    *,
    ledger: Ledger,
    github: GitHub,
    now: datetime,
    registry: Optional[Mapping[str, Any]] = None,
    outbound_filters: Iterable[OutboundFilter] = DFLT_OUTBOUND_FILTERS,
    fingerprint_key: Any = None,
    approval: Optional[Approval] = None,
    approve_shown: bool = False,
    justification: str = "",
    dry_run: bool = False,
) -> tuple[Posted, Optional[Approval]]:
    """Release a held verdict (``liaise review post``): judge it again, post it, move the case on.

    The draft is one of :func:`review_drafts`. Its summary and findings are read back
    from the case's ``review`` entry for the draft's head commit. ``approve_shown`` makes
    the operator's approval of exactly the decision this call reaches (as ``liaise case
    send-draft`` does on the dry run it shows), and ``approval`` is that approval on a later
    call. A dry run posts nothing and records nothing. Once posted, the draft leaves the
    case and the case moves to the verdict's state. Returns what happened and the approval
    made, if any.
    """
    head_sha = str(draft.get("head_sha") or "")
    entry = review_for(case, head_sha)
    if entry is None:
        raise ValueError(
            f"{case.id} holds no review of head {head_sha[:8] or '?'}: the draft cannot "
            f"be posted; take it off with liaise case reject-draft"
        )
    review = parse_review(
        {k: v for k, v in entry.detail.items() if k in REVIEW_SCHEMA["properties"]}
    )
    judged = post_verdict(
        subject,
        case,
        review,
        head_sha=head_sha,
        ledger=ledger,
        github=github,
        now=now,
        provenance=review_provenance(subject, pull_login(case)),
        registry=registry,
        outbound_filters=outbound_filters,
        fingerprint_key=fingerprint_key,
        dry_run=dry_run or approve_shown,
        actor=OPERATOR_ACTOR,
        approval=approval,
    )
    made = None
    if approve_shown and judged.decision is not None:
        made = approval_for(judged.decision, by=OPERATOR_ACTOR, at=now, justification=justification)
    if judged.sent and not (dry_run or approve_shown):
        current = ledger.get_case(case.id) or case
        kept = tuple(d for d in current.drafts if d is not draft and d != draft)
        ledger.save_case(replace(current, drafts=kept))
        ledger.transition(
            case.id,
            review.state,
            at=now,
            actor=OPERATOR_ACTOR,
            reason=f"review posted by the operator: {review.verdict}",
        )
    return judged, made


def review_list_lines(
    subjects: Mapping[str, Subject], ledger: Ledger, github: GitHub
) -> list[str]:
    """What ``liaise review list`` prints: each reviewed subject's open partner pull requests and their state."""
    lines = []
    for slug in sorted(subjects):
        subject = subjects[slug]
        if subject.review is None:
            continue
        policy = subject.review
        lines.append(
            f"subject {slug}: reviews pull requests by {', '.join(policy.authors)} in "
            f"{', '.join(review_repos(subject))} (merge: {policy.merge})"
        )
        try:
            pulls = wanted_pulls(subject, github)
        except GitHubError as error:
            lines.append(f"  could not list pull requests: {error_text(error)}")
            continue
        if not pulls:
            lines.append("  no open pull requests by reviewed authors")
        for pull in sorted(pulls, key=lambda p: (p.repo, p.number)):
            case = ledger.case_for_conversation(pull.ref)
            if case is None:
                state = "not yet taken in"
            else:
                reviewed = review_for(case, pull.head_sha)
                state = case.state + (
                    "" if reviewed is not None else " (head not yet reviewed)"
                )
            draft = ", draft" if pull.is_draft else ""
            lines.append(
                f"  {pull.ref} by @{pull.author}: {pull.title} [{state}{draft}, "
                f"checks {pull.checks}, head {pull.head_sha[:8]}]"
            )
    if not lines:
        lines.append("no subject has a [review] table, so no pull request is reviewed")
    return lines


def review_show_lines(ledger: Ledger, ref: str) -> list[str]:
    """What ``liaise review show`` adds before the case: each review, with its findings and note."""
    case = ledger.case_for_conversation(ref)
    if case is None or case.kind != PULL_KIND:
        raise ValueError(f"no review case for {ref}; liaise review list shows what there is")
    lines = [f"reviews of {ref} ({case.id}, {case.state}): {len(review_entries(case))}"]
    for entry in review_entries(case):
        detail = entry.detail
        sha = str(detail.get("head_sha") or "")[:8]
        lines.append(
            f"  {entry.at.isoformat()} head {sha}: {detail.get('verdict')} "
            f"(run {detail.get('run_id') or 'none'})"
        )
        lines += [f"    {line}" for line in str(entry.text or "").splitlines()]
        for finding in detail.get("findings") or ():
            where = finding.get("file", "")
            if finding.get("line") is not None:
                where += f":{finding['line']}"
            lines.append(f"    - {where} ({finding.get('severity')}): {finding.get('note')}")
        if detail.get("for_owner"):
            lines.append("    for you:")
            lines += [f"      {line}" for line in str(detail["for_owner"]).splitlines()]
    held = review_drafts(case)
    lines.append(f"verdicts held for you: {len(held)}")
    for index, draft in held:
        lines.append(
            f"  [{index}] {draft.get('event')} on head {str(draft.get('head_sha') or '')[:8]}: "
            f"{draft.get('reason')} (liaise review post ...)"
        )
    return lines
