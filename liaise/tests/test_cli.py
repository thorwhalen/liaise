"""Tests for liaise.cli: the 0.1 command tree, and each command over a fictional config root.

Every root is under tmp_path and every processor an EchoProcessor, so no test runs claude or
gh, sends anything, or reads the real config root. `liaise run --once --dry-run` end to end
is test_smoke.py.
"""

from __future__ import annotations

import copy
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import cw
import pytest

from liaise import cli
from liaise.github import FakeGitHub
from liaise.ledger import Ledger
from liaise.model import CASE_STATES, LedgerEntry, RunRecord
from liaise.outcomes import make_draft
from liaise.processor import EchoProcessor
from liaise.testing import FakeGitHubChannel, demo_registry
from liaise.tick import TickReport, run_lock, run_lock_path

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
SLUG = "example-app"
REPO = "example/app"
BINDING = "github:example/app?labels=partner:pat"

SUBJECT_TOML = """
bindings = ["github:example/app?labels=partner:pat"]
workspace = {{ path = "{workspace}" }}

[policy]
default_reply_mode = "direct"
people = {{ "github:pat" = "pat" }}
roles = {{ pat = "partner" }}
relays = ["github:example-bot"]
claim_labels = {{ "partner:pat" = "pat" }}
"""
#: A second subject, on a channel no registry knows, so its binding can never match.
UNMATCHABLE_TOML = """
bindings = ["nowhere:example-thing"]

[policy]
people = { "nowhere:pat" = "pat" }
roles = { pat = "partner" }
"""


@pytest.fixture(autouse=True)
def no_real_acquaint(monkeypatch):
    monkeypatch.setitem(sys.modules, "acquaint", None)


@pytest.fixture
def root(tmp_path) -> Path:
    root = tmp_path / "config"
    (root / "subjects").mkdir(parents=True)
    workspace = tmp_path / "code" / SLUG
    workspace.mkdir(parents=True)
    (tmp_path / "sessions").mkdir()
    (root / "config.toml").write_text(
        f'owner_login = "owner"\nstate_dir = "{(tmp_path / "state").as_posix()}"\n'
    )
    (root / "subjects" / f"{SLUG}.toml").write_text(SUBJECT_TOML.format(workspace=workspace.as_posix()))
    return root


def _fakes(root: Path, **overrides) -> dict:
    """The seams of `cli.run`, each a fake."""
    fakes = dict(
        registry=demo_registry(),
        processor=EchoProcessor(),
        labeler=FakeGitHub(),
        notify_fn=lambda *args, **kwargs: None,
        sessions_dir=str(root.parent / "sessions"),
        now=NOW,
    )
    fakes.update(overrides)
    return fakes


# ---- the command tree ----


def test_the_command_tree_is_the_0_1_one():
    commands = cli._dispatch_funcs
    assert set(commands) == {
        "run", "status", "hold", "unhold", "case", "subject", "setup", "migrate-config", "schedule"
    }
    assert set(commands["case"]) == {"list", "show", "set-state"}
    assert set(commands["subject"]) == {"list", "show"}
    assert set(commands["schedule"]) == {"install", "uninstall", "status"}


def test_run_takes_its_flags_and_hides_its_seams():
    parser = cw.mk_parser(cli._dispatch_funcs, config=cli._dispatch_config, prog="liaise")
    parsed = parser.parse_args(["run", "--once", "--dry-run", "--subject", SLUG, "--root", "somewhere"])
    assert (parsed.once, parsed.dry_run, parsed.subject, parsed.root) == (True, True, SLUG, "somewhere")
    seams = ("--registry", "--processor", "--labeler", "--store", "--notify-fn", "--sessions-dir", "--now")
    for seam in (*seams, "--resolver", "--workspace", "--triage"):
        with pytest.raises(SystemExit):
            parser.parse_args(["run", seam, "x"])


def test_the_case_commands_take_their_flags_and_hide_their_seams():
    parser = cw.mk_parser(cli._dispatch_funcs, config=cli._dispatch_config, prog="liaise")
    listed = parser.parse_args(["case", "list", "--state", "needs-owner"])
    assert listed.state == "needs-owner"
    moved = parser.parse_args(["case", "set-state", f"{SLUG}-1", "intake", "--reason", "fixed by hand", "--dry-run"])
    fields = (getattr(moved, "case-id"), moved.state, moved.reason, moved.dry_run)  # cw names a positional so
    assert fields == (f"{SLUG}-1", "intake", "fixed by hand", True)
    shown = parser.parse_args(["case", "show", f"{SLUG}-1"])
    assert getattr(shown, "case-id") == f"{SLUG}-1"
    positionals = {"list": [], "show": ["x"], "set-state": ["x", "intake"]}
    for command, seam in (("list", "--store"), ("show", "--store"), ("set-state", "--store"), ("set-state", "--now")):
        with pytest.raises(SystemExit):
            parser.parse_args(["case", command, *positionals[command], seam, "x"])


# ---- run ----


def test_a_dry_run_on_the_default_ledger_creates_nothing(root, tmp_path):
    output = cli.run(root=str(root), once=True, dry_run=True, **_fakes(root))
    assert output.splitlines()[0] == f"tick at {NOW.isoformat()} [dry run]"
    assert not (tmp_path / "state").exists()


def test_an_unknown_subject_is_one_line_naming_the_known_ones(root):
    with pytest.raises(cw.CommandError, match=f"no subject 'elsewhere'.*{SLUG}"):
        cli.run(root=str(root), once=True, dry_run=True, subject="elsewhere", **_fakes(root))
    with pytest.raises(cw.CommandError, match=f"no subject 'elsewhere'.*{SLUG}"):
        cli.subject_show("elsewhere", root=str(root))


def test_run_without_once_ticks_until_interrupted(root, monkeypatch, capsys):
    def interrupt(seconds):
        assert seconds == cli.DFLT_LOOP_SECONDS
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", interrupt)
    assert cli.run(root=str(root), store={}, **_fakes(root)) == cli.STOPPED
    assert capsys.readouterr().out.startswith(f"tick at {NOW.isoformat()}")


def test_run_notifies_through_the_configured_ntfy_topic_variable(root, monkeypatch):
    """0.0.x M-4: `notify.ntfy_topic_env` in config.toml reaches the notifier the tick uses
    by default, so a custom variable name still notifies."""
    config = root / "config.toml"
    config.write_text(config.read_text() + '\n[notify]\nntfy_topic_env = "EXAMPLE_NTFY_TOPIC"\n')
    monkeypatch.delenv("LIAISE_NTFY_TOPIC", raising=False)
    monkeypatch.setenv("EXAMPLE_NTFY_TOPIC", "example-topic")

    class StartFails(EchoProcessor):
        def start(self, job):
            raise RuntimeError("the spawn failed")

    github = FakeGitHubChannel(clock=lambda: NOW)
    github.add_issue(
        REPO, 1, author="pat", title="Search", body="Search skips accents.", labels=["partner:pat"],
        created_at=NOW - timedelta(hours=1),
    )
    fakes = _fakes(root, registry=demo_registry(github=github), processor=StartFails())
    del fakes["notify_fn"]
    with patch("liaise.notify.urllib.request.urlopen") as urlopen:
        cli.run(root=str(root), once=True, store={}, **fakes)
    urlopen.assert_called_once()


# ---- status, hold, unhold ----


def test_status_on_a_fresh_install_says_never_and_creates_nothing(root, tmp_path):
    lines = cli.status(root=str(root), now=NOW).splitlines()
    assert lines[0] == "last_run: never"
    assert "holds: 0" in lines
    assert f"subject {SLUG}: 0 case(s), 0/6 dispatches today" in lines
    assert not (tmp_path / "state").exists()


def test_hold_and_unhold_a_scope_in_the_default_ledger(root, tmp_path):
    held = cli.hold(f"subject:{SLUG}", mode="drain", reason="partner away", root=str(root))
    assert held == f"held subject:{SLUG} (drain): partner away"
    assert (tmp_path / "state" / "ledger").is_dir()

    lines = cli.status(root=str(root), now=NOW).splitlines()
    assert "holds: 1" in lines
    assert any(line.startswith(f"  subject:{SLUG}: drain, set by operator") for line in lines)

    assert cli.unhold(f"subject:{SLUG}", root=str(root)) == f"lifted the hold on subject:{SLUG}"
    assert cli.unhold(f"subject:{SLUG}", root=str(root)) == f"no hold on subject:{SLUG}"


def test_hold_writes_to_a_given_store(root):
    store: dict = {}
    cli.hold("processor", root=str(root), store=store)
    (placed,) = Ledger(store).holds()
    assert (placed.scope, placed.mode, placed.set_by) == ("processor", "block", "operator")


@pytest.mark.parametrize("scope, mode", [("somewhere", "block"), (f"subject:{SLUG}", "freeze")])
def test_a_bad_scope_or_mode_is_one_line_and_writes_nothing(root, tmp_path, scope, mode):
    with pytest.raises(cw.CommandError, match="not one of"):
        cli.hold(scope, mode=mode, root=str(root))
    assert not (tmp_path / "state").exists()


# ---- subjects ----


def test_subject_list_flags_a_binding_that_can_never_match(root):
    (root / "subjects" / "example-thing.toml").write_text(UNMATCHABLE_TOML)
    lines = cli.subject_list(root=str(root)).splitlines()
    assert lines[0] == f"{SLUG}\t{BINDING}"
    assert lines[1].startswith("example-thing\tnowhere:example-thing  [1 binding problem(s)")


def test_subject_show_prints_the_resolved_subject_with_its_defaults(root):
    lines = cli.subject_show(SLUG, root=str(root)).splitlines()
    assert lines[0] == f"subject: {SLUG}"
    assert f"  bindings: {BINDING}" in lines
    assert "  policy.people.github:pat: pat" in lines
    assert "  policy.roles.pat: partner" in lines
    assert "  policy.default_reply_mode: direct" in lines
    assert "  policy.budget.daily_dispatches: 6" in lines  # a default, applied
    assert "  policy.reply_modes: (none)" in lines
    assert "  delivery.kind: deploy" in lines
    assert lines[-1] == "binding problems: none"


def test_subject_show_lists_each_binding_problem(root):
    (root / "subjects" / "example-thing.toml").write_text(UNMATCHABLE_TOML)
    lines = cli.subject_show("example-thing", root=str(root)).splitlines()
    problems = lines.index("binding problems: 1")
    assert "nowhere:example-thing" in lines[problems + 1]


# ---- setup and migrate-config ----


def test_setup_creates_the_subjects_labels(root):
    """Ported from 0.0.x test_state's `liaise setup` test."""
    labeler = FakeGitHub()
    output = cli.setup(SLUG, root=str(root), labeler=labeler)
    assert set(labeler.labels_created(REPO)) == {"partner:pat", *(f"liaise:{state}" for state in CASE_STATES)}
    assert output.startswith(f"{REPO}: created 1 claim label(s) and {len(CASE_STATES)} state labels")


def test_migrate_config_prints_the_plan_and_writes_nothing_without_apply(config_root):
    lines = cli.migrate_config(root=str(config_root)).splitlines()
    assert lines[0].startswith("subject ")
    assert lines[-1] == "nothing written (dry run)"
    assert not (config_root / "subjects").exists()


def test_migrate_config_reports_a_config_that_does_not_load_as_one_line(tmp_path):
    with pytest.raises(cw.CommandError, match="Missing global config"):
        cli.migrate_config(root=str(tmp_path / "nowhere"))


# ---- cases (S7 #12) ----


def _seed_cases(store: dict) -> None:
    """``example-app-1`` in needs-owner and, opened after it, ``example-app-2`` deployed."""
    ledger = Ledger(store)
    for number, state in ((1, "needs-owner"), (2, "deployed")):
        opened = NOW - timedelta(hours=3 - number)
        case = ledger.new_case(SLUG, f"github:{REPO}#{number}", reporter="pat", at=opened)
        ledger.transition(case.id, state, at=opened, actor="liaise", reason="seeded")


def test_case_list_lists_every_case_or_those_in_one_state(root):
    store: dict = {}
    _seed_cases(store)
    assert cli.case_list(root=str(root), store=store).splitlines() == [
        f"{SLUG}-1\tneeds-owner\tgithub:{REPO}#1",
        f"{SLUG}-2\tdeployed\tgithub:{REPO}#2",
    ]
    assert cli.case_list(state="deployed", root=str(root), store=store) == f"{SLUG}-2\tdeployed\tgithub:{REPO}#2"
    assert cli.case_list(state="intake", root=str(root), store=store) == "(no cases in intake)"
    with pytest.raises(cw.CommandError, match="case state 'stuck' is not one of"):
        cli.case_list(state="stuck", root=str(root), store=store)


def test_case_set_state_moves_a_case_as_the_operator(root):
    store: dict = {}
    _seed_cases(store)
    output = cli.case_set_state(f"{SLUG}-1", "intake", reason="fixed the brief", root=str(root), store=store, now=NOW)

    assert output == f"moved {SLUG}-1 from needs-owner to intake; its labels follow on the next tick"
    case = Ledger(store).get_case(f"{SLUG}-1")
    transition = case.entries[-1]
    assert (case.state, transition.kind, transition.at, transition.actor) == ("intake", "transition", NOW, "operator")
    assert transition.detail == {"from": "needs-owner", "to": "intake", "reason": "fixed the brief"}
    assert cli.case_set_state(f"{SLUG}-1", "intake", root=str(root), store=store) == f"{SLUG}-1 is already intake"


def test_case_set_state_in_a_dry_run_says_what_it_would_do_and_writes_nothing(root, tmp_path):
    store: dict = {}
    _seed_cases(store)
    before = copy.deepcopy(store)

    output = cli.case_set_state(f"{SLUG}-2", "intake", dry_run=True, root=str(root), store=store, now=NOW)

    assert output.startswith(f"would move {SLUG}-2 from deployed to intake")
    assert store == before
    with pytest.raises(cw.CommandError, match=f"no case '{SLUG}-2'"):
        cli.case_set_state(f"{SLUG}-2", "intake", dry_run=True, root=str(root))  # the default ledger
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(
    "case_id, state, run_in_flight, message",
    [
        (f"{SLUG}-9", "intake", False, f"no case '{SLUG}-9'"),
        (f"{SLUG}-1", "stuck", False, "case state 'stuck' is not one of"),
        (f"{SLUG}-1", "working", False, "only the tick starts one"),
        (f"{SLUG}-1", "intake", True, f"has run {SLUG}-1-r1 in flight"),
    ],
)
def test_case_set_state_refuses_what_it_cannot_do_in_one_line(root, case_id, state, run_in_flight, message):
    store: dict = {}
    _seed_cases(store)
    if run_in_flight:
        run = RunRecord(
            run_id=f"{SLUG}-1-r1", case_id=f"{SLUG}-1", subject=SLUG, mode="fresh", status="running", started_at=NOW
        )
        Ledger(store).save_run(run)
    before = copy.deepcopy(store)
    with pytest.raises(cw.CommandError, match=message):
        cli.case_set_state(case_id, state, root=str(root), store=store)
    assert store == before


def test_case_set_state_refuses_while_a_tick_holds_the_run_lock(root, tmp_path):
    """S8 #5: a tick in flight would overwrite the move, so set-state takes the run lock, and
    does not wait for it. A dry run writes nothing, and needs no lock."""
    store: dict = {}
    _seed_cases(store)
    before = copy.deepcopy(store)
    with run_lock(run_lock_path(tmp_path / "state")):
        with pytest.raises(cw.CommandError, match=f"a liaise tick is running, so {SLUG}-1 was not moved; try again"):
            cli.case_set_state(f"{SLUG}-1", "intake", root=str(root), store=store, now=NOW)
        assert store == before
        planned = cli.case_set_state(f"{SLUG}-1", "intake", dry_run=True, root=str(root), store=store, now=NOW)
        assert planned.startswith(f"would move {SLUG}-1")
    moved = cli.case_set_state(f"{SLUG}-1", "intake", root=str(root), store=store, now=NOW)
    assert moved.startswith(f"moved {SLUG}-1 from needs-owner to intake")


def test_case_show_prints_what_a_notification_leaves_out(root):
    """S8 #2: the drafts with their text, the escalation's reason, the failed deploy's output."""
    store: dict = {}
    _seed_cases(store)
    ledger = Ledger(store)
    case_id = f"{SLUG}-1"
    draft = make_draft(
        at=NOW, outcome="escalate", recipient="pat", ref=f"github:{REPO}#1", text="It needs a paid plan.\nGo ahead?", reason="costs money"
    )
    ledger.save_case(replace(ledger.get_case(case_id), drafts=(draft,)))
    outcome = {"kind": "escalate", "questions": [], "reason": "costs money", "run_id": f"{case_id}-r1"}
    ledger.append(case_id, LedgerEntry(at=NOW, kind="outcome", actor="liaise", text="It needs a paid plan.", detail=outcome))
    failed = {"event": "deploy_failed", "cause": "exit code 1", "error": None}
    ledger.append(case_id, LedgerEntry(at=NOW, kind="run", actor="liaise", text="error: push refused", detail=failed))

    lines = cli.case_show(case_id, root=str(root), store=store).splitlines()

    stamp = NOW.isoformat(timespec="seconds")
    assert lines[:3] == [f"case: {case_id}", f"  subject: {SLUG}", "  state: needs-owner"]
    assert "last escalation reason: costs money" in lines
    deploy = lines.index(f"last failed deploy: {stamp} (exit code 1); its output:")
    assert lines[deploy + 1] == "    error: push refused"
    drafts = lines.index("drafts waiting for the operator: 1")
    assert lines[drafts + 1 : drafts + 4] == [
        f"  {NOW.isoformat()} escalate to github:{REPO}#1: costs money",
        "    It needs a paid plan.",
        "    Go ahead?",
    ]
    latest = lines.index("latest entries: 3 of 3")  # the seeded transition, the outcome, the failed deploy
    assert lines[latest + 2] == f"  {stamp} outcome by liaise: kind=escalate, reason=costs money, run_id={case_id}-r1 | It needs a paid plan."


def test_case_show_of_a_case_the_ledger_does_not_hold_is_one_line(root):
    with pytest.raises(cw.CommandError, match=f"no case '{SLUG}-9'"):
        cli.case_show(f"{SLUG}-9", root=str(root), store={})


# ---- the run's other seams (S7 #13, #14) ----


def test_run_passes_its_resolver_workspace_and_triage_to_the_tick(root, monkeypatch):
    seen = {}

    def spy(subjects, store, **kwargs):
        seen.clear()
        seen.update(kwargs)
        return TickReport()

    monkeypatch.setattr(cli, "run_once", spy)
    resolver = lambda address, subject: None  # noqa: E731
    workspace = lambda subject, **kwargs: None  # noqa: E731
    triage = lambda cases: [cases]  # noqa: E731

    cli.run(root=str(root), once=True, dry_run=True, resolver=resolver, workspace=workspace, triage=triage, **_fakes(root))
    assert (seen["resolver"], seen["workspace"], seen["triage"]) == (resolver, workspace, triage)

    cli.run(root=str(root), once=True, dry_run=True, **_fakes(root))
    assert ("resolver" in seen, "workspace" in seen, seen["triage"]) == (False, False, None)  # the tick's own defaults
