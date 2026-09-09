"""Tests for liaise.config: loading, defaults, missing-file errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from liaise.cli import partner_show
from liaise.config import ConfigError, load_config


def test_load_config_returns_frozen_dataclasses_with_defaults(config_root):
    config = load_config(config_root)

    assert config.global_.owner_login == "owner"
    with pytest.raises(AttributeError):
        config.global_.owner_login = "someone-else"  # frozen

    pat = config.partner("pat")
    assert pat.display_name == "Pat"
    assert pat.github_logins == ("pat",)
    assert pat.repo == "example/app"
    # not set explicitly in the fixture -> inherited default
    assert pat.quiet_minutes == 10
    assert pat.go_minutes == 2
    assert pat.markers.go == "#startwork#"
    assert pat.markers.wait == "#wait#"
    assert pat.label_prefix == "liaise:"
    assert pat.deploy_per == "batch"
    assert pat.budget.timeout_minutes == 60
    assert pat.budget.max_turns == 200
    assert pat.budget.daily_dispatches == 6
    assert pat.reply_mode == "draft"
    assert pat.label == "partner:pat"
    with pytest.raises(AttributeError):
        pat.repo = "example/other"  # frozen


def test_partner_override_beats_global_default(config_root):
    path = config_root / "partners" / "pat.toml"
    # prepend, not append: TOML keys after a [table] header belong to that
    # table, so an override must land before [dispatch].
    path.write_text('quiet_minutes = 30\nreply_mode = "direct"\n' + path.read_text())
    config = load_config(config_root)
    pat = config.partner("pat")
    assert pat.quiet_minutes == 30
    assert pat.reply_mode == "direct"
    # unrelated defaults still inherited
    assert pat.go_minutes == 2


def test_missing_global_config_names_the_path_and_minimal_content(tmp_path: Path):
    missing_root = tmp_path / "nowhere"
    with pytest.raises(ConfigError) as exc_info:
        load_config(missing_root)
    message = str(exc_info.value)
    assert str(missing_root / "config.toml") in message
    assert "owner_login" in message
    assert "state_dir" in message


def test_missing_partner_field_names_the_path(config_root):
    bad = config_root / "partners" / "broken.toml"
    bad.write_text('display_name = "Broken"\n')  # missing github_logins etc.
    with pytest.raises(ConfigError) as exc_info:
        load_config(config_root)
    message = str(exc_info.value)
    assert str(bad) in message
    assert "github_logins" in message


def test_unknown_partner_names_slug_and_known_partners(config_root):
    config = load_config(config_root)
    with pytest.raises(ConfigError) as exc_info:
        config.partner("nope")
    message = str(exc_info.value)
    assert "nope" in message
    assert "pat" in message


def test_partner_show_prints_resolved_partner(config_root):
    output = partner_show("pat", root=str(config_root))
    assert "partner: pat" in output
    assert "display_name:   Pat" in output
    assert "repo:           example/app" in output
    assert "github_logins:  pat" in output
