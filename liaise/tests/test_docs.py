"""Tests for README.md and the shipped skill (A.9): the quick start actually
works, and the skill file is well-formed.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from liaise.model import CASE_STATES

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent

REPO = "example/app"  # the README's own fictional repo
SUBJECT = "example-app"  # and its subject
#: What 0.1 removed: `subject` and `run --dry-run` replace them.
RETIRED_COMMANDS = ("liaise poll", "liaise partner")


def _bash_executable() -> str:
    """A real, usable `bash`.

    On Windows, the `bash` GitHub's runner puts first on PATH is the WSL
    launcher shim (`System32\\bash.exe`), not Git Bash — it runs and exits 1
    with "Windows Subsystem for Linux has no installed distributions"
    (confirmed in CI), which looks exactly like a script failure until you
    read the stdout. Git for Windows (present on every windows-latest
    runner) installs its own real bash at a fixed path; prefer that
    explicitly rather than trusting whatever `bash` PATH resolution finds.
    """
    if platform.system() == "Windows":
        for candidate in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        ):
            if Path(candidate).exists():
                return candidate
    return shutil.which("bash") or "bash"


def _readme() -> str:
    return (PACKAGE_ROOT / "README.md").read_text()


def _readme_quick_start_block() -> str:
    """The fenced shell block under README's "## Quick start" heading, verbatim."""
    readme = _readme()
    marker = "## Quick start"
    start = readme.index(marker)
    fence_start = readme.index("```", start) + 3
    fence_end = readme.index("```", fence_start)
    return readme[fence_start:fence_end].strip("\n")


def test_readme_quick_start_actually_builds_a_working_config(tmp_path):
    """M-9: drives the test from the README's own fenced block, instead of a
    hand-typed config that could drift from it silently, up to the first
    `liaise` command. Runs it as real shell (the heredocs, `~` expansion,
    quoting, exactly as a reader would type it) against a fake $HOME, then
    confirms `liaise subject show example-app` resolves the subject it built.
    """
    block = _readme_quick_start_block()
    lines = block.splitlines()

    first_command = next(i for i, line in enumerate(lines) if line.startswith("liaise "))
    setup_lines = [line for line in lines[:first_command] if line != "pip install liaise"]
    assert setup_lines, "README quick start block shape changed — nothing to run"

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # Override HOME on top of the real environment, not instead of it — a
    # from-scratch {"HOME": ..., "PATH": ...} environment starved Windows'
    # Git Bash of variables (SystemRoot, TEMP, ...) it needs just to start,
    # confirmed in CI: bash exited 1 before running a single line.
    env = {**os.environ, "HOME": str(fake_home)}
    result = subprocess.run(
        [_bash_executable(), "-c", "\n".join(setup_lines)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, (
        f"README quick start block failed under bash:\n{result.stderr}"
    )

    config_root = fake_home / ".config" / "liaise"
    assert (config_root / "config.toml").exists()
    assert (config_root / "subjects" / f"{SUBJECT}.toml").exists()

    out = subprocess.run(
        [sys.executable, "-m", "liaise", "subject", "show", SUBJECT, "--root", str(config_root)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert f"subject: {SUBJECT}" in out.stdout
    assert f"  bindings: github:{REPO}?labels=partner:pat" in out.stdout
    assert "  policy.roles.pat: partner" in out.stdout
    assert "binding problems: none" in out.stdout


def test_readme_quick_start_ends_at_a_read_only_command():
    """M-9: read-only by default is non-negotiable (A.1 rule 4). The quick start
    must not end on an acting command: `liaise schedule install` installs a
    recurring job, and `liaise run` without `--dry-run` acts. It ends at
    `liaise run --once --dry-run`, which prints a plan and changes nothing.
    """
    block = _readme_quick_start_block()
    lines = [line for line in block.splitlines() if line.strip()]
    last_command = lines[-1]
    assert "schedule install" not in last_command
    assert last_command.startswith("liaise run") and "--dry-run" in last_command


def test_readme_uses_the_fictional_partner_and_repo():
    readme = _readme()
    assert "pat" in readme
    assert REPO in readme


def test_readme_names_no_retired_command():
    readme = _readme()
    for retired in RETIRED_COMMANDS:
        assert retired not in readme


def test_skill_file_exists_and_has_valid_frontmatter():
    skill_path = PACKAGE_ROOT / ".claude" / "skills" / "liaise" / "SKILL.md"
    text = skill_path.read_text()

    assert text.startswith("---\n")
    end = text.index("\n---\n", 4)
    frontmatter = text[4:end]

    assert "name: liaise" in frontmatter
    assert "description:" in frontmatter
    # a real trigger phrase, not a placeholder
    assert "liaise" in frontmatter.split("description:", 1)[1].lower()


def test_skill_documents_every_state_label():
    skill_path = PACKAGE_ROOT / ".claude" / "skills" / "liaise" / "SKILL.md"
    text = skill_path.read_text()

    for state in CASE_STATES:
        assert f"liaise:{state}" in text
