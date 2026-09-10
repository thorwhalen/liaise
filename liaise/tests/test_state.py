"""Tests for liaise.state: the one-label invariant, and `liaise setup`.

The one-label test is mutation-checked (see the note on
test_set_state_removes_every_other_state_label): comment out the
`gh.remove_labels(...)` line in `set_state` and this test must fail.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from liaise.cli import setup as cli_setup
from liaise.config import PartnerConfig
from liaise.github import FakeGitHub, Issue
from liaise.state import STATE_LABELS, current_state, set_state, state_label

REPO = "example/app"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _partner(**overrides) -> PartnerConfig:
    fields = dict(
        slug="pat",
        display_name="Pat",
        github_logins=("pat",),
        repo=REPO,
        brief="brief.md",
        label="partner:pat",
    )
    fields.update(overrides)
    return PartnerConfig(**fields)


def _issue(*, labels=()) -> Issue:
    return Issue(
        repo=REPO,
        number=1,
        title="a bug",
        author="pat",
        body="broken",
        created_at=T0,
        updated_at=T0,
        state="open",
        labels=labels,
    )


def test_state_label_rejects_unknown_state():
    partner = _partner()
    with pytest.raises(ValueError):
        state_label(partner, "not-a-real-state")


def test_state_label_applies_configured_prefix():
    partner = _partner(label_prefix="custom:")
    assert state_label(partner, "working") == "custom:working"


def test_current_state_none_when_no_state_label():
    partner = _partner()
    issue = _issue(labels=("partner:pat",))
    assert current_state(issue, partner) is None


def test_current_state_reads_the_present_label():
    partner = _partner()
    issue = _issue(labels=("partner:pat", "liaise:working"))
    assert current_state(issue, partner) == "working"


def test_current_state_raises_if_invariant_already_broken():
    partner = _partner()
    issue = _issue(labels=("liaise:working", "liaise:paused"))
    with pytest.raises(ValueError):
        current_state(issue, partner)


def test_set_state_removes_every_other_state_label():
    """The one-label invariant (A.4), mutation-checked.

    Every other state label present is removed when transitioning; only the
    target state label survives. If `set_state` stopped calling
    `gh.remove_labels` for the other states, this test would start seeing
    both `liaise:intake` and `liaise:working` on the issue and fail — that is
    the mutation check.
    """
    partner = _partner()
    fake = FakeGitHub([_issue(labels=("partner:pat", "liaise:intake"))])
    issue = fake.get_issue(REPO, 1)

    set_state(fake, issue, partner, "working")

    updated = fake.get_issue(REPO, 1)
    state_labels_present = [l for l in updated.labels if l in {
        state_label(partner, s) for s in STATE_LABELS
    }]
    assert state_labels_present == ["liaise:working"]
    assert "partner:pat" in updated.labels  # non-state labels are untouched


def test_set_state_transitions_repeatedly_stay_single_label():
    partner = _partner()
    fake = FakeGitHub([_issue(labels=())])
    issue = fake.get_issue(REPO, 1)

    for state in ("intake", "working", "needs-partner", "deployed"):
        issue = fake.get_issue(REPO, 1)
        set_state(fake, issue, partner, state)
        issue = fake.get_issue(REPO, 1)
        assert current_state(issue, partner) == state


def test_setup_creates_partner_label_and_every_state_label():
    partner = _partner()
    fake = FakeGitHub()
    from liaise.state import setup

    setup(fake, partner)
    created = fake.labels_created(REPO)

    assert "partner:pat" in created
    for state in STATE_LABELS:
        assert f"liaise:{state}" in created
        assert created[f"liaise:{state}"]  # plain-language description, non-empty

    # L-2: operating_rules.md tells the agent to open a "discovered" note for
    # the owner; setup must actually create that label or the word is a
    # dead reference.
    assert "discovered" in created
    assert created["discovered"]


def test_setup_is_idempotent():
    partner = _partner()
    fake = FakeGitHub()
    from liaise.state import setup

    setup(fake, partner)
    first = fake.labels_created(REPO)
    setup(fake, partner)  # run again — no error, same result
    second = fake.labels_created(REPO)
    assert first == second


def test_cli_setup_reports_what_it_created(tmp_path):
    root = tmp_path / "config"
    (root / "partners").mkdir(parents=True)
    (root / "briefs").mkdir()
    (root / "briefs" / "pat.md").write_text("hi\n")
    # .as_posix(): TOML treats "\" as an escape char, so a raw Windows path
    # breaks parsing (tomllib.TOMLDecodeError, confirmed in CI).
    (root / "config.toml").write_text(
        f'owner_login = "owner"\nstate_dir = "{(tmp_path / "state").as_posix()}"\n'
    )
    (root / "partners" / "pat.toml").write_text(
        f'display_name = "Pat"\n'
        f'github_logins = ["pat"]\n'
        f'repo = "{REPO}"\n'
        f'brief = "{(root / "briefs" / "pat.md").as_posix()}"\n'
    )
    fake = FakeGitHub()
    output = cli_setup("pat", root=str(root), gh=fake)
    assert "partner:pat" in output
    assert str(len(STATE_LABELS)) in output
    assert fake.labels_created(REPO)
