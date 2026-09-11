"""The prompt composer: the whole prompt a processor run on one case starts from.

:func:`compose_case_prompt` builds it in a fixed order: the packaged operating rules, the
subject's brief, the case pointer, the outcome vocabulary, the verify and delivery
commands, and the budget. The processor writes it to a file and puts only a pointer to that
file on the command line. The agent reports only through the structured outcomes its run
ends with: it is never told to post, label or open anything.
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Optional

from liaise.config import ConfigError
from liaise.model import OUTCOME_KINDS

if TYPE_CHECKING:
    from liaise.model import Case
    from liaise.subjects import Subject

#: fresh: a new session. resume: continuing the case's stored session, because someone
#: wrote again since it last stopped.
MODES = ("fresh", "resume")

#: The packaged rules every prompt starts with, in `liaise/data`.
OPERATING_RULES_RESOURCE = "operating_rules.md"
_SECTION_SEPARATOR = "\n\n---\n\n"

#: `github:owner/repo#N`: the encoded reference of a GitHub issue or pull request.
_GITHUB_ISSUE_REF_RE = re.compile(
    r"^github:(?P<owner>[A-Za-z0-9][A-Za-z0-9-]*)/(?P<repo>[A-Za-z0-9._-]+)"
    r"#(?P<number>\d+)$"
)
#: GitHub redirects `/issues/N` to the pull request's page when N is a pull request.
GITHUB_ISSUE_URL = "https://github.com/{owner}/{repo}/issues/{number}"

#: What each outcome kind is for, as the agent reads it. Listed in the order of
#: :data:`liaise.model.OUTCOME_KINDS`, which must all have an entry.
OUTCOME_GUIDANCE = MappingProxyType(
    {
        "ask": (
            "you need the partner to answer before you can go on. Put the questions in "
            "`questions`, one per entry and in order, each carrying its suggested "
            "default, as in `Which browsers should this work in? (default: all current "
            "ones)`. `text` is an optional lead-in."
        ),
        "reply": (
            "a message to the partner that needs no answer: progress, an explanation, "
            "or pushback with a reason and an alternative. Put it in `text`."
        ),
        "escalate": (
            "the owner must decide (see the escalation rule). `text` is the message to "
            "the partner, written so the owner can send it as is; `reason` tells the "
            "owner why it needs them."
        ),
        "propose": (
            "you have a concrete option and want the partner's go-ahead before doing "
            "it. Describe it in `text`."
        ),
        "deliver": (
            "the change has landed (branch, pull request, CI green, verify passing) and "
            "is ready for the partner. `text` says what changed and what to try, in the "
            "partner's terms."
        ),
        "decline": (
            "you think the request should not be done. It is treated as `escalate`: the "
            "owner decides. Write the message to the partner in `text` and your reason "
            "in `reason`."
        ),
        "defer": (
            "the case should wait, because the partner asked you to or something "
            "outside this case has to happen first. `reason` says what it waits for."
        ),
        "note": (
            "something for the owner only, never shown to the partner: a problem you "
            "noticed, a follow-up worth doing, where you stopped. Put it in `text`."
        ),
    }
)


def _packaged_text(name: str) -> str:
    return resources.files("liaise.data").joinpath(name).read_text(encoding="utf-8")


def _join_sections(sections) -> str:
    return _SECTION_SEPARATOR.join(s.strip() for s in sections) + "\n"


def _check_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")


def conversation_link(ref: str) -> str:
    """Where the conversation that the encoded reference ``ref`` names can be read.

    A GitHub issue or pull request (``github:owner/repo#N``) becomes its web URL. Any
    other reference is returned as it is.

    >>> conversation_link("github:example/app#12")
    'https://github.com/example/app/issues/12'
    >>> conversation_link("webinbox:example-site")
    'webinbox:example-site'
    """
    match = _GITHUB_ISSUE_REF_RE.match(ref)
    return GITHUB_ISSUE_URL.format(**match.groupdict()) if match else ref


def _subject_brief(subject: Subject) -> str:
    if not subject.brief:
        name = subject.display_name or subject.slug
        return f"## Brief\n\nNo brief is configured for {name}."
    try:
        return Path(subject.brief).expanduser().read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(
            f"{subject.source or subject.slug}: cannot read the brief {subject.brief!r} "
            f"({error.strerror or error}). Write the brief there, or point `brief` at "
            "a file that exists."
        ) from None


def _case_pointer(case: Case, mode: str, *, runs_dir_note: Optional[str]) -> str:
    lines = ["## Case", "", f"Case `{case.id}`. Its conversations:", ""]
    lines += [f"- {conversation_link(ref)}" for ref in case.conversations]
    if mode == "resume":
        lines += [
            "",
            "This is a **resume** of the session already working on this case: someone "
            "wrote again since it last stopped. Pick up where you left off.",
        ]
    lines += [
        "",
        "Read each conversation yourself before acting: a GitHub one with `gh`, any "
        "other with `correspond read <reference>`. This list says where they are, not "
        "what they say, which may have changed since this prompt was composed. Reading "
        "is fine; writing to them is not (see the operating rules).",
    ]
    if runs_dir_note:
        lines += ["", runs_dir_note]
    return "\n".join(lines)


def _outcomes(subject: Subject) -> str:
    escalate = subject.policy.escalate
    lines = [
        "## Outcomes",
        "",
        "End this run with a structured result matching the JSON schema you were "
        "given: a list of `outcomes` and a short `summary` for the owner. If this "
        "section and that schema ever disagree, the schema wins.",
        "",
        "Each outcome has a `kind` from this closed list, and uses `text`, `questions` "
        "and `reason` as its kind needs:",
        "",
    ]
    lines += [f"- `{kind}`: {OUTCOME_GUIDANCE[kind]}" for kind in OUTCOME_KINDS]
    lines += [
        "",
        "Write every partner-facing `text` as if it will be sent as is. Whether it goes "
        "out directly or is held for the owner first is decided after the run, and "
        "`liaise` adds any mention the channel needs.",
        "",
        f"Escalation thresholds for this subject: money above "
        f"${escalate.money_usd:.2f}, or work beyond {escalate.max_scope}.",
    ]
    return "\n".join(lines)


def _case_commands(subject: Subject) -> str:
    lines = ["## Commands"]
    if subject.verify:
        lines += ["", "Verify (run before landing):", "", f"    {subject.verify}"]
    delivery = subject.delivery
    if delivery.kind == "pr_only":
        lines += [
            "",
            "Delivery: stop at a pull request with CI green. Do not merge or deploy; "
            "report `deliver` once the pull request is ready.",
        ]
    elif delivery.per == "batch":
        lines += [
            "",
            "Deploy: do not deploy yourself. `liaise` deploys once after the whole "
            "batch. Land the change and report `deliver` with what to tell the partner "
            "once it is live.",
        ]
    elif delivery.command:
        lines += [
            "",
            "Deploy (run it yourself once the change has landed, then report "
            "`deliver`):",
            "",
            f"    {delivery.command}",
        ]
    return "\n".join(lines)


def _case_budget(subject: Subject) -> str:
    b = subject.policy.budget
    return "\n".join(
        [
            "## Budget",
            "",
            f"- timeout: {b.timeout_minutes} minutes, enforced from outside: past it "
            "the run is stopped and handed to the owner, whatever you were doing.",
            f"- turn cap: {b.max_turns} turns, not enforced from outside: watch it "
            "yourself, and before you would pass it, stop cleanly with an `escalate` "
            "saying where you stopped and why.",
        ]
    )


def compose_case_prompt(
    subject: Subject,
    case: Case,
    mode: str,
    *,
    runs_dir_note: Optional[str] = None,
) -> str:
    """Build the prompt for one run on ``case``, in the fixed section order.

    The sections: the packaged operating rules, the subject's brief, the case pointer
    (each conversation, as a URL where it has one), the outcome vocabulary, the verify
    and delivery commands, and the budget. The agent is never told to post, label or open
    anything: it reports only through the structured outcomes its run ends with.

    ``mode`` is ``"fresh"`` or ``"resume"``. ``runs_dir_note``, when given, is added as
    it is to the case section (the tick can say there where this run's files are kept).
    Raises ``ValueError`` for an unknown mode, and :class:`~liaise.config.ConfigError`
    for a configured brief that cannot be read.
    """
    _check_mode(mode)
    sections = [
        _packaged_text(OPERATING_RULES_RESOURCE),
        _subject_brief(subject),
        _case_pointer(case, mode, runs_dir_note=runs_dir_note),
        _outcomes(subject),
        _case_commands(subject),
        _case_budget(subject),
    ]
    return _join_sections(sections)
