"""Tests for liaise.prompt.compose_case_prompt: the 0.1 case prompt and its operating rules."""

from __future__ import annotations

from datetime import datetime, timezone
from importlib import resources

import pytest

from liaise.config import ConfigError, EscalateConfig
from liaise.model import CASE_STATES, OUTCOME_KINDS, Case
from liaise.prompt import compose_case_prompt, conversation_link
from liaise.subjects import BudgetPolicy, Delivery, Policy, Subject

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
BRIEF = "Pat likes short, plain answers."
ISSUE_REF = "github:example/app#12"
ISSUE_URL = "https://github.com/example/app/issues/12"
WEB_REF = "webinbox:example-site#3"


def _subject(tmp_path, **overrides) -> Subject:
    brief = tmp_path / "pat.md"
    brief.write_text(BRIEF + "\n", encoding="utf-8")
    fields = dict(
        slug="pat",
        bindings=("github:example/app?labels=partner:pat",),
        policy=Policy(
            people={"github:pat": "pat"},
            roles={"pat": "partner"},
            escalate=EscalateConfig(money_usd=25.0, max_scope="half a day"),
            budget=BudgetPolicy(timeout_minutes=45, max_turns=120),
        ),
        display_name="Example app",
        brief=str(brief),
        verify="npm test",
        delivery=Delivery(kind="deploy", per="batch", command="./deploy.sh"),
    )
    fields.update(overrides)
    return Subject(**fields)


def _case(**overrides) -> Case:
    fields = dict(
        id="pat-1",
        subject="pat",
        conversations=(ISSUE_REF, WEB_REF),
        reporter="pat",
        state="intake",
        created_at=T0,
        updated_at=T0,
    )
    fields.update(overrides)
    return Case(**fields)


def _prompt(tmp_path, mode="fresh", **subject_overrides) -> str:
    return compose_case_prompt(_subject(tmp_path, **subject_overrides), _case(), mode)


def _rules() -> str:
    return (
        resources.files("liaise.data")
        .joinpath("operating_rules.md")
        .read_text(encoding="utf-8")
    )


def test_sections_come_in_order(tmp_path):
    prompt = _prompt(tmp_path)
    markers = (
        "# Operating rules",
        BRIEF,
        "## Case",
        ISSUE_URL,
        "## Outcomes",
        "## Commands",
        "## Budget",
    )
    order = [prompt.index(marker) for marker in markers]
    assert order == sorted(order)
    assert prompt.startswith("# Operating rules")


@pytest.mark.parametrize("prefix", ["liaise:", "custom:"])
@pytest.mark.parametrize("mode", ["fresh", "resume"])
def test_no_label_names_anywhere_in_the_prompt(tmp_path, prefix, mode):
    prompt = _prompt(tmp_path, mode, label_prefix=prefix)
    assert prefix not in prompt
    for state in CASE_STATES:
        assert f"{prefix}{state}" not in prompt
    assert "needs-partner" not in prompt
    assert "needs-owner" not in prompt


def test_the_rules_forbid_posting_labelling_and_opening_issues():
    rules = _rules()
    assert "Report only through outcomes" in rules
    assert "Never post a comment, set or remove a label, open or close an issue" in rules
    assert "liaise:" not in rules


def test_github_refs_become_urls_and_other_refs_are_listed_as_they_are(tmp_path):
    prompt = _prompt(tmp_path)
    assert f"- {ISSUE_URL}" in prompt
    assert f"- {WEB_REF}" in prompt
    assert f"- {ISSUE_REF}" not in prompt


@pytest.mark.parametrize(
    "ref", ["github:example/app", "github:example/app#x", "webinbox:example-site"]
)
def test_conversation_link_leaves_other_refs_alone(ref):
    assert conversation_link(ref) == ref


def test_resume_mode_is_noted(tmp_path):
    assert "**resume**" in _prompt(tmp_path, "resume")
    assert "**resume**" not in _prompt(tmp_path, "fresh")


def test_unknown_mode_is_refused(tmp_path):
    with pytest.raises(ValueError, match="mode must be one of"):
        _prompt(tmp_path, "sideways")


def test_rules_carry_the_ask_escalate_and_one_case_guidance(tmp_path):
    prompt = _prompt(tmp_path)
    for phrase in [
        "Speak plainly",
        "`ask` outcome with numbered questions, each carrying a suggested default",
        "Push back with a reason and an alternative",
        "Escalate to the owner",
        "money above the configured threshold",
        "declining a request",
        "reversing something the partner explicitly chose",
        "work beyond the configured scope",
        "billing, authentication, access or stored data",
        "the owner's intent is unclear",
        "A `decline` outcome is treated exactly as an `escalate`",
        "a branch, a pull request, CI green",
        "Never force-push",
        "One case per run",
    ]:
        assert phrase in prompt, phrase


def test_outcomes_section_lists_every_kind_and_requires_the_structured_result(
    tmp_path,
):
    prompt = _prompt(tmp_path)
    section = prompt[prompt.index("## Outcomes") : prompt.index("## Commands")]
    for kind in OUTCOME_KINDS:
        assert f"- `{kind}`: " in section, kind
    assert "structured result matching the JSON schema you were given" in section
    assert "$25.00" in section
    assert "half a day" in section


def test_batch_delivery_tells_the_agent_not_to_deploy(tmp_path):
    prompt = _prompt(tmp_path)
    assert "npm test" in prompt
    assert "do not deploy yourself" in prompt
    assert "./deploy.sh" not in prompt


def test_per_case_delivery_names_the_deploy_command(tmp_path):
    delivery = Delivery(kind="deploy", per="issue", command="./deploy.sh")
    prompt = _prompt(tmp_path, delivery=delivery)
    assert "./deploy.sh" in prompt
    assert "do not deploy yourself" not in prompt


def test_pr_only_delivery_stops_at_a_pull_request(tmp_path):
    prompt = _prompt(tmp_path, delivery=Delivery(kind="pr_only", command="./deploy.sh"))
    assert "stop at a pull request" in prompt
    assert "./deploy.sh" not in prompt


def test_budget_distinguishes_the_enforced_timeout_from_the_advisory_turn_cap(
    tmp_path,
):
    budget = _prompt(tmp_path).split("## Budget", 1)[1]
    assert "45 minutes, enforced from outside" in budget
    assert "120 turns, not enforced from outside" in budget


def test_runs_dir_note_goes_in_the_case_section(tmp_path):
    note = "This run's files are kept in runs/r1."
    prompt = compose_case_prompt(
        _subject(tmp_path), _case(), "fresh", runs_dir_note=note
    )
    assert prompt.index("## Case") < prompt.index(note) < prompt.index("## Outcomes")


def test_a_subject_without_a_brief_says_so(tmp_path):
    assert "No brief is configured for Example app." in _prompt(tmp_path, brief="")


def test_a_brief_that_cannot_be_read_is_a_config_error_naming_it(tmp_path):
    with pytest.raises(ConfigError, match="missing.md"):
        _prompt(tmp_path, brief=str(tmp_path / "missing.md"))
