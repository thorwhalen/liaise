"""Shared fixtures: a fictional config tree, so no test ever touches a real partner."""

from __future__ import annotations

from pathlib import Path

import pytest

GLOBAL_CONFIG_TOML = """
owner_login = "owner"
state_dir = "{state_dir}"

[notify]
ntfy_topic_env = "LIAISE_NTFY_TOPIC"
"""

PAT_PARTNER_TOML = """
display_name = "Pat"
github_logins = ["pat"]
repo = "example/app"
brief = "{brief_path}"

[dispatch]
cwd = "{cwd}"
"""


@pytest.fixture
def config_root(tmp_path: Path) -> Path:
    """A fictional ``~/.config/liaise``-shaped tree with one partner, ``pat``."""
    root = tmp_path / "config"
    (root / "partners").mkdir(parents=True)
    briefs_dir = root / "briefs"
    briefs_dir.mkdir()
    state_dir = tmp_path / "state"

    brief_path = briefs_dir / "pat.md"
    brief_path.write_text("Pat is friendly and non-technical. Keep it plain.\n")

    # .as_posix(): a TOML basic string treats "\" as an escape character, so
    # a raw Windows path (backslash-separated) breaks parsing — verified in
    # CI (tomllib.TOMLDecodeError: Invalid hex value) where this was
    # green on every platform this was ever run on by hand (all POSIX).
    # Forward slashes are accepted by pathlib on Windows too.
    (root / "config.toml").write_text(
        GLOBAL_CONFIG_TOML.format(state_dir=state_dir.as_posix())
    )
    (root / "partners" / "pat.toml").write_text(
        PAT_PARTNER_TOML.format(brief_path=brief_path.as_posix(), cwd=tmp_path.as_posix())
    )
    return root
