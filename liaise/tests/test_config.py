"""Tests for liaise.config: the global config, and the 0.0.x partner files migrate-config reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from liaise.config import ConfigError, load_config, load_global_config


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
    assert pat.notify_login == "pat"  # default: first github_logins entry
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


def test_notify_login_explicit_override_beats_the_default(config_root):
    path = config_root / "partners" / "pat.toml"
    path.write_text('notify_login = "octocat"\n' + path.read_text())
    config = load_config(config_root)
    assert config.partner("pat").notify_login == "octocat"


def test_direct_reply_mode_without_a_login_to_mention_raises(config_root):
    path = config_root / "partners" / "pat.toml"
    # a partner identified by label only — filed through the app, no
    # `github_logins` of their own — and no `notify_login` set explicitly.
    # Prepend, not append: TOML keys after a [table] header (`[dispatch]`)
    # belong to that table.
    text = path.read_text().replace('github_logins = ["pat"]', "github_logins = []")
    path.write_text('reply_mode = "direct"\n' + text)
    with pytest.raises(ConfigError) as exc_info:
        load_config(config_root)
    message = str(exc_info.value)
    assert str(path) in message
    assert "notify_login" in message
    assert "direct" in message


def test_non_draft_reply_mode_typo_without_notify_login_also_raises(config_root):
    """Every posting site in the codebase gates on `!= "draft"`, not on the
    literal string "direct" — a typo'd `reply_mode` must not silently bypass
    the notify_login requirement and post directly with no mention.
    """
    path = config_root / "partners" / "pat.toml"
    text = path.read_text().replace('github_logins = ["pat"]', "github_logins = []")
    path.write_text('reply_mode = "direkt"\n' + text)
    with pytest.raises(ConfigError) as exc_info:
        load_config(config_root)
    assert "notify_login" in str(exc_info.value)


def test_direct_reply_mode_with_notify_login_set_on_its_own_loads_fine(config_root):
    path = config_root / "partners" / "pat.toml"
    text = path.read_text().replace('github_logins = ["pat"]', "github_logins = []")
    path.write_text('reply_mode = "direct"\nnotify_login = "octocat"\n' + text)
    config = load_config(config_root)
    pat = config.partner("pat")
    assert pat.github_logins == ()
    assert pat.notify_login == "octocat"


def test_unknown_partner_names_slug_and_known_partners(config_root):
    config = load_config(config_root)
    with pytest.raises(ConfigError) as exc_info:
        config.partner("nope")
    message = str(exc_info.value)
    assert "nope" in message
    assert "pat" in message


def test_load_global_config_reads_config_toml_alone(tmp_path):
    """0.1 reads only config.toml: a root with no partners directory loads."""
    root = tmp_path / "config"
    root.mkdir()
    (root / "config.toml").write_text(
        'owner_login = "owner"\nstate_dir = "~/state"\n\n[notify]\nntfy_topic_env = "EXAMPLE_NTFY_TOPIC"\n'
    )
    glob = load_global_config(root)
    assert (glob.owner_login, glob.state_dir, glob.notify.ntfy_topic_env) == (
        "owner",
        "~/state",
        "EXAMPLE_NTFY_TOPIC",
    )
    assert load_config(root).global_ == glob
    with pytest.raises(ConfigError, match="owner_login"):
        load_global_config(tmp_path / "nowhere")


def test_a_0_0_x_log_dir_in_config_toml_still_loads_and_is_not_kept(config_root):
    """0.1 has no dispatch log (a run's files live under state_dir/runs): a leftover
    `log_dir` must not stop the config from loading."""
    path = config_root / "config.toml"
    path.write_text('log_dir = "dispatch-logs"\n' + path.read_text())
    assert not hasattr(load_global_config(config_root), "log_dir")


def test_permission_mode_defaults_to_auto_and_is_set_under_dispatch(config_root):
    assert load_config(config_root).partner("pat").dispatch.permission_mode == "auto"
    path = config_root / "partners" / "pat.toml"
    # the fixture's file ends inside its [dispatch] table
    path.write_text(path.read_text() + 'permission_mode = "acceptEdits"\n')
    pat = load_config(config_root).partner("pat")
    assert pat.dispatch.permission_mode == "acceptEdits"


def _with_dispatch_command(config_root, command: str) -> None:
    path = config_root / "partners" / "pat.toml"
    # the fixture's file ends inside its [dispatch] table
    path.write_text(path.read_text() + f'command = "{command}"\n')


def test_a_0_0_3_style_template_override_still_loads(config_root):
    """#22 review: refusing overrides at load broke consistent configs (a
    copied default plus one flag), and every command with them.
    """
    _with_dispatch_command(
        config_root, "claude -p {prompt_file} --permission-mode auto --model sonnet"
    )
    assert load_config(config_root).partner("pat").dispatch.permission_mode == "auto"


@pytest.mark.parametrize(
    "flag", ["--permission-mode acceptEdits", "--permission-mode=acceptEdits"]
)
def test_an_override_hardcoding_its_permission_mode_resumes_under_it(config_root, flag):
    """#22 review: a 0.0.3 `command` hardcoding its mode, with the default
    `resume_command`, must not quietly resume under `auto` instead. The
    override is where the mode comes from, so the resume takes it from there.
    """
    _with_dispatch_command(config_root, f"claude -p {{prompt_file}} {flag}")
    dispatch = load_config(config_root).partner("pat").dispatch
    assert dispatch.permission_mode == "acceptEdits"
    assert "--permission-mode {permission_mode}" in dispatch.resume_command


def test_an_override_passing_no_permission_mode_resumes_without_one(config_root):
    _with_dispatch_command(config_root, "claude -p {prompt_file} --output-format json")
    dispatch = load_config(config_root).partner("pat").dispatch
    assert "--permission-mode" not in dispatch.resume_command
