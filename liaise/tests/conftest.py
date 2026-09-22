"""Shared fixtures: a fictional config tree, so no test ever touches a real partner."""

from __future__ import annotations

import platform
import stat
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _correspond_data_root(tmp_path_factory, monkeypatch):
    """Point correspond's data root at a temporary folder, so no test writes the real one.

    liaise keeps its idempotency records under its own ``state_dir``, and a send with no
    store sends unkeyed; this is the guard for anything that slips past both.
    """
    folder = tmp_path_factory.mktemp("correspond-data")
    monkeypatch.setenv("CORRESPOND_DATA_DIR", str(folder))


def write_executable_script(path: Path, body: str) -> Path:
    """Write `body` (Python source) as a script runnable via
    `subprocess.run([returned_path, *args])` — no shell, no `sys.executable`
    prefix needed by the caller. Returns the actual runnable path (which may
    differ from `path` on Windows).

    POSIX: a shebang line + the executable bit — the standard trick.
    Windows: `subprocess.run(..., shell=False)` calls `CreateProcess`
    directly, which does NOT consult the `.py` file-association registry
    the way a real shell would — a bare shebang script fails with
    `OSError: [WinError 193] %1 is not a valid Win32 application` (confirmed
    in CI). So on Windows this writes the body to a companion `.py` file and
    returns a tiny `.bat` wrapper that invokes `sys.executable` on it
    explicitly and forwards all arguments.
    """
    if platform.system() == "Windows":
        impl = path.with_name(path.name + "_impl.py")
        impl.write_text(body)
        bat = path.with_name(path.name + ".bat")
        bat.write_text(f'@echo off\r\n"{sys.executable}" "{impl}" %*\r\n')
        return bat
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path

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


@pytest.fixture(autouse=True)
def no_real_state_dir(tmp_path_factory, monkeypatch):
    """The fingerprint key a test makes without naming a state directory lives under a temporary one.

    The gate fingerprints its findings with a key it creates on first use, in the state
    directory of the liaise config; a test that sends through the Python API names none,
    and must never read the real config or write into the real state directory.
    """
    from liaise import detect

    state_dir = tmp_path_factory.mktemp("state")
    monkeypatch.setattr(detect, "_configured_state_dir", lambda: state_dir)
