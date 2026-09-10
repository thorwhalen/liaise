"""Tests for liaise.messages: every liaise-authored comment template must
start with the partner's mention (#20) — asserted over the templates
themselves, not by re-typing the expected copy.
"""

from __future__ import annotations

import pytest

from liaise import messages
from liaise.config import PartnerConfig

REPO = "example/app"

#: Every function in liaise.messages that produces a partner-facing comment.
_TEMPLATES = (
    messages.budget_capped_message,
    messages.deployed_message,
    messages.nudge_message,
)


def _partner(**overrides) -> PartnerConfig:
    fields = dict(
        slug="pat",
        display_name="Pat",
        github_logins=("pat",),
        repo=REPO,
        brief="brief.md",
        label="partner:pat",
        notify_login="pat",
    )
    fields.update(overrides)
    return PartnerConfig(**fields)


@pytest.mark.parametrize("template", _TEMPLATES)
def test_every_template_starts_with_the_mention(template):
    partner = _partner(notify_login="pat")
    assert template(partner).startswith("@pat ")


@pytest.mark.parametrize("template", _TEMPLATES)
def test_every_template_uses_this_partner_specific_login(template):
    partner = _partner(notify_login="octocat")
    assert template(partner).startswith("@octocat ")


def test_mention_is_the_bare_at_login():
    assert messages.mention(_partner(notify_login="pat")) == "@pat"
