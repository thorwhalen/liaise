"""Tests for README.md and the shipped skill (A.9): the quick start actually
works, and the skill file is well-formed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent

REPO = "example/app"  # the README's own fictional partner repo


def test_readme_quick_start_config_shape_works(tmp_path):
    """Walk through the README's quick start (minus `liaise setup`/`schedule
    install`, which need `gh`/a real scheduler) against a clean config dir,
    and confirm `liaise partner show pat` resolves it correctly.
    """
    root = tmp_path / "config"
    (root / "partners").mkdir(parents=True)
    (root / "briefs").mkdir()

    (root / "config.toml").write_text(
        'owner_login = "you"\nstate_dir = "%s"\n' % (tmp_path / "state")
    )
    (root / "partners" / "pat.toml").write_text(
        'display_name = "Pat"\n'
        'github_logins = ["pat"]\n'
        f'repo = "{REPO}"\n'
        f'brief = "{root / "briefs" / "pat.md"}"\n'
    )
    (root / "briefs" / "pat.md").write_text(
        "Pat likes short, plain answers and hates surprises.\n"
    )

    out = subprocess.run(
        [sys.executable, "-m", "liaise", "partner", "show", "pat", "--root", str(root)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert out.returncode == 0
    assert "partner: pat" in out.stdout
    assert "display_name:   Pat" in out.stdout
    assert f"repo:           {REPO}" in out.stdout


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
