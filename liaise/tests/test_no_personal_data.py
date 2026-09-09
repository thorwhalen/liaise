"""The no-personal-data guard (A.1 rule 1).

Partner identities, repos, briefs, commands and hosts are configuration under
``~/.config/liaise/``, never in this package's code, tests, docs or data files.
This scans the package's own tracked source tree — not the repo's project
metadata (``pyproject.toml``, ``LICENSE``, ``README.md``'s author line) — for
the two concrete, generically-detectable leaks: email addresses, and absolute
local filesystem paths that would name a real machine or user.

Mutation check: comment out either ``assert not`` below and this test must
fail — that is what proves the guard actually guards something.
"""

from __future__ import annotations

import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Matches this file's own docstring/code, so exclude test_no_personal_data.py.
EMAIL_RE = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+")
ABS_LOCAL_PATH_RE = re.compile(r"/(?:Users|home|root)/[^\s\"'()]+")

#: Walks the filesystem directly (not `git ls-files`) so a newly-written,
#: not-yet-committed file is covered too — the moment that matters most.
_TEXT_SUFFIXES = {".py", ".md", ".toml", ".json", ".txt"}
_SKIP_DIR_NAMES = {"__pycache__", ".pytest_cache"}


def _package_text_files() -> list[Path]:
    files = []
    for path in PACKAGE_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        if any(part in _SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.name == "test_no_personal_data.py":
            continue
        files.append(path)
    return files


def test_package_tree_has_no_email_addresses_or_local_paths():
    offenders_email: list[str] = []
    offenders_path: list[str] = []
    for path in _package_text_files():
        text = path.read_text(errors="replace")
        if EMAIL_RE.search(text):
            offenders_email.append(str(path))
        if ABS_LOCAL_PATH_RE.search(text):
            offenders_path.append(str(path))

    assert not offenders_email, f"email address(es) found in: {offenders_email}"
    assert not offenders_path, f"absolute local path(s) found in: {offenders_path}"
