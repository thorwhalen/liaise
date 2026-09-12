"""Label projection: a case's state, shown on each of its GitHub issues as one label.

In 0.1 the ledger holds a case's state. The ``<label_prefix><state>`` label on its GitHub
issues (``liaise:working``) is a projection of it, kept for the partner and the owner to
see at a glance. :func:`project_labels` makes each issue carry exactly the current state's
label: the other state labels come off, then the current one goes on. It is the one place
a state label changes, so the one-label invariant is kept there.

:func:`setup_labels` creates the labels a subject needs in each repository it binds
(``liaise setup``): its claim labels, and one label per case state.

The labels go through :class:`liaise.github.GitHub` (``GhCli``, or ``FakeGitHub`` in
tests), since correspond has no label operations yet. Only a case's
``github:owner/repo#N`` conversations are projected; any other conversation has no labels.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Optional

from correspond.channels.github import REF_RE

from liaise.github import GitHub
from liaise.model import CASE_STATES, Case
from liaise.subjects import Subject, poll_ref

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


def project_labels(
    case: Case, subject: Subject, *, labeler: GitHub, dry_run: bool = False
) -> list[str]:
    """Label each of ``case``'s GitHub issues with its state, and with no other state.

    For every ``github:owner/repo#N`` conversation of the case, the other
    ``<label_prefix><state>`` labels are removed and the current one is added (labels
    that are not state labels stay). Returns one line per issue. A dry run returns the
    lines and calls nothing on ``labeler``. A ``GitHubError`` from ``labeler`` propagates.
    """
    prefix = subject.label_prefix
    current = f"{prefix}{case.state}"
    others = [f"{prefix}{state}" for state in CASE_STATES if state != case.state]
    lines = []
    for conversation in case.conversations:
        issue = github_issue(conversation)
        if issue is None:
            continue
        if dry_run:
            lines.append(
                f"would label {conversation} {current}, removing any other "
                f"{prefix} state label"
            )
            continue
        repo, number = issue
        labeler.remove_labels(repo, number, others)
        labeler.add_labels(repo, number, [current])
        lines.append(f"labelled {conversation} {current}")
    return lines


def setup_labels(labeler: GitHub, subject: Subject) -> list[str]:
    """Create the labels ``subject`` needs in each GitHub repository it binds; a line per repository.

    Those are its ``policy.claim_labels``, the routing labels a relay puts on the issues it
    files, and one ``<label_prefix><state>`` label per case state, described and coloured
    as ``data/labels.json`` says. Idempotent, since ``create_label`` updates a label that
    already exists. A ``GitHubError`` from ``labeler`` propagates.
    """
    repos = github_repos(subject)
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
    states = [
        (
            f"{subject.label_prefix}{state}",
            specs.get(state, {}).get("color", DFLT_LABEL_COLOR),
            specs.get(state, {}).get("description", ""),
        )
        for state in CASE_STATES
    ]
    lines = []
    for repo in repos:
        for name, color, description in (*claims, *states):
            labeler.create_label(repo, name, color=color, description=description)
        lines.append(
            f"{repo}: created {len(claims)} claim label(s) and {len(states)} state "
            f"labels ({subject.label_prefix}<state>)"
        )
    return lines
