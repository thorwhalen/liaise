"""The prompt composers (A.5, and liaise 0.1).

Both build the whole prompt handed to the coding agent, in a fixed order, and both are
written to a file and referenced by path, never passed on the command line.

- :func:`compose_case_prompt` (0.1) works on a case: the packaged 0.1 operating rules,
  the subject's brief, the case pointer, the outcome vocabulary, the verify and delivery
  commands, and the budget. The agent reports only through structured outcomes; it is
  never told to post, label or open anything.
- :func:`compose_prompt` (0.0.x) works on one GitHub issue: the operating rules, the
  partner brief, the issue pointer, the state contract (labels to set, comments to
  post), the verify/deploy commands, and the budget lines. :mod:`liaise.dispatch` uses it.
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Optional

from liaise.config import ConfigError, PartnerConfig
from liaise.github import Issue
from liaise.messages import mention
from liaise.model import OUTCOME_KINDS

if TYPE_CHECKING:
    from liaise.model import Case
    from liaise.subjects import Subject

#: fresh: a new dispatch. resume: continuing a stored session (A.3 — a new
#: partner comment on a `liaise:needs-partner` issue re-arms readiness and
#: dispatches a resume rather than a fresh run).
MODES = ("fresh", "resume")

#: The packaged rules each composer starts with, in `liaise/data`.
OPERATING_RULES_RESOURCE = "operating_rules.md"
CASE_OPERATING_RULES_RESOURCE = "operating_rules_01.md"
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


# ---- 0.0.x: one GitHub issue ----


def _operating_rules() -> str:
    return _packaged_text(OPERATING_RULES_RESOURCE)


def _partner_brief(partner: PartnerConfig) -> str:
    return Path(partner.brief).expanduser().read_text()


def _issue_pointer(issue: Issue, mode: str) -> str:
    lines = ["## Issue", "", issue.url]
    if mode == "resume":
        lines += [
            "",
            "This is a **resume** of a session already working on this issue "
            "(the partner commented again after the agent asked a question).",
        ]
    lines += [
        "",
        "Read the thread yourself with `gh` — this pointer is deliberately just "
        "the URL, not a copy of the issue body or comments, which may have "
        "changed since this prompt was composed.",
    ]
    return "\n".join(lines)


def _state_contract(partner: PartnerConfig, log_path: Optional[str]) -> str:
    prefix = partner.label_prefix
    lines = [
        "## State contract",
        "",
        f"This partner's `reply_mode` is `{partner.reply_mode}`. "
        + (
            "Post nothing to the thread; write every partner-facing comment to "
            "the dispatch log as a draft instead (see the operating rules above)."
            if partner.reply_mode == "draft"
            else "Post directly to the thread as the operating rules describe."
        ),
    ]
    if log_path:
        lines += [
            "",
            f"The dispatch log for this run is `{log_path}`. Append to it, never "
            "overwrite it: every draft, escalation, and note for the owner that "
            "the operating rules send to the dispatch log goes there. `liaise` "
            "adds the exit code and output after you stop. If you can't write to "
            "it, put that text at the end of your final message instead; the log "
            "records your output too.",
        ]
    if partner.notify_login:
        lines += [
            "",
            f"Every comment meant for the partner starts with `{mention(partner)}` "
            '— questions, progress notes, pushback, and the "it\'s live" note alike. '
            "This applies even in draft mode: write the mention into the draft itself.",
        ]
    lines += [
        "",
        "Set exactly one of these labels before you stop, matching what actually happened:",
        "",
        f"- `{prefix}needs-partner` — you asked a question and are waiting on the partner.",
        f"- `{prefix}needs-owner` — you escalated, or are declining, or hit a stop condition.",
        f"- `{prefix}deployed` — the change is live and the partner has been told to try it "
        f"(only when you deploy yourself; see the deploy command below).",
        f"- `{prefix}paused` — the partner asked you to wait.",
        "",
        f"Escalation money threshold: ${partner.escalate.money_usd:.2f}. "
        f"Scope beyond {partner.escalate.max_scope} needs the owner.",
    ]
    return "\n".join(lines)


def _commands(partner: PartnerConfig) -> str:
    lines = ["## Commands"]
    if partner.verify:
        lines += ["", "Verify (run before landing):", "", f"    {partner.verify}"]
    if partner.deploy_per == "issue" and partner.deploy:
        lines += [
            "",
            "Deploy (run yourself once landed, then post and set deployed):",
            "",
            f"    {partner.deploy}",
        ]
    elif partner.deploy_per == "batch":
        lines += [
            "",
            "Deploy: do not deploy yourself. `liaise` deploys once after the whole "
            "batch. Land the change and say in the dispatch log what to tell the "
            "partner once it's live.",
        ]
    return "\n".join(lines)


def _budget(partner: PartnerConfig) -> str:
    b = partner.budget
    return "\n".join(
        [
            "## Budget",
            "",
            f"- timeout: {b.timeout_minutes} minutes — enforced from outside: past this, "
            f"the dispatch is killed and reconciled as a crash, whatever you were doing.",
            f"- turn cap: {b.max_turns} turns — not enforced from outside; please self-monitor "
            f"and stop cleanly (right label, one-line comment saying why) before you'd exceed it.",
        ]
    )


def compose_prompt(
    partner: PartnerConfig,
    issue: Issue,
    mode: str,
    *,
    log_path: Optional[str] = None,
) -> str:
    """Build the full prompt for `issue`, in the fixed section order (A.5).

    `mode` is `"fresh"` for a new dispatch or `"resume"` for continuing a
    stored session. `log_path`, when given, is named in the State contract
    as the dispatch log the operating rules send drafts and escalations to.
    """
    _check_mode(mode)
    sections = [
        _operating_rules(),
        _partner_brief(partner),
        _issue_pointer(issue, mode),
        _state_contract(partner, log_path),
        _commands(partner),
        _budget(partner),
    ]
    return _join_sections(sections)


# ---- 0.1: one case ----


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
    """Build the prompt for one run on ``case``, in the fixed section order (liaise 0.1).

    The sections: the packaged 0.1 operating rules, the subject's brief, the case pointer
    (each conversation, as a URL where it has one), the outcome vocabulary, the verify
    and delivery commands, and the budget. Unlike :func:`compose_prompt`, the agent is
    never told to post, label or open anything: it reports only through the structured
    outcomes its run ends with.

    ``mode`` is ``"fresh"`` or ``"resume"``. ``runs_dir_note``, when given, is added as
    it is to the case section (the tick can say there where this run's files are kept).
    Raises ``ValueError`` for an unknown mode, and :class:`~liaise.config.ConfigError`
    for a configured brief that cannot be read.
    """
    _check_mode(mode)
    sections = [
        _packaged_text(CASE_OPERATING_RULES_RESOURCE),
        _subject_brief(subject),
        _case_pointer(case, mode, runs_dir_note=runs_dir_note),
        _outcomes(subject),
        _case_commands(subject),
        _case_budget(subject),
    ]
    return _join_sections(sections)
