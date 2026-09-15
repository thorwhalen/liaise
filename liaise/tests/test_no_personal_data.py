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
#: The same shape after `--repo` (a `gh`/README invocation), or where a URL names
#: an owner on a code host, in any letter case: a repository, a gist, a raw file,
#: or an API path for a repository, user or organisation. A GitHub subdomain that
#: holds no repositories is not a context: `docs.github.com/en/code-security/...`
#: is a documentation page.
OWNER_REPO_CONTEXT_RE = re.compile(
    r"(?:--repo[= ]"
    r"|(?<![\w.])(?:www\.|gist\.)?github\.com/"
    r"|(?<![\w.])(?:api|uploads)\.github\.com/(?:repos|users|orgs)/"
    r"|(?<![\w.])raw\.githubusercontent\.com/"
    r"|(?<![\w.])(?:www\.)?(?:gitlab\.com|bitbucket\.org|codeberg\.org)/)"
    r"([a-zA-Z0-9][a-zA-Z0-9-]{1,38})/([a-zA-Z0-9._-]+)",
    re.IGNORECASE,
)
#: A URL, up to the whitespace, bracket, quote, table pipe or list punctuation
#: that ends it in Markdown or prose.
URL_RE = re.compile(r"https?://[^\s)\]<>\"'`|*{},;]+")
#: A URL that points at code hosting or a package or model registry, by its host
#: or its path: a repository, a raw file, an image or package namespace, or a
#: badge or notebook link such as `img.shields.io/github/...`.
CODE_HOSTING_URL_RE = re.compile(
    r"github\.com|githubusercontent\.com|gitlab\.com|bitbucket\.org|codeberg\.org"
    r"|huggingface\.co|hub\.docker\.com|npmjs\.com|sr\.ht|travis-ci\.|deepwiki\.com"
    r"|/gh/|/github/",
    re.IGNORECASE,
)
#: Code-host subdomains that serve documentation, never repositories.
DOCUMENTATION_HOSTS = frozenset({"docs.github.com", "support.github.com", "docs.gitlab.com"})
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
#: `notify_login = "..."` — the other place a bare GitHub login can appear
#: (#20): a partner identified by label only sets this directly, outside
#: any `github_logins` array, so it needs its own scan.
NOTIFY_LOGIN_RE = re.compile(r"""notify_login\s*=\s*["']([a-zA-Z0-9][a-zA-Z0-9-]{0,38})["']""")

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


def _without_documentation_urls(text: str) -> str:
    """``text`` without the URLs that cannot name a repository owner.

    A documentation link's path (a locale such as `en-us` before a product
    segment) is made of hyphenated words, which the bare check would read as
    owner/repo slugs. A URL that points at code hosting stays, unless its host
    only serves documentation, since its path can name an owner.
    """

    def keep_or_drop(match: re.Match) -> str:
        url = match.group(0)
        host = url.split("/")[2].lower()
        if host in DOCUMENTATION_HOSTS or not CODE_HOSTING_URL_RE.search(url):
            return " "
        return url

    return URL_RE.sub(keep_or_drop, text)


def _owner_repo_offenders(text: str) -> list[str]:
    offenders = []
    checks = (
        (OWNER_REPO_LITERAL_RE, text),
        (OWNER_REPO_CONTEXT_RE, text),
        (OWNER_REPO_BARE_RE, _without_documentation_urls(text)),
    )
    for regex, scanned in checks:
        for owner, repo in regex.findall(scanned):
            if owner.lower() != ALLOWED_OWNER:
                offenders.append(f"{owner}/{repo}")
    return offenders


def _login_offenders(text: str) -> list[str]:
    offenders = []
    for array_body in GITHUB_LOGINS_ARRAY_RE.findall(text):
        for login in LOGIN_LITERAL_RE.findall(array_body):
            if login not in ALLOWED_LOGINS:
                offenders.append(login)
    for login in NOTIFY_LOGIN_RE.findall(text):
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


def test_login_offenders_catches_a_standalone_notify_login():
    """#20: `notify_login = "..."` is a second place a bare GitHub login can
    appear, outside any `github_logins` array — must not be a blind spot.

    Built by concatenation, not as a literal `notify_login = "..."` string —
    a literal here would trip this very guard against this very file.
    """
    fake_login = "not" + "-fictional"
    text = "notify_login" + " = " + '"' + fake_login + '"'
    assert _login_offenders(text) == [fake_login]
    assert _login_offenders('notify_login = "pat"') == []


def test_owner_repo_offenders_reads_links_as_links():
    """A research document cites documentation whose URL paths are hyphenated
    words (a locale such as `en-us` before a product segment), which the bare
    prose check would read as owner/repo slugs. The bare check skips those
    URLs, and still reads every URL that points at code hosting. Every URL
    that names an owner on a code host is caught, in any letter case, and so
    is a repository named in prose, a table cell or a list right after a
    documentation link.

    Built by concatenation, so this file does not trip the guard it tests.
    """
    owner = "someone" + "-real"
    login = "some" + "one"
    docs = (
        "[labels](https://learn.microsoft.com/en" + "-us/purview/sensitivity-labels)"
        " and https://docs.github.com/en/code" + "-security/secret-scanning"
        " and https://docs.gitlab.com/ee/user/project" + "-settings/access"
        " and https://support.github.com/en/code" + "-security/secret-scanning"
    )
    assert _owner_repo_offenders(docs) == []
    # A login without a hyphen escapes the bare check, so only the context
    # check can catch these.
    for text in (
        "https://github.com/" + login + "/notes",
        "https://www.GitHub.com/" + login + "/notes",
        "https://gist.github.com/" + login + "/abc123",
        "GET https://api.github.com/repos/" + login + "/notes",
        "GET https://api.github.com/users/" + login + "/repos",
        "GET https://api.github.com/orgs/" + login + "/repos",
        "POST https://uploads.github.com/repos/" + login + "/notes/releases",
        "https://raw.githubusercontent.com/" + login + "/notes/main/README.md",
        "https://gitlab.com/" + login + "/notes",
        "https://bitbucket.org/" + login + "/notes",
        "https://codeberg.org/" + login + "/notes",
        "--repo " + login + "/notes",
    ):
        assert _owner_repo_offenders(text), text
    # No context names these owners, so only the bare check can catch them: it
    # must keep reading URLs that point at code hosting or a registry, and a
    # documentation URL must not swallow the text after it.
    for text in (
        "https://img.shields.io/GitHub/stars/" + owner + "/notes",
        "https://codecov.io/gh/" + owner + "/notes",
        "https://user-images.githubusercontent.com/1/" + owner + "/notes",
        "https://huggingface.co/" + owner + "/notes",
        "https://hub.docker.com/r/" + owner + "/notes",
        "https://www.npmjs.com/package/@" + owner + "/notes",
        "https://git.sr.ht/~" + owner + "/notes",
        "https://travis-ci.org/" + owner + "/notes",
        "https://deepwiki.com/" + owner + "/notes",
        "https://docs.example.com/x " + owner + "/notes",
        "|https://docs.example.com/x|" + owner + "/notes|",
        "https://docs.example.com/x," + owner + "/notes",
        "https://docs.example.com/x;" + owner + "/notes",
        "https://docs.example.com/x*" + owner + "/notes",
        "https://docs.example.com/x{" + owner + "/notes}",
        "https://docs.example.com/x}" + owner + "/notes",
        "https://docs.example.com/x<" + owner + "/notes",
        "see " + owner + "/notes for it",
    ):
        assert owner + "/notes" in _owner_repo_offenders(text), text
    assert _owner_repo_offenders("https://github.com/example/app") == []
