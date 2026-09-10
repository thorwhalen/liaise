"""The no-personal-data guard (A.1 rule 1).

Partner identities, repos, briefs, commands and hosts are configuration under
``~/.config/liaise/``, never in this repository's code, tests, docs, data or
skill files. This scans the whole repository — deliberately excluding only
``pyproject.toml`` and ``LICENSE`` (project metadata, not partner data) — for
four things: email addresses, absolute local filesystem paths, a real-looking
``owner/repo`` string, and a GitHub login not on the small fictional
allowlist.

Mutation check: comment out any ``assert not`` below and the corresponding
test must fail — that is what proves the guard actually guards something.
Verified against a matrix of six injection shapes (local path + email,
GitHub login shape, owner/repo shape, hostname-with-@) each in a file the
guard actually reads.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Project metadata, not partner data — the package's own name/URL legitimately
#: appears here (``thorwhalen/liaise``, the author line) and is out of scope for
#: this guard, which is about partner identities and hosts, not the package's own.
_EXCLUDED_FILES = {"pyproject.toml", "LICENSE"}
#: Not source: virtualenvs (CI's `uv sync` creates `.venv` INSIDE the repo
#: checkout, full of third-party package METADATA files carrying real email
#: addresses and repo-shaped strings — caught this the hard way, green
#: locally with no .venv present, red in CI with one), caches, build output.
_SKIP_DIR_NAMES = {
    ".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "dist", "build", ".venv", "venv", ".tox", "node_modules",
}

#: Walks the filesystem directly (not `git ls-files`) so a newly-written,
#: not-yet-committed file is covered too — the moment that matters most.
_TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".json", ".txt",
    ".yml", ".yaml", ".sh", ".cfg", ".plist", "",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+")
ABS_LOCAL_PATH_RE = re.compile(r"/(?:Users|home|root)/[^\s\"'()]+")
#: `\b` is load-bearing: without it this matches the "t" in ordinary prose
#: like "at least:\n\n" (a Python source file's literal backslash-n), which
#: is not a path at all — a drive letter is always its own token. Scoped to
#: `\Users\` specifically (mirroring ABS_LOCAL_PATH_RE's POSIX scope) so a
#: generic, non-personal system path like `C:\Program Files\Git\bin\bash.exe`
#: — needed for real cross-platform test code — isn't a false positive.
WINDOWS_LOCAL_PATH_RE = re.compile(r"\b[A-Za-z]:\\Users\\[^\s\"'<>]+")

#: A quoted, EXACTLY-two-segment value shaped like a GitHub owner-slash-repo —
#: anchored to the quote characters so a longer path (e.g. a quoted brief
#: path under ~/.config/liaise/) can never satisfy it: there is nothing in
#: the pattern between the two captured segments and the quotes but the "/".
OWNER_REPO_LITERAL_RE = re.compile(
    r"""["']([a-zA-Z0-9][a-zA-Z0-9-]{1,38})/([a-zA-Z0-9._-]+)["']"""
)
#: The same shape after `--repo` (a `gh`/README invocation) or in a
#: `github.com/` URL.
OWNER_REPO_CONTEXT_RE = re.compile(
    r"(?:--repo[= ]|github\.com/)([a-zA-Z0-9][a-zA-Z0-9-]{1,38})/([a-zA-Z0-9._-]+)"
)
#: Bare (unquoted) prose mentions — e.g. a repo named in a markdown sentence.
#: Requiring a hyphen or digit in the owner segment is what keeps this from
#: matching this package's own plain-lowercase-word path fragments
#: (`liaise/tests`, `config/liaise`, `local/share`, ...): real org/repo slugs
#: commonly carry a hyphen or digit, our own internal path words never do.
#: A narrower net than the quoted/context checks above, on purpose.
OWNER_REPO_BARE_RE = re.compile(
    r"\b([a-zA-Z][a-zA-Z0-9]*-[a-zA-Z0-9-]{1,37})/([a-zA-Z0-9][a-zA-Z0-9._-]*)\b"
)
#: `github_logins = [...]` TOML/Python array literals, whose quoted entries
#: must all be fictional.
GITHUB_LOGINS_ARRAY_RE = re.compile(r"github_logins\s*=\s*\[([^\]]*)\]")
LOGIN_LITERAL_RE = re.compile(r"""["']([a-zA-Z0-9][a-zA-Z0-9-]{0,38})["']""")

#: The only owner segment allowed in an owner/repo-shaped literal.
ALLOWED_OWNER = "example"
#: The only GitHub logins allowed anywhere `github_logins` is set.
ALLOWED_LOGINS = {"pat", "octocat"}


def _repo_text_files() -> list[Path]:
    files = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.name in _EXCLUDED_FILES:
            continue
        if path.suffix not in _TEXT_SUFFIXES:
            continue
        if any(part in _SKIP_DIR_NAMES for part in path.parts):
            continue
        # Belt and suspenders on the .venv miss above: installed-package
        # metadata (site-packages, *.dist-info, *.egg-info) is never source,
        # regardless of what the virtualenv directory happens to be named.
        if any(
            part == "site-packages" or part.endswith((".dist-info", ".egg-info"))
            for part in path.parts
        ):
            continue
        files.append(path)
    return files


def _owner_repo_offenders(text: str) -> list[str]:
    offenders = []
    for regex in (OWNER_REPO_LITERAL_RE, OWNER_REPO_CONTEXT_RE, OWNER_REPO_BARE_RE):
        for owner, repo in regex.findall(text):
            if owner.lower() != ALLOWED_OWNER:
                offenders.append(f"{owner}/{repo}")
    return offenders


def _login_offenders(text: str) -> list[str]:
    offenders = []
    for array_body in GITHUB_LOGINS_ARRAY_RE.findall(text):
        for login in LOGIN_LITERAL_RE.findall(array_body):
            if login not in ALLOWED_LOGINS:
                offenders.append(login)
    return offenders


def test_repo_has_no_email_addresses_or_local_paths():
    offenders_email: list[str] = []
    offenders_path: list[str] = []
    for path in _repo_text_files():
        text = path.read_text(errors="replace")
        if EMAIL_RE.search(text):
            offenders_email.append(str(path))
        if ABS_LOCAL_PATH_RE.search(text) or WINDOWS_LOCAL_PATH_RE.search(text):
            offenders_path.append(str(path))

    assert not offenders_email, f"email address(es) found in: {offenders_email}"
    assert not offenders_path, f"absolute local path(s) found in: {offenders_path}"


def test_repo_has_no_real_owner_repo_strings():
    offenders: dict[str, list[str]] = {}
    for path in _repo_text_files():
        found = _owner_repo_offenders(path.read_text(errors="replace"))
        if found:
            offenders[str(path)] = found
    assert not offenders, f"non-fictional owner/repo string(s): {offenders}"


def test_repo_has_no_real_github_logins():
    offenders: dict[str, list[str]] = {}
    for path in _repo_text_files():
        found = _login_offenders(path.read_text(errors="replace"))
        if found:
            offenders[str(path)] = found
    assert not offenders, f"non-fictional github login(s): {offenders}"
