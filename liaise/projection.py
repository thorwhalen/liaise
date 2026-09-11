"""Label projection: a case's state, shown on each of its GitHub issues as one label.

In 0.1 the ledger holds a case's state. The ``<label_prefix><state>`` label on its GitHub
issues (``liaise:working``) is a projection of it, kept for the partner and the owner to
see at a glance. :func:`project_labels` makes each issue carry exactly the current state's
label: the other state labels come off, then the current one goes on, which is the 0.0.x
``liaise.state.set_state`` choke point, now driven by the case.

The labels go through :class:`liaise.github.GitHub` (``GhCli``, or ``FakeGitHub`` in
tests), since correspond has no label operations yet. Only a case's
``github:owner/repo#N`` conversations are projected; any other conversation has no labels.
"""

from __future__ import annotations

from typing import Optional

from correspond.channels.github import REF_RE

from liaise.github import GitHub
from liaise.model import CASE_STATES, Case
from liaise.subjects import Subject

#: The channel whose conversations carry labels.
GITHUB_CHANNEL = "github"


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
