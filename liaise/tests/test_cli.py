"""Tests for liaise.cli: the 0.1 command tree, and each command over a fictional config root.

Every root is under tmp_path and every processor an EchoProcessor, so no test runs claude or
gh, sends anything, or reads the real config root. `liaise run --once --dry-run` end to end
is test_smoke.py.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import cw
import pytest

from liaise import cli
from liaise.github import FakeGitHub
from liaise.ledger import Ledger
from liaise.model import CASE_STATES
from liaise.processor import EchoProcessor
from liaise.testing import FakeGitHubChannel, demo_registry

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
    assert set(commands) == {"run", "status", "hold", "unhold", "subject", "setup", "migrate-config", "schedule"}
    assert set(commands["subject"]) == {"list", "show"}
    assert set(commands["schedule"]) == {"install", "uninstall", "status"}


def test_run_takes_its_flags_and_hides_its_seams():
    parser = cw.mk_parser(cli._dispatch_funcs, config=cli._dispatch_config, prog="liaise")
    parsed = parser.parse_args(["run", "--once", "--dry-run", "--subject", SLUG, "--root", "somewhere"])
    assert (parsed.once, parsed.dry_run, parsed.subject, parsed.root) == (True, True, SLUG, "somewhere")
    for seam in ("--registry", "--processor", "--labeler", "--store", "--notify-fn", "--sessions-dir", "--now"):
        with pytest.raises(SystemExit):
            parser.parse_args(["run", seam, "x"])


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
