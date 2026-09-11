"""Tests for liaise.dispatch: ClaudeHeadless, EchoDispatcher, budgets, reconciliation."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from liaise.config import DFLT_PERMISSION_MODE, Budget, DispatchConfig, PartnerConfig
from liaise.dispatch import (
    ClaudeHeadless,
    EchoDispatcher,
    Job,
    daily_dispatch_count,
    dispatch_issue,
    stored_session_id,
)
from liaise.github import FakeGitHub, Issue
from liaise.state import current_state, set_state
from liaise.tests.conftest import write_executable_script

REPO = "example/app"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: A `command`/`resume_command` template gets `shlex.split()` after
#: `.format()`. `shlex` treats "\" as a POSIX escape character, so a raw
#: Windows path (sys.executable, or a tmp_path-derived script path) embedded
#: directly corrupts under shlex — .as_posix() sidesteps it; Windows accepts
#: forward-slash paths from Python just fine.
_PY = Path(sys.executable).as_posix()


def _partner(tmp_path, **overrides) -> PartnerConfig:
    brief = tmp_path / "brief.md"
    brief.write_text("Keep it plain.\n")
    fields = dict(
        slug="pat",
        display_name="Pat",
        github_logins=("pat",),
        repo=REPO,
        brief=str(brief),
        label="partner:pat",
        notify_login="pat",
        dispatch=DispatchConfig(cwd=str(tmp_path)),
        budget=Budget(timeout_minutes=1, max_turns=10, daily_dispatches=2),
    )
    fields.update(overrides)
    return PartnerConfig(**fields)


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
        labels=("partner:pat",),
    )
    fields.update(overrides)
    return Issue(**fields)


# ---- EchoDispatcher ----


def test_echo_dispatcher_records_jobs_and_returns_a_session_id():
    dispatcher = EchoDispatcher()
    job = Job(prompt="hello", cwd=".", budget=Budget(), command="x", resume_command="y")
    result = dispatcher.dispatch(job)
    assert dispatcher.jobs == [job]
    assert result.returncode == 0
    assert result.session_id


# ---- ClaudeHeadless: captures and resumes a session id ----


@pytest.fixture
def fake_claude_bin(tmp_path: Path) -> Path:
    """A fake `claude` that echoes a session id and records whether it was a resume."""
    return write_executable_script(
        tmp_path / "claude",
        'import sys, json\n'
        'resumed = "--resume" if "--resume" in sys.argv else "fresh"\n'
        'print(json.dumps({"session_id": "sess-abc123", "mode": resumed}))\n',
    )


def test_claude_headless_captures_session_id(fake_claude_bin, tmp_path):
    claude = fake_claude_bin.as_posix()
    job = Job(
        prompt="do the thing",
        cwd=str(tmp_path),
        budget=Budget(timeout_minutes=1),
        command=f"{claude} -p {{prompt_file}} --output-format json",
        resume_command=f"{claude} --resume {{session_id}} -p {{prompt_file}} --output-format json",
    )
    result = ClaudeHeadless().dispatch(job)
    assert result.returncode == 0
    assert result.session_id == "sess-abc123"
    assert "fresh" in result.stdout


def test_claude_headless_uses_resume_command_when_session_id_given(fake_claude_bin, tmp_path):
    claude = fake_claude_bin.as_posix()
    job = Job(
        prompt="do the thing",
        cwd=str(tmp_path),
        budget=Budget(timeout_minutes=1),
        command=f"{claude} -p {{prompt_file}} --output-format json",
        resume_command=f"{claude} --resume {{session_id}} -p {{prompt_file}} --output-format json",
        session_id="sess-prior",
    )
    result = ClaudeHeadless().dispatch(job)
    assert result.session_id == "sess-abc123"  # captured the NEW session id
    assert '"mode": "--resume"' in result.stdout


def test_claude_headless_writes_prompt_to_a_tempfile_not_argv(tmp_path):
    # a script that dumps argv, so we can confirm the prompt text is nowhere in it
    script = tmp_path / "record_argv.py"
    script.write_text(
        "import sys, json\n"
        "print(json.dumps({'session_id': 's1', 'argv': sys.argv}))\n"
    )
    job = Job(
        prompt="SECRET-LOOKING-PROMPT-TEXT",
        cwd=str(tmp_path),
        budget=Budget(timeout_minutes=1),
        command=f"{_PY} {script.as_posix()} -p {{prompt_file}}",
        resume_command=f"{_PY} {script.as_posix()} --resume {{session_id}} -p {{prompt_file}}",
    )
    result = ClaudeHeadless().dispatch(job)
    assert "SECRET-LOOKING-PROMPT-TEXT" not in result.stdout


def test_claude_headless_rejects_a_malformed_session_id(tmp_path):
    """L-1 regression. `session_id` comes from the dispatched agent's own
    stdout and is later `.format()`-ed into `resume_command` before
    `shlex.split` — not a shell, but a value containing whitespace becomes
    extra argv elements on the next invocation. A session id shaped like
    that must not be captured at all.
    """
    script = tmp_path / "bad_session.py"
    script.write_text(
        "import json\n"
        "print(json.dumps({'session_id': 'not a valid id; --dangerous-flag'}))\n"
    )
    job = Job(
        prompt="hi",
        cwd=str(tmp_path),
        budget=Budget(timeout_minutes=1),
        command=f"{_PY} {script.as_posix()} -p {{prompt_file}}",
        resume_command=f"{_PY} {script.as_posix()} --resume {{session_id}} -p {{prompt_file}}",
    )
    result = ClaudeHeadless().dispatch(job)
    assert result.session_id is None


def test_claude_headless_timeout_returns_a_result_instead_of_raising(tmp_path):
    """H-7 regression. A hung dispatch used to raise `subprocess.TimeoutExpired`
    straight out of `ClaudeHeadless.dispatch` — uncaught, it propagated through
    `dispatch_issue` (leaving the issue at `liaise:working`, no reconciliation,
    no notification, the daily counter already incremented) and killed the
    rest of `run_once`'s pass. The `Dispatcher` protocol's own contract is
    "must not raise on a nonzero exit"; a timeout is the extreme case of that.
    """
    script = tmp_path / "hangs.py"
    script.write_text("import time\ntime.sleep(2)\n")
    job = Job(
        prompt="hi",
        cwd=str(tmp_path),
        budget=Budget(timeout_minutes=0),  # 0 seconds: expires immediately
        command=f"{_PY} {script.as_posix()} -p {{prompt_file}}",
        resume_command=f"{_PY} {script.as_posix()} --resume {{session_id}} -p {{prompt_file}}",
    )
    result = ClaudeHeadless().dispatch(job)  # must not raise
    assert result.returncode != 0
    assert "timed out" in result.stderr.lower()


# ---- budgets: daily cap sets liaise:budget ----


def test_daily_cap_sets_budget_label_and_does_not_dispatch(tmp_path):
    partner = _partner(tmp_path, budget=Budget(daily_dispatches=1))
    fake = FakeGitHub([_issue()])
    dispatcher = EchoDispatcher()
    store: dict = {}

    first = dispatch_issue(fake, dispatcher, store, partner, fake.get_issue(REPO, 1), now=T0)
    assert first.dispatched
    assert len(dispatcher.jobs) == 1

    second = dispatch_issue(fake, dispatcher, store, partner, fake.get_issue(REPO, 1), now=T0)
    assert not second.dispatched
    assert second.budget_capped
    assert len(dispatcher.jobs) == 1  # not dispatched again
    assert current_state(fake.get_issue(REPO, 1), partner) == "budget"


def test_daily_cap_posts_a_comment_and_notifies_the_owner_once(tmp_path):
    """M-8 regression: A.1 rule 5 says a tripped cap is "a visible label AND
    A SHORT COMMENT, never silence" — the label alone was already correct;
    nothing was ever posted or notified.
    """
    partner = _partner(tmp_path, budget=Budget(daily_dispatches=1), reply_mode="direct")
    fake = FakeGitHub([_issue(number=1), _issue(number=2)])
    dispatcher = EchoDispatcher()
    store: dict = {}
    notifications = []

    def fake_notify(*a, **k):
        notifications.append(a)
        return True

    dispatch_issue(fake, dispatcher, store, partner, fake.get_issue(REPO, 1), now=T0)  # uses the day's one slot
    dispatch_issue(
        fake, dispatcher, store, partner, fake.get_issue(REPO, 2), now=T0, notify_fn=fake_notify
    )  # capped

    comments = fake.get_issue(REPO, 2).comments
    assert len(comments) == 1
    assert "tomorrow" in comments[0].body.lower()
    assert len(notifications) == 1


def test_daily_cap_owner_notified_once_per_day_not_per_issue(tmp_path):
    partner = _partner(tmp_path, budget=Budget(daily_dispatches=0), reply_mode="direct")
    fake = FakeGitHub([_issue(number=1), _issue(number=2)])
    store: dict = {}
    notifications = []

    dispatch_issue(fake, EchoDispatcher(), store, partner, fake.get_issue(REPO, 1), now=T0,
                    notify_fn=lambda *a, **k: notifications.append(a) or True)
    dispatch_issue(fake, EchoDispatcher(), store, partner, fake.get_issue(REPO, 2), now=T0,
                    notify_fn=lambda *a, **k: notifications.append(a) or True)

    assert len(notifications) == 1  # not one per issue
    # but each issue still gets its own comment
    assert len(fake.get_issue(REPO, 1).comments) == 1
    assert len(fake.get_issue(REPO, 2).comments) == 1


def test_daily_cap_does_not_repost_every_pass_while_still_capped(tmp_path):
    partner = _partner(tmp_path, budget=Budget(daily_dispatches=0), reply_mode="direct")
    fake = FakeGitHub([_issue(number=1)])
    store: dict = {}

    dispatch_issue(fake, EchoDispatcher(), store, partner, fake.get_issue(REPO, 1), now=T0)
    dispatch_issue(fake, EchoDispatcher(), store, partner, fake.get_issue(REPO, 1), now=T0)
    dispatch_issue(fake, EchoDispatcher(), store, partner, fake.get_issue(REPO, 1), now=T0)

    assert len(fake.get_issue(REPO, 1).comments) == 1  # not reposted every call


def test_daily_cap_draft_mode_notifies_but_posts_nothing(tmp_path):
    partner = _partner(tmp_path, budget=Budget(daily_dispatches=0), reply_mode="draft")
    fake = FakeGitHub([_issue(number=1)])
    store: dict = {}
    notifications = []

    dispatch_issue(fake, EchoDispatcher(), store, partner, fake.get_issue(REPO, 1), now=T0,
                    notify_fn=lambda *a, **k: notifications.append(a) or True)

    assert fake.get_issue(REPO, 1).comments == ()
    assert len(notifications) == 1


def test_daily_count_resets_the_next_day(tmp_path):
    partner = _partner(tmp_path, budget=Budget(daily_dispatches=1))
    fake = FakeGitHub([_issue()])
    dispatcher = EchoDispatcher()
    store: dict = {}
    dispatch_issue(fake, dispatcher, store, partner, fake.get_issue(REPO, 1), now=T0)
    assert daily_dispatch_count(store, partner, now=T0) == 1

    next_day = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert daily_dispatch_count(store, partner, now=next_day) == 0


# ---- reconciliation: a crashed dispatch ends in needs-owner, with a notification ----


def test_crashed_dispatch_reconciles_to_needs_owner_and_notifies(tmp_path):
    partner = _partner(tmp_path)
    fake = FakeGitHub([_issue()])
    dispatcher = EchoDispatcher(returncode=1)  # simulates a crash: never sets an exit label
    store: dict = {}
    notifications = []

    def fake_notify(title, body, **kwargs):
        notifications.append((title, body, kwargs))
        return True

    outcome = dispatch_issue(
        fake, dispatcher, store, partner, fake.get_issue(REPO, 1),
        notify_fn=fake_notify, now=T0,
    )

    assert outcome.crashed
    assert current_state(fake.get_issue(REPO, 1), partner) == "needs-owner"
    assert len(notifications) == 1
    title, body, kwargs = notifications[0]
    assert "crashed" in title.lower()
    assert "1" in body  # exit code
    assert issue_url_in(body, fake, 1)


def issue_url_in(body: str, fake: FakeGitHub, number: int) -> bool:
    return fake.get_issue(REPO, number).url in body


def test_dispatch_that_sets_its_own_exit_label_is_not_reconciled(tmp_path):
    """If the agent itself sets needs-partner (say) before exiting, that is not a crash."""
    partner = _partner(tmp_path)
    fake = FakeGitHub([_issue()])
    store: dict = {}
    notifications = []

    class SelfTransitioningDispatcher:
        def dispatch(self, job: Job):
            from liaise.dispatch import DispatchResult

            issue = fake.get_issue(REPO, 1)
            set_state(fake, issue, partner, "needs-partner")
            return DispatchResult(returncode=0, session_id="sess-1")

    outcome = dispatch_issue(
        fake, SelfTransitioningDispatcher(), store, partner, fake.get_issue(REPO, 1),
        notify_fn=lambda *a, **k: notifications.append((a, k)), now=T0,
    )
    assert not outcome.crashed
    assert current_state(fake.get_issue(REPO, 1), partner) == "needs-partner"
    assert notifications == []


# ---- expect_working_on_success: the deploy_per == "batch" exception ----


def test_batch_deploy_success_left_working_is_not_a_crash_when_excused(tmp_path):
    partner = _partner(tmp_path, deploy_per="batch")
    fake = FakeGitHub([_issue()])
    dispatcher = EchoDispatcher(returncode=0)  # succeeds, never touches labels
    store: dict = {}
    notifications = []

    outcome = dispatch_issue(
        fake, dispatcher, store, partner, fake.get_issue(REPO, 1),
        notify_fn=lambda *a, **k: notifications.append((a, k)),
        expect_working_on_success=True, now=T0,
    )

    assert not outcome.crashed
    assert outcome.landed_awaiting_batch_deploy
    assert current_state(fake.get_issue(REPO, 1), partner) == "working"
    assert notifications == []


def test_still_working_success_without_the_flag_is_reconciled(tmp_path):
    """deploy_per == "issue": the agent owns its own exit label. Silently
    leaving `working` after a successful run is still a protocol violation
    worth flagging to the owner, not silently excused.
    """
    partner = _partner(tmp_path, deploy_per="issue")
    fake = FakeGitHub([_issue()])
    dispatcher = EchoDispatcher(returncode=0)
    store: dict = {}
    notifications = []

    outcome = dispatch_issue(
        fake, dispatcher, store, partner, fake.get_issue(REPO, 1),
        notify_fn=lambda *a, **k: notifications.append((a, k)), now=T0,
    )  # expect_working_on_success defaults to False

    assert outcome.crashed
    assert not outcome.landed_awaiting_batch_deploy
    assert current_state(fake.get_issue(REPO, 1), partner) == "needs-owner"
    assert len(notifications) == 1


def test_nonzero_exit_is_always_a_crash_even_with_the_flag_set(tmp_path):
    partner = _partner(tmp_path, deploy_per="batch")
    fake = FakeGitHub([_issue()])
    dispatcher = EchoDispatcher(returncode=1)
    store: dict = {}

    outcome = dispatch_issue(
        fake, dispatcher, store, partner, fake.get_issue(REPO, 1),
        expect_working_on_success=True, now=T0,
    )

    assert outcome.crashed
    assert not outcome.landed_awaiting_batch_deploy
    assert current_state(fake.get_issue(REPO, 1), partner) == "needs-owner"


# ---- reconciliation: a partner-facing comment missing the mention (#20) ----


def test_agent_comment_missing_the_mention_is_repaired(tmp_path):
    partner = _partner(tmp_path, notify_login="pat")
    fake = FakeGitHub([_issue()])
    log_dir = tmp_path / "logs"

    class ForgetfulDispatcher:
        def dispatch(self, job: Job):
            from liaise.dispatch import DispatchResult

            issue = fake.get_issue(REPO, 1)
            fake.post_comment(REPO, 1, "Quick question: what color?")  # no mention
            set_state(fake, issue, partner, "needs-partner")
            return DispatchResult(returncode=0, session_id="sess-1")

    dispatch_issue(
        fake, ForgetfulDispatcher(), {}, partner, fake.get_issue(REPO, 1),
        now=T0, log_dir=log_dir,
    )

    comment = fake.get_issue(REPO, 1).comments[-1]
    assert comment.body == "@pat Quick question: what color?"

    [log_file] = list(log_dir.iterdir())
    assert "repaired" in log_file.read_text().lower()
    assert "@pat" in log_file.read_text()


def test_agent_comment_already_mentioning_the_partner_is_left_byte_identical(tmp_path):
    partner = _partner(tmp_path, notify_login="pat")
    fake = FakeGitHub([_issue()])

    class DiligentDispatcher:
        def dispatch(self, job: Job):
            from liaise.dispatch import DispatchResult

            issue = fake.get_issue(REPO, 1)
            fake.post_comment(REPO, 1, "@pat Quick question: what color?")
            set_state(fake, issue, partner, "needs-partner")
            return DispatchResult(returncode=0, session_id="sess-1")

    dispatch_issue(fake, DiligentDispatcher(), {}, partner, fake.get_issue(REPO, 1), now=T0)

    comment = fake.get_issue(REPO, 1).comments[-1]
    assert comment.body == "@pat Quick question: what color?"
    assert comment.updated_at == comment.created_at  # untouched


def test_reconciliation_skipped_when_dispatch_ends_at_needs_owner(tmp_path):
    """Only `needs-partner` and `deployed` are partner-facing exit states — a
    `needs-owner` escalation posts nothing to the partner at all (the
    operating rules), so there is nothing to repair.
    """
    partner = _partner(tmp_path, notify_login="pat")
    fake = FakeGitHub([_issue()])

    class EscalatingDispatcher:
        def dispatch(self, job: Job):
            from liaise.dispatch import DispatchResult

            issue = fake.get_issue(REPO, 1)
            set_state(fake, issue, partner, "needs-owner")
            return DispatchResult(returncode=0, session_id="sess-1")

    dispatch_issue(fake, EscalatingDispatcher(), {}, partner, fake.get_issue(REPO, 1), now=T0)
    assert fake.get_issue(REPO, 1).comments == ()


def test_reconciliation_no_op_when_the_agent_posted_nothing(tmp_path):
    partner = _partner(tmp_path, notify_login="pat")
    fake = FakeGitHub([_issue()])

    outcome = dispatch_issue(
        fake, SelfTransitioningDispatcherFor(fake, partner), {}, partner,
        fake.get_issue(REPO, 1), now=T0,
    )
    assert not outcome.crashed
    assert fake.get_issue(REPO, 1).comments == ()


class SelfTransitioningDispatcherFor:
    """Sets `needs-partner` without posting a comment — reconciliation must
    not invent a mention out of nothing.
    """

    def __init__(self, fake, partner):
        self._fake = fake
        self._partner = partner

    def dispatch(self, job: Job):
        from liaise.dispatch import DispatchResult

        issue = self._fake.get_issue(REPO, 1)
        set_state(self._fake, issue, self._partner, "needs-partner")
        return DispatchResult(returncode=0, session_id="sess-1")


def test_reconciliation_leaves_a_stale_unrelated_comment_untouched(tmp_path):
    """A comment left by this identity on a PRIOR, unrelated dispatch (e.g. an
    old owner-facing escalation note) must not be mistaken for something this
    dispatch just posted — reconciliation gates on the comment count actually
    having grown during this dispatch, not merely on "my last comment exists".
    """
    partner = _partner(tmp_path, notify_login="pat")
    fake = FakeGitHub([_issue()])
    fake.post_comment(REPO, 1, "Escalating: needs owner approval for $500")

    outcome = dispatch_issue(
        fake, SelfTransitioningDispatcherFor(fake, partner), {}, partner,
        fake.get_issue(REPO, 1), now=T0,
    )

    assert not outcome.crashed
    comments = fake.get_issue(REPO, 1).comments
    assert len(comments) == 1
    assert comments[0].body == "Escalating: needs owner approval for $500"  # untouched


def test_reconciliation_failure_does_not_discard_the_dispatch_outcome(tmp_path):
    """A transient `gh` error (rate limit, network blip) during reconciliation
    must not propagate out of `dispatch_issue` — that would discard an
    otherwise-successful outcome (e.g. `landed_awaiting_batch_deploy`) via the
    broad `except Exception` in `run.py`'s per-issue loop.
    """
    from liaise.github import GitHubError

    partner = _partner(tmp_path, notify_login="pat")

    class FlakyGitHub(FakeGitHub):
        def ensure_last_comment_mentions(self, repo, number, mention):
            raise GitHubError("rate limited")

    fake = FlakyGitHub([_issue()])
    log_dir = tmp_path / "logs"

    class ForgetfulDispatcher:
        def dispatch(self, job: Job):
            from liaise.dispatch import DispatchResult

            issue = fake.get_issue(REPO, 1)
            fake.post_comment(REPO, 1, "Quick question: what color?")
            set_state(fake, issue, partner, "needs-partner")
            return DispatchResult(returncode=0, session_id="sess-1")

    outcome = dispatch_issue(  # must not raise
        fake, ForgetfulDispatcher(), {}, partner, fake.get_issue(REPO, 1),
        now=T0, log_dir=log_dir,
    )

    assert not outcome.crashed
    assert outcome.dispatched
    [log_file] = list(log_dir.iterdir())
    assert "reconciliation failed" in log_file.read_text().lower()


# ---- session id memory (resume) ----


def test_session_id_stored_and_reused_for_resume(tmp_path):
    partner = _partner(tmp_path)
    fake = FakeGitHub([_issue()])
    dispatcher = EchoDispatcher()
    store: dict = {}

    dispatch_issue(fake, dispatcher, store, partner, fake.get_issue(REPO, 1), now=T0)
    assert stored_session_id(store, _issue())  # captured from the first dispatch
    first_job = dispatcher.jobs[0]
    assert first_job.session_id is None  # first dispatch: fresh

    dispatch_issue(fake, dispatcher, store, partner, fake.get_issue(REPO, 1), now=T0)
    second_job = dispatcher.jobs[1]
    assert second_job.session_id == stored_session_id(store, _issue())


# ---- #22: a resume runs under the same permission mode as a fresh run ----


@pytest.fixture
def argv_recording_claude(tmp_path: Path) -> str:
    """A fake `claude` that reports the argv it was given."""
    return write_executable_script(
        tmp_path / "claude",
        "import sys, json\n"
        "print(json.dumps({'session_id': 'sess-abc123', 'argv': sys.argv[1:]}))\n",
    ).as_posix()


@pytest.mark.parametrize("permission_mode", [DFLT_PERMISSION_MODE, "plan"])
def test_resumed_dispatch_carries_the_same_permission_mode_as_a_fresh_one(
    argv_recording_claude, tmp_path, permission_mode
):
    """#22 regression: the default resume template dropped `--permission-mode`,
    so a resumed dispatch ran under a different mode than the run it
    continued. Both default templates now take it from the one
    `dispatch.permission_mode` setting — change it, and both follow.
    """
    dispatch = DispatchConfig(permission_mode=permission_mode)

    def argv_of_run(session_id=None) -> list[str]:
        job = Job(
            prompt="hi",
            cwd=str(tmp_path),
            budget=Budget(timeout_minutes=1),
            # the default templates, with only the program swapped for the fake
            command=dispatch.command.replace("claude", argv_recording_claude, 1),
            resume_command=dispatch.resume_command.replace(
                "claude", argv_recording_claude, 1
            ),
            permission_mode=dispatch.permission_mode,
            session_id=session_id,
        )
        return json.loads(ClaudeHeadless().dispatch(job).stdout)["argv"]

    def mode_of(argv: list[str]) -> str:
        return argv[argv.index("--permission-mode") + 1]

    fresh, resumed = argv_of_run(), argv_of_run(session_id="sess-prior")
    assert "--resume" not in fresh and "--resume" in resumed
    assert mode_of(fresh) == mode_of(resumed) == permission_mode


def test_dispatch_issue_hands_the_partner_permission_mode_to_every_job(tmp_path):
    partner = _partner(
        tmp_path, dispatch=DispatchConfig(cwd=str(tmp_path), permission_mode="plan")
    )
    fake = FakeGitHub([_issue()])
    dispatcher = EchoDispatcher()
    store: dict = {}

    dispatch_issue(fake, dispatcher, store, partner, fake.get_issue(REPO, 1), now=T0)
    dispatch_issue(fake, dispatcher, store, partner, fake.get_issue(REPO, 1), now=T0)

    fresh, resumed = dispatcher.jobs
    assert resumed.session_id is not None
    assert fresh.permission_mode == resumed.permission_mode == "plan"


# ---- #22: the dispatch log exists before the agent runs, and keeps what it wrote ----


def test_dispatch_log_is_named_in_the_prompt_and_keeps_what_the_agent_wrote(tmp_path):
    """#22: the operating rules send drafts and escalations "to the dispatch
    log", so it must exist before the agent runs, its path must be in the
    prompt, and `liaise`'s own record must be appended after the agent's
    drafts rather than written over them.
    """
    partner = _partner(tmp_path)
    fake = FakeGitHub([_issue()])
    log_dir = tmp_path / "logs"

    class DraftingDispatcher:
        def dispatch(self, job: Job):
            from liaise.dispatch import DispatchResult

            [log_file] = list(log_dir.iterdir())
            assert str(log_file) in job.prompt
            with open(log_file, "a") as f:
                f.write("DRAFT for the partner: it's fixed, try again\n")
            set_state(fake, fake.get_issue(REPO, 1), partner, "needs-owner")
            return DispatchResult(returncode=0, session_id="sess-1")

    outcome = dispatch_issue(
        fake, DraftingDispatcher(), {}, partner, fake.get_issue(REPO, 1),
        now=T0, log_dir=log_dir,
    )

    text = Path(outcome.log_path).read_text()
    assert text.index("DRAFT for the partner") < text.index("exit code: 0")


def test_a_log_the_agent_removed_does_not_skip_crash_reconciliation(tmp_path):
    """Appending the result to a log that is gone must not raise past the
    reconciliation — that would strand the issue at `liaise:working`.
    """
    import shutil

    partner = _partner(tmp_path)
    fake = FakeGitHub([_issue()])
    log_dir = tmp_path / "logs"
    notifications = []

    class LogRemovingDispatcher:
        def dispatch(self, job: Job):
            from liaise.dispatch import DispatchResult

            shutil.rmtree(log_dir)
            return DispatchResult(returncode=1, session_id=None)

    outcome = dispatch_issue(  # must not raise
        fake, LogRemovingDispatcher(), {}, partner, fake.get_issue(REPO, 1),
        notify_fn=lambda *a, **k: notifications.append(a) or True,
        now=T0, log_dir=log_dir,
    )

    assert outcome.crashed
    assert current_state(fake.get_issue(REPO, 1), partner) == "needs-owner"
    assert len(notifications) == 1
