"""The prompt composer (A.5).

`compose_prompt` builds the whole prompt handed to the coding agent, in a
fixed order: the packaged operating rules, the partner brief, the issue
pointer, the state contract, the verify/deploy commands, and the budget
lines. The prompt is written to a temp file and referenced by path by
:mod:`liaise.dispatch` — never passed on the command line.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from liaise.config import PartnerConfig
from liaise.github import Issue

#: fresh: a new dispatch. resume: continuing a stored session (A.3 — a new
#: partner comment on a `liaise:needs-partner` issue re-arms readiness and
#: dispatches a resume rather than a fresh run).
MODES = ("fresh", "resume")


def _operating_rules() -> str:
    return resources.files("liaise.data").joinpath("operating_rules.md").read_text()


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


def _state_contract(partner: PartnerConfig) -> str:
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
        lines += ["", "Deploy (run yourself once landed, then post and set deployed):", "", f"    {partner.deploy}"]
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


def compose_prompt(partner: PartnerConfig, issue: Issue, mode: str) -> str:
    """Build the full prompt for `issue`, in the fixed section order (A.5).

    `mode` is `"fresh"` for a new dispatch or `"resume"` for continuing a
    stored session.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")

    sections = [
        _operating_rules(),
        _partner_brief(partner),
        _issue_pointer(issue, mode),
        _state_contract(partner),
        _commands(partner),
        _budget(partner),
    ]
    return "\n\n---\n\n".join(s.strip() for s in sections) + "\n"
