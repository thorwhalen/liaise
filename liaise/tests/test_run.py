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
from liaise.tests.conftest import write_executable_script

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


def test_cli_run_uses_the_configured_ntfy_topic_env(tmp_path, monkeypatch):
    """M-4 regression: `notify.ntfy_topic_env` was resolved from config and
    never read anywhere — `dispatch_issue`'s notify_fn call carried no
    topic_env, so `notify()` always checked its own hardcoded default.
    Setting a *custom* variable name must still reach a real notification.
    """
    root = tmp_path / "config"
    (root / "partners").mkdir(parents=True)
    (root / "briefs").mkdir()
    (root / "briefs" / "pat.md").write_text("hi\n")
    (root / "config.toml").write_text(
        f'owner_login = "owner"\nstate_dir = "{tmp_path / "state"}"\n'
        f'[notify]\nntfy_topic_env = "MY_CUSTOM_NTFY_VAR"\n'
    )
    (root / "partners" / "pat.toml").write_text(
        f'display_name = "Pat"\n'
        f'github_logins = ["pat"]\n'
        f'repo = "{REPO}"\n'
        f'brief = "{root / "briefs" / "pat.md"}"\n'
        f'budget = {{daily_dispatches = 0}}\n'  # forces an immediate budget-cap notification
    )
    monkeypatch.delenv("LIAISE_NTFY_TOPIC", raising=False)
    monkeypatch.setenv("MY_CUSTOM_NTFY_VAR", "some-topic")

    from unittest.mock import patch

    fake = FakeGitHub([_issue(author="pat", labels=("partner:pat", "liaise:intake"))])
    with patch("liaise.notify.urllib.request.urlopen") as urlopen:
        cli_run(root=str(root), once=True, gh=fake, dispatcher=EchoDispatcher(), store={})
    urlopen.assert_called_once()  # reached the real notify() under the custom var name


# ---- dispatch + batch deploy: runs once, posts + deploys per issue ----


def test_run_once_dispatches_ready_issue_and_batch_deploys(tmp_path):
    deploy_script = write_executable_script(tmp_path / "deploy", "pass\n")
    # reply_mode="direct": the package default is "draft", which (H-5) posts
    # nothing to the thread — this test is about the direct-mode behavior.
    config = _config(
        tmp_path, deploy_per="batch", deploy=deploy_script.as_posix(), reply_mode="direct"
    )
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
    from subprocess import CompletedProcess

    def fake_run(*a, **k):
        calls.append(a)
        return CompletedProcess(a, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(run_module.subprocess, "run", fake_run)

    store: dict = {}
    now = T0 + timedelta(minutes=20)
    report = run_once(fake, LandingDispatcher(), store, config, now=now)

    assert len(calls) == 1  # deploy command run exactly once for the whole batch
    assert sorted(report.deployed["pat"]) == [1, 2]


# ---- H-3: a needs-partner issue is not re-dispatched with no new activity ----


def test_needs_partner_issue_not_redispatched_without_new_partner_activity(tmp_path):
    """Reproduces H-3's exact scenario: a dispatch already happened (recorded
    via dispatch_issue, which stamps last_dispatch_at), the issue is still
    needs-partner, and the partner has said nothing since. Must not
    dispatch again.
    """
    from liaise.dispatch import dispatch_issue
    from liaise.state import set_state

    config = _config(tmp_path)
    partner = config.partner("pat")
    fake = FakeGitHub([_issue(created_at=T0)])
    store: dict = {}

    # simulate: the first dispatch already happened, agent asked a question
    dispatch_issue(fake, EchoDispatcher(), store, partner, fake.get_issue(REPO, 1), now=T0)
    set_state(fake, fake.get_issue(REPO, 1), partner, "needs-partner")

    dispatcher = EchoDispatcher()
    later = T0 + timedelta(hours=1)  # well past quiet_minutes, no partner reply
    report = run_once(fake, dispatcher, store, config, now=later)

    assert dispatcher.jobs == []  # not re-dispatched
    assert any(item.action == "skip (awaiting partner reply)" for item in report.plan)
    assert current_state(fake.get_issue(REPO, 1), partner) == "needs-partner"


def test_needs_partner_issue_redispatched_after_new_partner_comment(tmp_path):
    from liaise.dispatch import dispatch_issue
    from liaise.github import Comment
    from liaise.state import set_state

    config = _config(tmp_path)
    partner = config.partner("pat")
    fake = FakeGitHub([_issue(created_at=T0)])
    store: dict = {}

    dispatch_issue(fake, EchoDispatcher(), store, partner, fake.get_issue(REPO, 1), now=T0)
    set_state(fake, fake.get_issue(REPO, 1), partner, "needs-partner")

    # the partner replies AFTER the dispatch
    reply_time = T0 + timedelta(minutes=30)
    issue = fake.get_issue(REPO, 1)
    fake.seed(
        issue.__class__(
            **{**issue.__dict__, "comments": (Comment(author="pat", body="ok", created_at=reply_time, updated_at=reply_time),)}
        )
    )

    dispatcher = EchoDispatcher()
    later = reply_time + timedelta(minutes=partner.quiet_minutes + 1)
    run_once(fake, dispatcher, store, config, now=later)

    assert len(dispatcher.jobs) == 1  # re-dispatched: the partner did reply


# ---- H-4: liaise:budget resumes the next day ----


def test_budget_capped_issue_is_reconsidered_and_dispatches_the_next_day(tmp_path):
    config = _config(tmp_path, budget=Budget(timeout_minutes=1, daily_dispatches=1))
    partner = config.partner("pat")
    fake = FakeGitHub([_issue(created_at=T0, labels=(partner.label, "liaise:budget"))])
    dispatcher = EchoDispatcher()
    store: dict = {}

    next_day = T0 + timedelta(days=1, hours=1)
    run_once(fake, dispatcher, store, config, now=next_day)

    assert len(dispatcher.jobs) == 1
    assert current_state(fake.get_issue(REPO, 1), partner) != "budget"


def test_budget_capped_issue_stays_capped_same_day(tmp_path):
    config = _config(tmp_path, budget=Budget(timeout_minutes=1, daily_dispatches=1))
    partner = config.partner("pat")
    fake = FakeGitHub([_issue(created_at=T0, labels=(partner.label, "liaise:budget"))])
    dispatcher = EchoDispatcher()
    store: dict = {f"daily__pat__{T0.date().isoformat()}": 1}  # already capped today

    later_same_day = T0 + timedelta(hours=2)
    run_once(fake, dispatcher, store, config, now=later_same_day)

    assert dispatcher.jobs == []
    assert current_state(fake.get_issue(REPO, 1), partner) == "budget"


# ---- H-5: draft mode never posts, even from a batch deploy ----


def test_batch_deploy_in_draft_mode_posts_nothing(tmp_path):
    deploy_script = write_executable_script(tmp_path / "deploy", "pass\n")
    config = _config(
        tmp_path, deploy_per="batch", deploy=deploy_script.as_posix(), reply_mode="draft"
    )
    partner = config.partner("pat")
    fake = FakeGitHub([_issue(labels=(partner.label, "liaise:intake"))])

    class LandingDispatcher:
        def dispatch(self, job):
            from liaise.dispatch import DispatchResult

            return DispatchResult(returncode=0, session_id="sess-1")

    notifications = []
    store: dict = {}
    now = T0 + timedelta(minutes=20)
    run_once(
        fake, LandingDispatcher(), store, config, now=now,
        notify_fn=lambda *a, **k: notifications.append(a) or True,
    )

    issue = fake.get_issue(REPO, 1)
    assert issue.comments == ()  # nothing posted to the partner
    assert current_state(issue, partner) == "deployed"  # still lands and deploys
    assert len(notifications) == 1  # the owner is told instead


# ---- M-1 / M-2: a failed or unconfigured deploy does not claim success ----


def test_failed_deploy_does_not_tell_the_partner_its_live(tmp_path):
    deploy_script = write_executable_script(tmp_path / "deploy", "import sys\nsys.exit(1)\n")
    config = _config(
        tmp_path, deploy_per="batch", deploy=deploy_script.as_posix(), reply_mode="direct"
    )
    partner = config.partner("pat")
    fake = FakeGitHub([_issue(labels=(partner.label, "liaise:intake"))])

    class LandingDispatcher:
        def dispatch(self, job):
            from liaise.dispatch import DispatchResult

            return DispatchResult(returncode=0, session_id="sess-1")

    notifications = []
    store: dict = {}
    now = T0 + timedelta(minutes=20)
    run_once(
        fake, LandingDispatcher(), store, config, now=now,
        notify_fn=lambda *a, **k: notifications.append(a) or True,
    )

    issue = fake.get_issue(REPO, 1)
    assert issue.comments == ()  # never told the partner it's live
    assert current_state(issue, partner) == "needs-owner"
    assert len(notifications) == 1


def test_batch_with_no_deploy_command_reconciles_instead_of_stranding(tmp_path):
    """M-2: deploy_per="batch" with no deploy command configured is the
    out-of-the-box default combination. Must not leave the issue at
    liaise:working forever.
    """
    config = _config(tmp_path, deploy_per="batch", deploy="", reply_mode="direct")
    partner = config.partner("pat")
    fake = FakeGitHub([_issue(labels=(partner.label, "liaise:intake"))])

    class LandingDispatcher:
        def dispatch(self, job):
            from liaise.dispatch import DispatchResult

            return DispatchResult(returncode=0, session_id="sess-1")

    store: dict = {}
    now = T0 + timedelta(minutes=20)
    run_once(fake, LandingDispatcher(), store, config, now=now,
             notify_fn=lambda *a, **k: True)

    issue = fake.get_issue(REPO, 1)
    assert current_state(issue, partner) == "needs-owner"
    assert issue.comments == ()

    # a week later: still needs-owner, not stuck forever at "working"
    week_later = now + timedelta(days=7)
    run_once(fake, EchoDispatcher(), store, config, now=week_later)
    assert current_state(fake.get_issue(REPO, 1), partner) == "needs-owner"


# ---- M-10: a stale `deployed` issue gets one nudge, once ----


def test_stale_deployed_issue_gets_one_nudge(tmp_path):
    config = _config(tmp_path, deployed_nudge_days=3, reply_mode="direct")
    partner = config.partner("pat")
    fake = FakeGitHub([_issue(labels=(partner.label, "liaise:deployed"), created_at=T0)])
    store: dict = {}

    stale = T0 + timedelta(days=4)
    run_once(fake, EchoDispatcher(), store, config, now=stale)
    assert len(fake.get_issue(REPO, 1).comments) == 1

    # a second pass the same week must not nudge again
    later_stale = stale + timedelta(hours=1)
    run_once(fake, EchoDispatcher(), store, config, now=later_stale)
    assert len(fake.get_issue(REPO, 1).comments) == 1


def test_deployed_issue_not_yet_stale_is_not_nudged(tmp_path):
    config = _config(tmp_path, deployed_nudge_days=3, reply_mode="direct")
    partner = config.partner("pat")
    fake = FakeGitHub([_issue(labels=(partner.label, "liaise:deployed"), created_at=T0)])
    store: dict = {}

    not_yet_stale = T0 + timedelta(days=2)
    run_once(fake, EchoDispatcher(), store, config, now=not_yet_stale)
    assert fake.get_issue(REPO, 1).comments == ()


# ---- L-3: concurrency of one ----


def test_concurrent_run_once_refuses_to_start(tmp_path):
    from liaise.run import _run_lock

    lock_path = tmp_path / "state" / "run.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(str(__import__("os").getpid()))  # our own pid: "alive"

    config = _config(tmp_path)
    fake = FakeGitHub([])
    with pytest.raises(RuntimeError, match="already in progress"):
        run_once(fake, EchoDispatcher(), {}, config, now=T0)


def test_stale_lock_from_a_dead_pid_is_reclaimed(tmp_path):
    lock_path = tmp_path / "state" / "run.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("999999999")  # not a real pid

    config = _config(tmp_path)
    fake = FakeGitHub([])
    run_once(fake, EchoDispatcher(), {}, config, now=T0)  # must not raise
    assert not lock_path.exists()  # released after the run


def test_dry_run_does_not_take_the_lock(tmp_path):
    config = _config(tmp_path)
    fake = FakeGitHub([_issue(labels=())])
    run_once(fake, EchoDispatcher(), {}, config, dry_run=True, now=T0)
    assert not (tmp_path / "state" / "run.lock").exists()


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
