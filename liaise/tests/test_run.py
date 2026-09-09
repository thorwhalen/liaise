"""Tests for liaise.run: run_once (intake, dispatch, batch deploy) and liaise run/status."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from liaise.cli import run as cli_run
from liaise.cli import status as cli_status
from liaise.config import Budget, Config, DispatchConfig, PartnerConfig
from liaise.dispatch import EchoDispatcher
from liaise.github import FakeGitHub, Issue
from liaise.run import last_run_age, run_once
from liaise.state import current_state

REPO = "example/app"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _partner(tmp_path, **overrides) -> PartnerConfig:
    brief = tmp_path / "brief.md"
    brief.write_text("plain and short\n")
    fields = dict(
        slug="pat",
        display_name="Pat",
        github_logins=("pat",),
        repo=REPO,
        brief=str(brief),
        label="partner:pat",
        quiet_minutes=10,
        dispatch=DispatchConfig(cwd=str(tmp_path)),
        budget=Budget(timeout_minutes=1, daily_dispatches=5),
        deploy="./deploy.sh",
    )
    fields.update(overrides)
    return PartnerConfig(**fields)


def _config(tmp_path, **overrides) -> Config:
    from liaise.config import GlobalConfig

    partner = _partner(tmp_path, **overrides)
    glob = GlobalConfig(owner_login="owner", state_dir=str(tmp_path / "state"))
    return Config(global_=glob, partners={partner.slug: partner})


def _issue(**overrides) -> Issue:
    fields = dict(
        repo=REPO,
        number=1,
        title="a bug",
        author="pat",
        body="broken",
        created_at=T0,
        updated_at=T0,
        state="open",
        labels=(),
    )
    fields.update(overrides)
    return Issue(**fields)


# ---- intake: first sight gets the partner label + liaise:intake ----


def test_run_once_intakes_a_new_issue(tmp_path):
    config = _config(tmp_path)
    partner = config.partner("pat")
    fake = FakeGitHub([_issue(labels=())])
    dispatcher = EchoDispatcher()
    store: dict = {}

    report = run_once(fake, dispatcher, store, config, now=T0)

    assert any(item.action == "intake" for item in report.plan)
    issue = fake.get_issue(REPO, 1)
    assert partner.label in issue.labels
    assert current_state(issue, partner) == "intake"


# ---- --dry-run: prints the plan, changes nothing ----


def test_dry_run_prints_plan_and_changes_nothing(tmp_path):
    config = _config(tmp_path)
    fake = FakeGitHub([_issue(labels=())])
    dispatcher = EchoDispatcher()
    store: dict = {}

    report = run_once(fake, dispatcher, store, config, dry_run=True, now=T0)

    assert len(report.plan) == 1
    assert report.plan[0].action == "intake"
    # changed nothing:
    issue = fake.get_issue(REPO, 1)
    assert issue.labels == ()
    assert current_state(issue, config.partner("pat")) is None
    assert dispatcher.jobs == []
    assert "last_run" not in store


def test_dry_run_on_a_ready_issue_plans_dispatch_without_dispatching(tmp_path):
    config = _config(tmp_path)
    partner = config.partner("pat")
    fake = FakeGitHub([_issue(labels=(partner.label, "liaise:intake"))])
    dispatcher = EchoDispatcher()
    store: dict = {}

    now = T0 + timedelta(minutes=20)  # well past the 10-minute quiet window
    report = run_once(fake, dispatcher, store, config, dry_run=True, now=now)

    assert [item.action for item in report.plan] == ["dispatch"]
    assert dispatcher.jobs == []
    assert current_state(fake.get_issue(REPO, 1), partner) == "intake"  # unchanged


def test_cli_run_once_dry_run_prints_the_plan(tmp_path):
    root = tmp_path / "config"
    (root / "partners").mkdir(parents=True)
    (root / "briefs").mkdir()
    (root / "briefs" / "pat.md").write_text("hi\n")
    (root / "config.toml").write_text(
        f'owner_login = "owner"\nstate_dir = "{tmp_path / "state"}"\n'
    )
    (root / "partners" / "pat.toml").write_text(
        f'display_name = "Pat"\n'
        f'github_logins = ["pat"]\n'
        f'repo = "{REPO}"\n'
        f'brief = "{root / "briefs" / "pat.md"}"\n'
    )
    fake = FakeGitHub([_issue(labels=())])
    output = cli_run(root=str(root), once=True, dry_run=True, gh=fake, dispatcher=EchoDispatcher(), store={})
    assert "plan (" in output
    assert "intake" in output


# ---- dispatch + batch deploy: runs once, posts + deploys per issue ----


def test_run_once_dispatches_ready_issue_and_batch_deploys(tmp_path):
    deploy_script = tmp_path / "deploy.sh"
    deploy_script.write_text("#!/bin/sh\ntrue\n")
    deploy_script.chmod(0o755)
    config = _config(tmp_path, deploy_per="batch", deploy=str(deploy_script))
    partner = config.partner("pat")
    fake = FakeGitHub([_issue(labels=(partner.label, "liaise:intake"))])

    class LandingDispatcher:
        """Simulates an agent that lands the change but (per deploy_per=batch)
        does not deploy or change the label itself."""

        def dispatch(self, job):
            from liaise.dispatch import DispatchResult

            return DispatchResult(returncode=0, session_id="sess-1")

    store: dict = {}
    now = T0 + timedelta(minutes=20)

    report = run_once(fake, LandingDispatcher(), store, config, now=now)

    assert report.deployed == {"pat": [1]}
    issue = fake.get_issue(REPO, 1)
    assert current_state(issue, partner) == "deployed"
    assert len(issue.comments) == 1
    assert "live" in issue.comments[0].body.lower()


def test_batch_deploy_runs_the_deploy_command_once_for_multiple_issues(tmp_path, monkeypatch):
    config = _config(tmp_path, deploy_per="batch")
    partner = config.partner("pat")
    fake = FakeGitHub(
        [
            _issue(number=1, labels=(partner.label, "liaise:intake")),
            _issue(number=2, labels=(partner.label, "liaise:intake")),
        ]
    )

    class LandingDispatcher:
        def dispatch(self, job):
            from liaise.dispatch import DispatchResult

            return DispatchResult(returncode=0, session_id="sess-1")

    calls = []
    import liaise.run as run_module

    monkeypatch.setattr(
        run_module.subprocess, "run", lambda *a, **k: calls.append(a) or None
    )

    store: dict = {}
    now = T0 + timedelta(minutes=20)
    report = run_once(fake, LandingDispatcher(), store, config, now=now)

    assert len(calls) == 1  # deploy command run exactly once for the whole batch
    assert sorted(report.deployed["pat"]) == [1, 2]


# ---- liaise status ----


def _write_config_root(tmp_path):
    root = tmp_path / "config"
    (root / "partners").mkdir(parents=True)
    (root / "briefs").mkdir()
    (root / "briefs" / "pat.md").write_text("hi\n")
    (root / "config.toml").write_text(
        f'owner_login = "owner"\nstate_dir = "{tmp_path / "state"}"\n'
    )
    (root / "partners" / "pat.toml").write_text(
        f'display_name = "Pat"\n'
        f'github_logins = ["pat"]\n'
        f'repo = "{REPO}"\n'
        f'brief = "{root / "briefs" / "pat.md"}"\n'
    )
    return root


def test_status_reports_last_run_age(tmp_path):
    root = _write_config_root(tmp_path)
    fake = FakeGitHub([])
    store: dict = {"last_run": T0.isoformat()}
    output = cli_status(root=str(root), gh=fake, store=store)
    assert "last_run" in output


def test_last_run_age_none_when_never_run():
    assert last_run_age({}) is None


def test_last_run_age_computed_from_stamp():
    store = {"last_run": T0.isoformat()}
    later = T0 + timedelta(seconds=90)
    assert last_run_age(store, now=later) == pytest.approx(90)


def test_status_lists_needs_owner_issues(tmp_path):
    root = _write_config_root(tmp_path)
    fake = FakeGitHub([_issue(labels=("partner:pat", "liaise:needs-owner"))])
    store: dict = {}
    output = cli_status(root=str(root), gh=fake, store=store)
    assert "needs-owner: #1" in output
