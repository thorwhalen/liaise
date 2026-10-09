"""Label projection: a case's state, shown on each of its GitHub issues as one label.

In 0.1 the ledger holds a case's state. The ``<label_prefix><state>`` label on its GitHub
issues (``liaise:working``) is a projection of it, kept for the partner and the owner to
see at a glance. :func:`project_labels` makes each issue carry exactly the current state's
label: the other state labels come off, then the current one goes on. It is the one place
a state label changes, so the one-label invariant is kept there.

**Pull requests.** A review case (``kind == "pull"``, :mod:`liaise.review`) carries one
of :data:`~liaise.model.PR_STATES` instead, as ``liaise:pr-approved``, on its pull request.
The two vocabularies never touch: projecting a pull request's state removes only the other
pull-request labels, and projecting an issue's removes only the other case labels, so a
label from the wrong vocabulary, however it got there, is left alone. Pull requests get no
waiting label. :data:`HOLD_LABEL` (``liaise:hold``) is the one label a person sets on a
pull request: it keeps liaise from merging it.

**Waiting labels.** When a subject sets ``policy.waiting_labels``, a case that waits on a
person (:data:`WAITING_STATES`: its reporter was asked) also carries that person's label,
``needs-pat``, and the same function keeps that invariant too: at most one waiting label,
and none once the case waits on no one. With several people on a subject, it is the fact a
reader filters a backlog by. It is the mirror of a claim label: liaise reads a claim label
as a claim coming in, and writes a waiting label as a fact going out.

:func:`setup_labels` creates the labels a subject needs in each repository it binds
(``liaise setup``): its claim labels, its waiting labels, and one label per case state.

The labels go through :class:`liaise.github.GitHub` (``GhCli``, or ``FakeGitHub`` in
tests), since correspond has no label operations yet. Only a case's
``github:owner/repo#N`` conversations are projected; any other conversation has no labels.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from importlib import resources
from typing import Optional

from correspond.channels.github import REF_RE

from liaise.github import GitHub
from liaise.model import CASE_STATES, PR_STATES, PULL_KIND, Case
from liaise.subjects import HOLD_LABEL_SUFFIX, Subject, poll_ref

#: The channel whose conversations carry labels.
GITHUB_CHANNEL = "github"
#: Each state label's description and colour, in ``liaise/data``.
LABEL_SPECS_RESOURCE = "labels.json"
#: The colour of a label no spec describes: GitHub's own grey.
DFLT_LABEL_COLOR = "ededed"
#: What a claim label says on GitHub, formatted with the person it files an issue for.
CLAIM_LABEL_DESCRIPTION = (
    "Files the issue for {person}. It counts only when a relay sets it."
)
#: The states in which a case waits on a person: its reporter, asked a question or a
#: proposal. ``needs-owner`` waits on the operator, who has no waiting label.
WAITING_STATES = ("needs-partner",)
#: What a waiting label says on GitHub, formatted with the person the case waits on.
WAITING_LABEL_DESCRIPTION = "Waiting on {person} to answer. liaise sets and removes it."
#: The spec key, in ``labels.json``, of the label a person puts on a pull request to keep
#: liaise from merging it: ``<label_prefix>hold`` (see :func:`hold_label`).
HOLD_LABEL_SPEC = HOLD_LABEL_SUFFIX


def hold_label(subject: Subject) -> str:
    """The label that keeps liaise from merging a pull request of ``subject``: ``liaise:hold``.

    >>> from liaise.subjects import Policy
    >>> hold_label(Subject("app", ("github:example/app",), Policy(people={}, roles={})))
    'liaise:hold'
    """
    return f"{subject.label_prefix}{HOLD_LABEL_SUFFIX}"


def github_issue(conversation: str) -> Optional[tuple[str, int]]:
    """``(owner/repo, number)`` for an encoded ``github:owner/repo#N``, else None.

    >>> github_issue("github:example/app#12")
    ('example/app', 12)
    >>> github_issue("github:example/app") is None, github_issue("webinbox:example-site") is None
    (True, True)
    """
    channel, _, native_id = conversation.partition(":")
    match = REF_RE.match(native_id) if channel == GITHUB_CHANNEL else None
    if match is None or match["number"] is None:
        return None
    return f"{match['owner']}/{match['repo']}", int(match["number"])


def github_repos(subject: Subject) -> list[str]:
    """The ``owner/repo`` of each GitHub repository ``subject`` binds, once each, in binding order.

    A binding on one issue names its repository. A binding with a wildcard in its
    conversation part is polled on nothing (see :func:`liaise.subjects.poll_ref`), so it
    names none. Repositories compare without regard to case, as GitHub's do.
    """
    repos: dict[str, str] = {}
    for binding in subject.bindings:
        channel, _, native_id = (poll_ref(binding) or "").partition(":")
        match = REF_RE.match(native_id) if channel == GITHUB_CHANNEL else None
        if match is not None:
            repo = f"{match['owner']}/{match['repo']}"
            repos.setdefault(repo.casefold(), repo)
    return list(repos.values())


def waiting_label(case: Case, subject: Subject) -> Optional[str]:
    """The waiting label ``case`` carries now: its reporter's while it waits on them, else None.

    A case waits on its reporter in :data:`WAITING_STATES`, and the label is
    ``policy.waiting_labels[reporter]``. A reporter the policy gives no label has none.
    """
    if case.state not in WAITING_STATES:
        return None
    return subject.policy.waiting_labels.get(case.reporter)


def project_labels(
    case: Case,
    subject: Subject,
    *,
    labeler: GitHub,
    dry_run: bool = False,
    stale: Iterable[str] = (),
) -> list[str]:
    """Label each of ``case``'s GitHub issues with its state, and with no other state.

    For every ``github:owner/repo#N`` conversation of the case, the other
    ``<label_prefix><state>`` labels are removed and the current one is added (labels
    that are not state labels stay). When the subject has waiting labels, the other
    people's come off too, and :func:`waiting_label` goes on beside the state label while
    the case waits on its reporter. ``stale`` are waiting labels projected before that the
    subject no longer gives (turned off, or renamed): they come off as well, except a label
    that is now a claim label. Returns one line per issue. A dry run returns the lines and
    calls nothing on ``labeler``. A ``GitHubError`` from ``labeler`` propagates.
    """
    prefix = subject.label_prefix
    pull = case.kind == PULL_KIND
    vocabulary = PR_STATES if pull else CASE_STATES
    current = f"{prefix}{case.state}"
    others = [f"{prefix}{state}" for state in vocabulary if state != case.state]
    waiting = None if pull else waiting_label(case, subject)
    claims = subject.policy.claim_labels
    candidates = dict.fromkeys((*subject.policy.waiting_labels.values(), *stale))
    not_waiting = (
        []
        if pull
        else [
            label
            for label in candidates
            if label and label != waiting and label not in claims
        ]
    )
    shown = f"{current} and {waiting}" if waiting else current
    removing = f"any other {prefix} {'review' if pull else 'state'} label" + (
        " and waiting label" if not_waiting else ""
    )
    lines = []
    for conversation in case.conversations:
        issue = github_issue(conversation)
        if issue is None:
            continue
        if dry_run:
            lines.append(f"would label {conversation} {shown}, removing {removing}")
            continue
        repo, number = issue
        labeler.remove_labels(repo, number, [*others, *not_waiting], pull=pull)
        labeler.add_labels(
            repo, number, [current, *([waiting] if waiting else [])], pull=pull
        )
        lines.append(f"labelled {conversation} {shown}")
    return lines


def setup_labels(labeler: GitHub, subject: Subject) -> list[str]:
    """Create the labels ``subject`` needs in each GitHub repository it binds; a line per repository.

    Those are its ``policy.claim_labels``, the routing labels a relay puts on the issues it
    files; its ``policy.waiting_labels``, coloured as the state they go with; one
    ``<label_prefix><state>`` label per case state and per pull-request review state,
    described and coloured as ``data/labels.json`` says; and :func:`hold_label`. The
    repositories are the ones the subject binds and, when it reviews pull requests, the
    ones its ``[review]`` table adds (:func:`liaise.review.review_repos`). Idempotent,
    since ``create_label`` updates a label that already exists. A ``GitHubError`` from
    ``labeler`` propagates.
    """
    repos = github_repos(subject)
    for extra in subject.review.repos if subject.review is not None else ():
        if extra.casefold() not in {repo.casefold() for repo in repos}:
            repos.append(extra)
    if not repos:
        where = subject.source or subject.slug
        return [f"{where} binds no GitHub repository: no labels to create"]
    specs = json.loads(
        resources.files("liaise.data")
        .joinpath(LABEL_SPECS_RESOURCE)
        .read_text(encoding="utf-8")
    )
    claims = [
        (label, DFLT_LABEL_COLOR, CLAIM_LABEL_DESCRIPTION.format(person=person))
        for label, person in subject.policy.claim_labels.items()
    ]
    waiting_color = specs.get(WAITING_STATES[0], {}).get("color", DFLT_LABEL_COLOR)
    waiting = [
        (label, waiting_color, WAITING_LABEL_DESCRIPTION.format(person=person))
        for person, label in subject.policy.waiting_labels.items()
    ]
    states = [
        (
            f"{subject.label_prefix}{state}",
            specs.get(state, {}).get("color", DFLT_LABEL_COLOR),
            specs.get(state, {}).get("description", ""),
        )
        for state in (*CASE_STATES, *PR_STATES, HOLD_LABEL_SPEC)
    ]
    counted = f", {len(waiting)} waiting label(s)" if waiting else ""
    lines = []
    for repo in repos:
        for name, color, description in (*claims, *waiting, *states):
            labeler.create_label(repo, name, color=color, description=description)
        lines.append(
            f"{repo}: created {len(claims)} claim label(s){counted} and {len(states)} "
            f"state labels ({subject.label_prefix}<state>)"
        )
    return lines
