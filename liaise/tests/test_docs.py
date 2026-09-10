"""Tests for README.md and the shipped skill (A.9): the quick start actually
works, and the skill file is well-formed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent

REPO = "example/app"  # the README's own fictional partner repo


def _readme_quick_start_block() -> str:
    """The fenced shell block under README's "## Quick start" heading, verbatim."""
    readme = (PACKAGE_ROOT / "README.md").read_text()
    marker = "## Quick start"
    start = readme.index(marker)
    fence_start = readme.index("```", start) + 3
    fence_end = readme.index("```", fence_start)
    return readme[fence_start:fence_end].strip("\n")


def test_readme_quick_start_actually_builds_a_working_config(tmp_path):
    """M-9: drives the test from the README's own fenced block — instead of a
    hand-typed config that could drift from it silently — up to the first
    line that needs a real `gh` (`liaise setup pat`). Runs it as real shell
    (the heredocs, `~` expansion, quoting — exactly as a reader would type
    it) against a fake $HOME, then confirms `liaise partner show pat`
    resolves the config it actually built.
    """
    block = _readme_quick_start_block()
    lines = block.splitlines()

    setup_end = next(i for i, line in enumerate(lines) if line.startswith("liaise setup"))
    setup_lines = [line for line in lines[:setup_end] if line != "pip install liaise"]
    assert setup_lines, "README quick start block shape changed — nothing to run"

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # Override HOME on top of the real environment, not instead of it — a
    # from-scratch {"HOME": ..., "PATH": ...} environment starved Windows'
    # Git Bash of variables (SystemRoot, TEMP, ...) it needs just to start,
    # confirmed in CI: bash exited 1 before running a single line.
    env = {**os.environ, "HOME": str(fake_home)}
    result = subprocess.run(
        ["bash", "-c", "\n".join(setup_lines)],
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
    assert (config_root / "partners" / "pat.toml").exists()
    assert (config_root / "briefs" / "pat.md").exists()

    out = subprocess.run(
        [sys.executable, "-m", "liaise", "partner", "show", "pat", "--root", str(config_root)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert out.returncode == 0
    assert "partner: pat" in out.stdout
    assert "display_name:   Pat" in out.stdout
    assert f"repo:           {REPO}" in out.stdout


def test_readme_quick_start_does_not_end_by_installing_a_live_daemon():
    """M-9: read-only-by-default is non-negotiable (A.1 rule 4) — the quick
    start must not end on an acting command (`liaise schedule install`
    installs a recurring scheduled job). It should end at a read-only
    command (`poll`, or `run --once --dry-run`).
    """
    block = _readme_quick_start_block()
    lines = [line for line in block.splitlines() if line.strip()]
    last_command = lines[-1]
    assert "schedule install" not in last_command
    assert last_command.startswith("liaise poll") or "--dry-run" in last_command


def test_readme_uses_the_fictional_partner_and_repo():
    readme = (PACKAGE_ROOT / "README.md").read_text()
    assert "pat" in readme
    assert REPO in readme


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
    from liaise.state import STATE_LABELS

    for state in STATE_LABELS:
        assert f"liaise:{state}" in text
