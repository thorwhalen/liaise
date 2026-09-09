"""Tests for liaise.prompt: compose_prompt includes every section, in order."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from liaise.config import DispatchConfig, EscalateConfig, PartnerConfig
from liaise.github import Issue
from liaise.prompt import compose_prompt

REPO = "example/app"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _partner(tmp_path, **overrides) -> PartnerConfig:
    brief = tmp_path / "brief.md"
    brief.write_text("Pat likes short, plain answers.\n")
    fields = dict(
        slug="pat",
        display_name="Pat",
        github_logins=("pat",),
        repo=REPO,
        brief=str(brief),
        label="partner:pat",
        reply_mode="draft",
        dispatch=DispatchConfig(),
        verify="run the checks",
        deploy="ship it",
        escalate=EscalateConfig(money_usd=25.0, max_scope="half a day"),
    )
    fields.update(overrides)
    return PartnerConfig(**fields)


def _issue() -> Issue:
    return Issue(
        repo=REPO,
        number=42,
        title="button broken",
        author="pat",
        body="it does nothing",
        created_at=T0,
        updated_at=T0,
        state="open",
    )


def test_compose_prompt_includes_every_section_in_order(tmp_path):
    partner = _partner(tmp_path)
    issue = _issue()
    prompt = compose_prompt(partner, issue, "fresh")

    # every section is present
    assert "Operating rules" in prompt
    assert "Pat likes short, plain answers." in prompt
    assert issue.url in prompt
    assert "State contract" in prompt
    assert "Commands" in prompt
    assert "Budget" in prompt

    # in the design's order: operating rules, brief, issue pointer, state
    # contract, commands, budget
    order = [
        prompt.index("Operating rules"),
        prompt.index("Pat likes short, plain answers."),
        prompt.index(issue.url),
        prompt.index("State contract"),
        prompt.index("Commands"),
        prompt.index("Budget"),
    ]
    assert order == sorted(order)


def test_compose_prompt_rejects_unknown_mode(tmp_path):
    partner = _partner(tmp_path)
    with pytest.raises(ValueError):
        compose_prompt(partner, _issue(), "sideways")


def test_compose_prompt_resume_mode_notes_the_resume(tmp_path):
    partner = _partner(tmp_path)
    prompt = compose_prompt(partner, _issue(), "resume")
    assert "resume" in prompt.lower()


def test_draft_reply_mode_is_stated_in_the_state_contract(tmp_path):
    partner = _partner(tmp_path, reply_mode="draft")
    prompt = compose_prompt(partner, _issue(), "fresh")
    assert "`reply_mode` is `draft`" in prompt
    assert "Post nothing to the thread" in prompt


def test_direct_reply_mode_is_stated_in_the_state_contract(tmp_path):
    partner = _partner(tmp_path, reply_mode="direct")
    prompt = compose_prompt(partner, _issue(), "fresh")
    assert "`reply_mode` is `direct`" in prompt


def test_verify_command_always_included(tmp_path):
    partner = _partner(tmp_path, verify="pytest -q")
    prompt = compose_prompt(partner, _issue(), "fresh")
    assert "pytest -q" in prompt


def test_deploy_command_included_when_deploy_per_issue(tmp_path):
    partner = _partner(tmp_path, deploy_per="issue", deploy="./deploy.sh")
    prompt = compose_prompt(partner, _issue(), "fresh")
    assert "./deploy.sh" in prompt


def test_deploy_command_not_included_when_deploy_per_batch(tmp_path):
    partner = _partner(tmp_path, deploy_per="batch", deploy="./deploy.sh")
    prompt = compose_prompt(partner, _issue(), "fresh")
    assert "./deploy.sh" not in prompt
    assert "do not deploy yourself" in prompt


def test_operating_rules_written_in_full_includes_every_bullet(tmp_path):
    partner = _partner(tmp_path)
    prompt = compose_prompt(partner, _issue(), "fresh")
    for phrase in [
        "Speak plainly",
        "Clarify systematically",
        "Push back with a reason and an alternative",
        "Escalate to the owner",
        "Work in the repo's own conventions",
        "Close the loop",
        "One issue per run",
        "Stop conditions",
        "In `draft` reply mode",
    ]:
        assert phrase in prompt
