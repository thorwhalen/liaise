"""The Claude Code hook: every ``gh`` or ``correspond`` write from any session is vetted first (discussion 32, §5.8).

``liaise hook install`` adds two hooks to the user's Claude Code settings, each running
``liaise vet --hook``:

- **PreToolUse**, on ``Bash`` and on correspond's MCP write tools. :func:`pre_tool_use`
  reads the hook's JSON, finds each write in it (:func:`writes_in`), vets it
  (:func:`liaise.vet.vet`, with the provenance unknown) and answers ``deny`` with the
  reasons for a block and ``ask`` with the reasons for anything for the operator. A write
  it cannot read (a body in a file that is not there, a body the shell computes, a ``gh``
  command behind ``eval`` or ``xargs``) is ``ask`` with the reason, never let through.
- **PostToolUse**, on the same tools. When a command the hook answered ``ask`` ran, the
  operator said yes: :func:`post_tool_use` records that in the ledger as an override
  (:meth:`liaise.ledger.Ledger.add_override`), never the text.

**The hook only tightens.** A write the gate would send, and every command that is not a
write, gets no answer at all: the hook prints nothing and exits 0, so the operator's own
permission rules decide, as they did before the hook. Claude Code's ``allow`` would skip
the operator's prompt, which is new outbound behaviour, so the hook never gives it. A
``delay`` is ``ask``, since the write happens at once and nothing can hold it.

**The grammar is a fixed table** (:data:`GH_WRITES`, not a seam): ``gh issue
comment|create|edit``, ``gh pr comment|create|edit|review``, ``gh api`` writes to issues,
comments, pulls, discussions and GraphQL mutations, ``correspond send|edit``, and the
correspond MCP tools ``send`` and ``edit``. Bodies come from ``--body``/``-b``,
``--body-file``/``-F`` (a regular file; ``-`` is the command's one heredoc, or
``cat <<'EOF' |`` before it), ``-f body=…``, ``-F body=@file`` and ``--input``.

**Only plain commands are read** (:func:`writes_in`). A command that names ``gh`` or
``correspond`` is read when it is a list of ``gh``/``correspond`` commands, a
``cat <<'EOF' |`` feeding one, or a gh read piped into a filter. Anything else in it
(another program, ``cd``, a ``$`` or a backtick, an assignment such as ``GH_REPO=…``, a
subshell or compound command, a redirection but ``2>&1`` and ``>/dev/null``, a second
heredoc, a cluster of short flags, a ``gh`` alias or extension, a ``gh`` command outside
the table that changes something) is ``ask``: an independent review showed that each
shell construct the hook tries to see through is one more way around it. A ``GH_REPO``
already exported in the session's shell is not visible to the hook, which then judges
the checkout's repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

from liaise.ledger import Ledger
from liaise.policy import ROUTE_BLOCK, ROUTE_DRAFT, ROUTE_SEND
from liaise.vet import immediate_route, vet

#: What the hook answers, least restrictive first. ``allow`` is never printed.
ALLOW, ASK, DENY = "allow", "ask", "deny"
DECISIONS = (ALLOW, ASK, DENY)
_FOR_ROUTE = {ROUTE_SEND: ALLOW, ROUTE_DRAFT: ASK, ROUTE_BLOCK: DENY}
#: The command both hooks run.
HOOK_COMMAND = "liaise vet --hook"
#: The tools the hooks watch: Bash, and correspond's MCP write tools under any server name
#: that holds "correspond".
HOOK_MATCHER = r"Bash|mcp__.*correspond.*__(send|edit)"
PRE_TOOL_USE, POST_TOOL_USE = "PreToolUse", "PostToolUse"
HOOK_EVENTS = (PRE_TOOL_USE, POST_TOOL_USE)
#: The user's Claude Code settings, where ``liaise hook install`` writes.
DFLT_SETTINGS = Path("~/.claude/settings.json")
#: How long a pending ask waits for its tool to run before it is pruned (an answer of no
#: leaves one behind).
PENDING_TTL = timedelta(hours=24)
#: The ref a write goes to when the command does not say and the checkout cannot tell:
#: its audience is unknown, which resolves to public.
UNKNOWN_REF = "unknown:destination"
#: The largest body file the hook reads; a larger one is ask.
MAX_BODY_FILE_BYTES = 1_000_000
#: What the hook answers when it cannot read a write's body.
NO_BODY = "could not read the body"
#: The flags of a ``gh`` command that carry text to someone.
GH_BODY_FLAGS = frozenset(
    {
        "--body",
        "-b",
        "--body-file",
        "-F",
        "--title",
        "-t",
        "--notes",
        "--notes-file",
        "-n",
        "-f",
        "--field",
        "--raw-field",
        "--input",
        "--message",
        "-m",
        "-c",
        "--comment",
    }
)
#: ``gh`` verbs that only read, whatever flags they take (``gh run list -b main``).
GH_READ_VERBS = frozenset(
    {
        "list",
        "view",
        "status",
        "diff",
        "checks",
        "watch",
        "download",
        "browse",
        "search",
        "get",
    }
)
#: ``gh`` verbs that change nothing anyone reads (a local checkout, a rerun, a login).
GH_QUIET_VERBS = frozenset(
    {
        "checkout",
        "clone",
        "rerun",
        "cancel",
        "login",
        "logout",
        "refresh",
        "setup-git",
        "token",
        "switch",
    }
)
#: ``gh``'s own command groups; anything else is an alias or an extension.
GH_GROUPS = frozenset(
    {
        "issue",
        "pr",
        "api",
        "repo",
        "release",
        "gist",
        "auth",
        "run",
        "workflow",
        "label",
        "search",
        "browse",
        "status",
        "cache",
        "codespace",
        "config",
        "extension",
        "gpg-key",
        "secret",
        "ssh-key",
        "variable",
        "project",
        "ruleset",
        "org",
        "alias",
        "completion",
        "help",
        "version",
        "attestation",
    }
)
#: ``gh`` commands that are one word and read.
GH_SINGLE_COMMANDS = frozenset({"status", "help", "version", "browse", "completion"})
#: ``gh <group> <verb>`` commands that publish files or text the hook does not read.
GH_PUBLISHES = frozenset(
    {
        ("gist", "create"),
        ("gist", "edit"),
        ("release", "create"),
        ("release", "edit"),
        ("release", "upload"),
    }
)
#: The ``gh <group> <verb>`` commands that write text, and whether the first positional is
#: the issue or pull request (``True``) or nothing (``False``: it opens one).
GH_WRITES: Mapping[tuple[str, str], bool] = {
    ("issue", "comment"): True,
    ("issue", "create"): False,
    ("issue", "edit"): True,
    ("pr", "comment"): True,
    ("pr", "create"): False,
    ("pr", "edit"): True,
    ("pr", "review"): True,
}
#: ``gh <group> <verb>`` flags that take a value, so their value is not a positional.
_GH_VALUE_FLAGS = frozenset(
    {
        "-R",
        "--repo",
        "-b",
        "--body",
        "-F",
        "--body-file",
        "-t",
        "--title",
        "-a",
        "--assignee",
        "-l",
        "--label",
        "-m",
        "--milestone",
        "-p",
        "--project",
        "-B",
        "--base",
        "-H",
        "--head",
        "-r",
        "--reviewer",
        "--template",
        "-T",
        "--add-label",
        "--remove-label",
        "--add-assignee",
        "--remove-assignee",
        "--add-project",
        "--remove-project",
        "--add-reviewer",
        "--remove-reviewer",
        "-X",
        "--method",
        "-f",
        "--field",
        "--raw-field",
        "--input",
        "-H",
        "--header",
        "-q",
        "--jq",
        "--hostname",
        "--cache",
        "--notes",
        "--notes-file",
        "-n",
        "--recover",
    }
)
#: The REST paths ``gh api`` writes messages at; the ref is built from the groups.
_API_PATH_RE = re.compile(
    r"^/?repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?:"
    r"(?:issues|pulls)(?:/(?P<number>\d+))?(?:/(?:comments|reviews))?"
    r"|issues/comments/\d+|pulls/comments/\d+|pulls/\d+/reviews/\d+(?:/events)?"
    r"|discussions(?:/\d+(?:/comments(?:/\d+)?)?)?)/?$"
)
_GRAPHQL_MUTATION_RE = re.compile(r"\bmutation\b", re.IGNORECASE)
_ISSUE_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?:issues|pull)/(?P<number>\d+)"
)
_REPO_RE = re.compile(
    r"^(?:https?://github\.com/)?(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)
_REMOTE_RE = re.compile(
    r"github\.com[:/](?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)
_HEREDOC_RE = re.compile(
    r"<<(?P<dash>-?)[ \t]*(?P<quote>['\"]?)(?P<word>[A-Za-z_][\w-]*)(?P=quote)"
)
#: What in a body means the shell computes it: its value is not the text the hook read.
_SHELL_EXPANSION_RE = re.compile(r"\$[\w{(]|`")
_SHELL_QUERY_RE = re.compile(r"\$[{(]|`")
_SEPARATORS = frozenset(
    {"&&", "||", ";", "|", "&", "\n", ";;", "|&", "(", ")", "((", "))", ";&", ";;&"}
)
#: Words that open or continue a compound command: what follows is a command.
_KEYWORDS = frozenset(
    {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "fi", "done"}
)
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_]\w*(\[[^]]*\])?\+?=")
#: A ``gh`` command line, whatever names the program.
_GH_SHAPED_RE = re.compile(
    r"\b(?:issue|pr)\s+(?:comment|create|edit|review|close|reopen|merge)\b"
    r"|\bapi\s+(?:-\S+\s+)*(?:/?repos/|graphql\b)"
)
_HEREDOC_TOKEN = "\x00liaise-heredoc-{}\x00"
#: Redirections that move no text the hook would need: ``2>&1``, ``>/dev/null``.
_HARMLESS_REDIRECT_RE = re.compile(r"\d?>>?[ \t]*(?:&[12]\b|/dev/null\b)")
#: Short flags whose value may be attached (``-bTEXT``, ``-Rowner/repo``): read as such.
_ATTACHED_VALUE_FLAGS = frozenset("bFtR")
#: ``gh api``'s short flags whose value may be attached.
_API_ATTACHED_VALUE_FLAGS = frozenset("fFXHq")
#: Programs a gh read may be piped into: they print, and run nothing.
INERT_FILTERS = frozenset(
    {
        "jq",
        "head",
        "tail",
        "grep",
        "egrep",
        "fgrep",
        "wc",
        "sort",
        "uniq",
        "cut",
        "tr",
        "cat",
        "less",
        "column",
        "tee",
    }
)


@dataclass(frozen=True)
class Write:
    """One write the hook found: where it goes, and its text, or why it cannot be read.

    ``what`` names the command in words (``gh issue comment``). ``problem`` set means the
    hook answers ``ask`` with it, without vetting.
    """

    what: str
    ref: str = UNKNOWN_REF
    text: Optional[str] = None
    title: Optional[str] = None
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    problem: Optional[str] = None


@dataclass(frozen=True)
class Answer:
    """The hook's answer: ``decision`` (one of :data:`DECISIONS`) and the reasons for it."""

    decision: str
    reasons: tuple[str, ...] = ()
    records: tuple[Mapping[str, Any], ...] = ()
    #: How many writes could not be read or vetted, and so went to the operator unvetted.
    unread: int = 0

    @property
    def reason(self) -> str:
        return "liaise: " + "; ".join(self.reasons) if self.reasons else ""


# ---- reading a shell command ----


def _heredocs(command: str) -> tuple[str, dict[str, tuple[str, bool]], list[str]]:
    """``command`` with comments dropped, each heredoc body replaced by a token, and the bodies.

    A quote-aware scan: ``<<WORD`` counts only outside quotes (and ``<<<`` never), a
    ``#`` starts a comment only at the start of a word outside quotes, and a
    backslash-newline joins two lines, as the shell reads them. A body is
    ``(text, expands)``: ``expands`` when the delimiter is unquoted, so the shell
    substitutes ``$…`` and backticks in it. The third value lists every redirection
    outside quotes but ``2>&1``, ``>/dev/null`` and their kin: each is a place a body or a
    file can come from, or be written to, that the hook does not follow. Raises
    ``ValueError`` for an unterminated quote.
    """
    bodies: dict[str, tuple[str, bool]] = {}
    redirects: list[str] = []
    out: list[str] = []
    pending: list[tuple[str, str, bool, bool]] = []  # token, word, dash, expands
    quote: Optional[str] = None
    index, length = 0, len(command)
    while index < length:
        char = command[index]
        if quote == "'":
            out.append(char)
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            if command[index + 1] == "\n":  # a line continuation: the two lines are one
                index += 2
                continue
            out.append(command[index : index + 2])
            index += 2
            continue
        if quote == '"':
            out.append(char)
            if char == '"':
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "#" and (not out or out[-1][-1:] in " \t\n;&|()"):
            while index < length and command[index] != "\n":
                index += 1
            continue
        if command.startswith("<<", index) and not command.startswith("<<<", index):
            match = _HEREDOC_RE.match(command, index)
            if match is None:
                raise ValueError("a heredoc whose delimiter the hook cannot read")
            token = _HEREDOC_TOKEN.format(len(bodies) + len(pending))
            pending.append(
                (token, match["word"], bool(match["dash"]), not match["quote"])
            )
            out.append(f"<<{token}")
            index = match.end()
            continue
        if command.startswith("<<<", index):
            redirects.append("a here-string (<<<)")
            out.append("<<<")
            index += 3
            continue
        if char in "<>":
            harmless = _HARMLESS_REDIRECT_RE.match(command, index)
            if char == ">" and harmless is not None:
                if out and out[-1].isdigit():  # the 2 of 2>&1
                    out.pop()
                index = harmless.end()  # dropped: it moves no text the hook needs
                continue
            redirects.append(f"a redirection ({command[index : index + 12].strip()}…)")
        if char == "\n" and pending:
            out.append(char)
            index += 1
            for token, word, dash, expands in pending:
                body: list[str] = []
                while index < length:
                    end = command.find("\n", index)
                    end = length if end < 0 else end
                    line = command[index:end]
                    index = end + 1
                    stripped = line.lstrip("\t") if dash else line
                    if stripped == word:
                        break
                    body.append(stripped)
                bodies[token] = ("\n".join(body) + ("\n" if body else ""), expands)
            pending = []
            continue
        out.append(char)
        index += 1
    if quote is not None:
        raise ValueError("an unterminated quote")
    for token, _, _, _ in pending:  # a heredoc on the last line has no body
        bodies[token] = ("", False)
    return "".join(out), bodies, redirects


def _tokens(command: str) -> list[str]:
    """``command`` as shell words and separators; raises ``ValueError`` when it does not parse.

    A newline and the grouping parentheses are separators, as ``;`` is.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n()")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens: list[str] = []
    for token in lexer:
        tokens.append(token)
    return tokens


def _segments(tokens: Sequence[str]) -> list[tuple[list[str], Optional[str]]]:
    """The simple commands of ``tokens``, each with the separator before it."""
    segments: list[tuple[list[str], Optional[str]]] = []
    current: list[str] = []
    before: Optional[str] = None
    for token in tokens:
        # shlex joins neighbouring punctuation (``)\n``, ``;;``): any run of it separates.
        if token in _SEPARATORS or (token and set(token) <= set(";&|\n()")):
            if current:
                segments.append((current, before))
            current, before = [], token
        else:
            current.append(token)
    if current:
        segments.append((current, before))
    return segments


def _program(word: str) -> str:
    return os.path.basename(word)


def _mentions_a_writer(text: str) -> bool:
    """Whether ``text`` names ``gh`` or ``correspond``, however the shell's quoting spells it.

    Quotes and backslashes are dropped first (``"g"h`` and ``g\\h`` are ``gh``), and a
    ``gh``-shaped command line (``issue comment``, ``api repos/``) counts even without the
    program's name, which a ``$`` may have built.
    """
    plain = re.sub(r"[\"'\\]", "", text)
    return bool(
        re.search(r"(?<![\w.-])(gh|correspond)(?![\w.-])", plain)
        or _GH_SHAPED_RE.search(plain)
    )


# ---- reading one gh or correspond command ----


def _flag_values(args: Sequence[str], names: Iterable[str]) -> list[str]:
    """Every value given to any of ``names``, as ``--flag value`` or ``--flag=value``."""
    names = set(names)
    values = []
    index = 0
    while index < len(args):
        arg = args[index]
        name, eq, value = arg.partition("=")
        short = arg[:2]
        if eq and name in names and name.startswith("--"):
            values.append(value)
        elif arg in names:
            if index + 1 < len(args):
                values.append(args[index + 1])
            index += 1
        elif short in names and len(arg) > 2 and not arg.startswith("--"):
            values.append(
                arg[2:].removeprefix("=")
            )  # -bTEXT, -b=TEXT, as pflag reads them
        index += 1
    return values


def _positionals(args: Sequence[str]) -> list[str]:
    """The arguments that are neither a flag nor a flag's value."""
    out = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith("-") and arg != "-":
            if "=" not in arg and arg in _GH_VALUE_FLAGS:
                index += 1
        else:
            out.append(arg)
        index += 1
    return out


def _stdin_of(
    args: Sequence[str],
    bodies: Mapping[str, tuple[str, bool]],
    piped: Optional[tuple[str, bool]],
) -> Optional[tuple[str, bool]]:
    """What the command reads on stdin: its own heredoc, else what ``cat <<EOF |`` piped in."""
    for arg in args:
        if arg.startswith("<<") and arg[2:] in bodies:
            return bodies[arg[2:]]
    return piped


def _read_file(path: str, *, cwd: Optional[str]) -> Optional[str]:
    try:
        full = Path(path).expanduser()
        if not full.is_absolute() and cwd:
            full = Path(cwd) / full
        resolved = full.resolve()
        if (
            not resolved.is_file()
            or resolved.parts[1:2] in (("dev",), ("proc",))
            or resolved.stat().st_size > MAX_BODY_FILE_BYTES
        ):
            return None  # stdin, a pipe or a device the hook cannot read as the shell would
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _checked(
    text: Optional[str], *, expands: bool, source: str
) -> tuple[Optional[str], Optional[str]]:
    """``(text, problem)``: a body the shell would compute is a problem, not a text."""
    if text is None:
        return None, f"{NO_BODY} ({source})"
    if expands and _SHELL_EXPANSION_RE.search(text):
        return None, f"{NO_BODY}: the shell computes part of it ({source})"
    return text, None


def _repo_ref(
    args: Sequence[str],
    *,
    cwd: Optional[str],
    repo_of: Callable[[Optional[str]], Optional[str]],
) -> Optional[str]:
    """``owner/repo`` a gh command targets: ``-R``/``--repo``, else the checkout's."""
    given = _flag_values(args, ("-R", "--repo"))
    if given:
        match = _REPO_RE.match(given[-1].strip())
        return f"{match['owner']}/{match['repo']}" if match else None
    return repo_of(cwd)


def _gh_write(
    group: str,
    verb: str,
    args: Sequence[str],
    *,
    cwd: Optional[str],
    stdin: Optional[tuple[str, bool]],
    repo_of: Callable[[Optional[str]], Optional[str]],
) -> Write:
    what = f"gh {group} {verb}"
    repo = _repo_ref(args, cwd=cwd, repo_of=repo_of)
    positionals = _positionals(args)
    ref = UNKNOWN_REF
    if GH_WRITES[(group, verb)]:
        target = positionals[0] if positionals else None
        # The design's shorthand ``gh issue comment example/app 12`` too.
        if target and _REPO_RE.match(target) and "/" in target and len(positionals) > 1:
            repo, target = target, positionals[1]
        url = _ISSUE_URL_RE.match(target or "")
        if url:
            ref = f"github:{url['owner']}/{url['repo']}#{url['number']}"
        elif target and target.lstrip("#").isdigit() and repo:
            ref = f"github:{repo}#{int(target.lstrip('#'))}"
        elif (
            repo
            and target is None
            and verb in ("comment", "edit", "review")
            and group == "pr"
        ):
            ref = f"github:{repo}"  # the current branch's pull request, somewhere in the repo
    elif repo:
        ref = f"github:{repo}"
    titles = _flag_values(args, ("--title", "-t"))
    bodies = _flag_values(args, ("--body", "-b"))
    files = _flag_values(args, ("--body-file", "-F"))
    if not bodies and not files:
        if titles:
            text, problem = _checked(titles[-1], expands=False, source="--title")
            return Write(what, ref=ref, text=text, problem=problem)
        if verb in ("create", "edit", "review"):
            if (
                verb == "create"
                and "--fill" not in args
                and "--fill-first" not in args
                and "-f" not in args
            ):
                return Write(
                    what,
                    ref=ref,
                    problem=f"{NO_BODY}: {what} with no --body asks for one interactively",
                )
            return Write(
                what, ref=ref, text=""
            )  # nothing written but what the command derives
        return Write(
            what, ref=ref, problem=f"{NO_BODY}: {what} with no --body or --body-file"
        )
    if bodies:
        text, problem = bodies[-1], None
    elif files[-1] == "-":
        text, problem = _checked(
            None if stdin is None else stdin[0],
            expands=bool(stdin and stdin[1]),
            source="--body-file - with nothing the hook can see on stdin",
        )
    else:
        text, problem = _checked(
            _read_file(files[-1], cwd=cwd),
            expands=False,
            source=f"--body-file {files[-1]}",
        )
    if problem is None and bodies:
        text, problem = _checked(text, expands=True, source="--body")
    title = titles[-1] if titles else None
    if title is not None and _SHELL_EXPANSION_RE.search(title):
        problem = problem or f"{NO_BODY}: the shell computes the title"
    return Write(what, ref=ref, text=text, title=title, problem=problem)


def _api_fields(args: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """``gh api``'s ``-f``/``-F`` fields as ``{name: value}``, and its ``--input`` values."""
    fields: dict[str, str] = {}
    for value in _flag_values(args, ("-f", "--raw-field", "-F", "--field")):
        name, _, given = value.partition("=")
        fields[name] = given
    return fields, _flag_values(args, ("--input",))


def _gh_api(
    args: Sequence[str],
    *,
    cwd: Optional[str],
    stdin: Optional[tuple[str, bool]],
    repo_of: Callable[[Optional[str]], Optional[str]],
) -> Optional[Write]:
    what = "gh api"
    methods = [m.upper() for m in _flag_values(args, ("-X", "--method"))]
    fields, inputs = _api_fields(args)
    method = methods[-1] if methods else ("POST" if fields or inputs else "GET")
    if method in ("GET", "HEAD"):
        return None
    positionals = _positionals(args)
    endpoint = positionals[0] if positionals else ""
    if "{owner}" in endpoint or "{repo}" in endpoint:
        repo = repo_of(cwd)
        if repo:
            owner, name = repo.split("/", 1)
            endpoint = endpoint.replace("{owner}", owner).replace("{repo}", name)
    body: Optional[str] = None
    problem: Optional[str] = None
    query_text: Optional[str] = None
    for name in ("body", "query"):
        if name in fields:
            value = fields[name]
            if value.startswith("@"):
                path = value[1:]
                read = (
                    (stdin[0] if stdin else None)
                    if path == "-"
                    else _read_file(path, cwd=cwd)
                )
                expands = bool(stdin and stdin[1]) if path == "-" else False
                text, problem = _checked(
                    read, expands=expands, source=f"-F {name}=@{path}"
                )
            elif name == "query":
                # GraphQL's own variables are ``$name``; what the shell would compute is a
                # command substitution, ``${…}``, or a query that is one variable.
                computed = _SHELL_QUERY_RE.search(value) or re.fullmatch(
                    r"\s*\$\w+\s*", value
                )
                text, problem = (
                    (None, f"{NO_BODY}: the shell computes the query")
                    if computed
                    else (value, None)
                )
            else:
                text, problem = _checked(value, expands=True, source=f"-f {name}=…")
            if problem:
                break
            if name == "body":
                body = text
            elif endpoint.strip("/") == "graphql":
                if text is not None and not _GRAPHQL_MUTATION_RE.search(text):
                    return None  # a GraphQL query reads
                query_text = text
    if inputs and not problem:
        path = inputs[-1]
        raw = (
            (stdin[0] if stdin else None) if path == "-" else _read_file(path, cwd=cwd)
        )
        expands = bool(stdin and stdin[1]) if path == "-" else False
        raw, problem = _checked(raw, expands=expands, source=f"--input {path}")
        if raw is not None:
            try:
                data = json.loads(raw)
                if not isinstance(data, Mapping):
                    raise ValueError("not an object")
            except ValueError:
                problem = f"{NO_BODY}: --input {path} is not a JSON object"
            else:
                if endpoint.strip("/") == "graphql":
                    query = str(data.get("query", ""))
                    if not _GRAPHQL_MUTATION_RE.search(query):
                        return None
                    variables = data.get("variables") or {}
                    parts = (query, _graphql_body(variables))
                    body = "\n".join(p for p in parts if p) or None
                else:
                    body = "\n".join(_strings(data)) or body
    if endpoint.strip("/") == "graphql":
        if problem:
            return Write(f"{what} graphql", problem=problem)
        # Every text the mutation carries: its variables, and the query itself, which can
        # hold a literal body.
        texts = [v for k, v in fields.items() if k != "query"]
        for value in texts:
            if value.startswith("@") or _SHELL_EXPANSION_RE.search(value):
                return Write(
                    f"{what} graphql",
                    problem=f"{NO_BODY}: a variable the hook cannot read",
                )
        parts = [t for t in (query_text, body, *texts) if t]
        if not parts:
            return Write(
                f"{what} graphql",
                problem=f"{NO_BODY}: a GraphQL mutation whose text the hook cannot find",
            )
        return Write(
            f"{what} graphql", text="\n".join(dict.fromkeys(parts))
        )  # node ids: destination unknown
    match = _API_PATH_RE.match(endpoint)
    if match is None:
        return Write(
            f"{what} {method} {endpoint or '(no endpoint)'}",
            problem=(
                f"a {method} through gh api to an endpoint the hook does not know; the operator judges it"
            ),
        )
    ref = f"github:{match['owner']}/{match['repo']}"
    if match["number"]:
        ref += f"#{int(match['number'])}"
    if problem:
        return Write(f"{what} {method}", ref=ref, problem=problem)
    # Every text field goes out (a review's ``comments[][body]`` too), so all are vetted.
    texts = [body] if body else []
    for name, value in fields.items():
        if name in ("body", "query"):
            continue
        if value.startswith("@"):
            return Write(
                f"{what} {method}",
                ref=ref,
                problem=f"{NO_BODY}: -F {name}=@… is a file the hook does not read",
            )
        text, problem = _checked(value, expands=True, source=f"-f {name}=…")
        if problem:
            return Write(f"{what} {method}", ref=ref, problem=problem)
        texts.append(text)
    texts = [t for t in texts if t]
    if not texts:
        if method == "DELETE" or (not fields and not inputs):
            return None  # nothing written to anyone
        return Write(
            f"{what} {method}",
            ref=ref,
            problem=f"{NO_BODY}: no text field the hook can read",
        )
    return Write(f"{what} {method}", ref=ref, text="\n".join(texts))


def _strings(value: Any) -> list[str]:
    """Every string in a JSON value, depth first."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def _graphql_body(variables: Any) -> Optional[str]:
    if not isinstance(variables, Mapping):
        return None
    texts = _strings(variables)
    return "\n".join(texts) if texts else None


def _correspond(
    args: Sequence[str], *, stdin: Optional[tuple[str, bool]]
) -> Optional[Write]:
    words = [a for a in args if a != "--json"]  # correspond takes --json anywhere
    verbs = [w for w in words if not w.startswith("-")]
    if not verbs or verbs[0] not in ("send", "edit"):
        return None
    verb = verbs[0]
    rest = [w for i, w in enumerate(words) if i != words.index(verb)]
    if "--dry-run" in rest:
        return None  # a dry run writes nothing, and correspond runs its own check on it
    positionals = [a for a in _positionals(rest) if not a.startswith("<<")]
    for flag in (
        "--title",
        "--reply-to",
        "--priority",
        "--cc",
        "--bcc",
        "--idempotency-key",
    ):
        for value in _flag_values(rest, (flag,)):
            if value in positionals:
                positionals.remove(value)
    what = f"correspond {verb}"
    if not positionals:
        return Write(what, problem=f"{NO_BODY}: {what} with no reference")
    ref = positionals[0]
    raw = positionals[-1] if len(positionals) >= (2 if verb == "send" else 3) else None
    if raw is None:
        return Write(what, ref=ref, problem=f"{NO_BODY}: {what} with no text")
    if raw == "-":
        text, problem = _checked(
            None if stdin is None else stdin[0],
            expands=bool(stdin and stdin[1]),
            source="text on stdin",
        )
    else:
        text, problem = _checked(raw, expands=True, source="the text argument")
    cc = tuple(
        v
        for value in _flag_values(rest, ("--cc",))
        for v in value.split(",")
        if v.strip()
    )
    bcc = tuple(
        v
        for value in _flag_values(rest, ("--bcc",))
        for v in value.split(",")
        if v.strip()
    )
    titles = _flag_values(rest, ("--title",))
    return Write(
        what,
        ref=ref,
        text=text,
        title=titles[-1] if titles else None,
        cc=cc,
        bcc=bcc,
        problem=problem,
    )


def repo_of_checkout(cwd: Optional[str]) -> Optional[str]:
    """``owner/repo`` of the GitHub remote ``origin`` of the checkout at ``cwd``, or None."""
    try:
        url = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    match = _REMOTE_RE.search(url)
    return f"{match['owner']}/{match['repo']}" if match else None


def writes_in(
    command: str,
    *,
    cwd: Optional[str] = None,
    repo_of: Callable[[Optional[str]], Optional[str]] = repo_of_checkout,
) -> list[Write]:
    """Every ``gh`` or ``correspond`` write in the shell ``command``, each with its text or its problem.

    A command that names neither is no write at all. One that names either is read only
    when it is plain: a list of ``gh`` and ``correspond`` commands (joined by ``&&``,
    ``||``, ``;``, ``|`` or newlines), a ``cat <<EOF |`` feeding one of them, and a gh read
    piped into a filter (:data:`INERT_FILTERS`). Anything else in such a command (another
    program, a ``$``, a backtick, a subshell or a compound command, an assignment, a
    redirection, more than one heredoc) is one problem write, since the hook cannot tell
    what the shell will run, where, or with what text: ``ask``. The shell is not parsed
    beyond that, on purpose.
    """
    if not _mentions_a_writer(command):
        return []

    def unplain(why: str) -> list[Write]:
        return [Write("a shell command", problem=f"{why}: the operator judges it")]

    try:
        stripped, bodies, redirects = _heredocs(command)
        tokens = _tokens(stripped)
    except ValueError as error:
        return unplain(f"the hook could not parse the command ({error})")
    if "$" in stripped or "`" in stripped:
        return unplain(
            "the shell substitutes part of a command that names gh or correspond"
        )
    if redirects:
        return unplain(f"{redirects[0]} in a command that names gh or correspond")
    if len(bodies) > 1:
        return unplain("more than one heredoc in a command that names gh or correspond")
    punctuation = set(";&|\n()")
    if any(set(t) & set("()") for t in tokens if t and set(t) <= punctuation):
        return unplain("a subshell or a group in a command that names gh or correspond")
    writes: list[Write] = []
    piped: Optional[str] = (
        None  # the heredoc token ``cat <<EOF |`` hands the next command
    )
    read_by_a_writer: set[str] = set()
    previous: Optional[str] = None  # "read": a gh command that writes nothing
    for words, before in _segments(tokens):
        piping = bool(before) and "|" in before and "||" not in before
        from_pipe = piped if piping else None
        piped = None
        if not words:
            continue
        if words[0] in _KEYWORDS or _ASSIGNMENT_RE.match(words[0]):
            return unplain(f"a compound command or an assignment ({words[0][:12]})")
        program = _program(words[0])
        rest = words[1:]
        if program == "cat" and rest and all(w.startswith("<<") for w in rest):
            piped = next((w[2:] for w in rest if w[2:] in bodies), None)
            previous = "cat"
            continue
        if program not in ("gh", "correspond"):
            if piping and previous == "read" and program in INERT_FILTERS:
                continue  # ``gh issue list | jq …``
            return unplain(f"{program} runs beside gh or correspond")
        if previous == "cat" and not piping:
            return unplain("a heredoc that feeds nothing the hook reads")
        own = next(
            (w[2:] for w in rest if w.startswith("<<") and w[2:] in bodies), None
        )
        token = own or from_pipe
        own_stdin = bodies.get(token) if token else None
        if token:
            read_by_a_writer.add(token)
        problem = _unreadable_words(rest, api="api" in _positionals(rest)[:1])
        if problem:
            writes.append(Write(f"{program} …", problem=problem))
            previous = "write"
            continue
        if program == "gh":
            write = _gh(rest, cwd=cwd, stdin=own_stdin, repo_of=repo_of)
        else:
            write = _correspond(rest, stdin=own_stdin)
        previous = "read" if write is None else "write"
        if write is not None:
            writes.append(write)
    for token, (text, _) in bodies.items():
        if token not in read_by_a_writer and _mentions_a_writer(text):
            writes.append(
                Write(
                    "a heredoc",
                    problem=(
                        "a heredoc that names gh or correspond is run by another "
                        "program: the hook does not look through it"
                    ),
                )
            )
    return writes


def _unreadable_words(words: Sequence[str], *, api: bool = False) -> Optional[str]:
    """Why the words of a ``gh`` or ``correspond`` command cannot be read as written, or None.

    A cluster of short flags (``-sb``, ``-ab``) can hide a body flag, so only a flag that
    takes a value may carry one attached (``-bTEXT``).
    """
    attached = _API_ATTACHED_VALUE_FLAGS if api else _ATTACHED_VALUE_FLAGS
    for word in words:
        if (
            word.startswith("-")
            and not word.startswith("--")
            and len(word) > 2
            and word[1] not in attached
        ):
            return f"a cluster of short flags ({word[:6]}…) the hook does not unpack"
    return None


def _gh(
    args: Sequence[str],
    *,
    cwd: Optional[str],
    stdin: Optional[tuple[str, bool]],
    repo_of: Callable[[Optional[str]], Optional[str]],
) -> Optional[Write]:
    # gh's own global flags come before the group: -R is also a command flag, so keep it.
    words = [a for a in args if not a.startswith("<<")]
    groups = _positionals(words)
    if not groups:
        return None
    group = groups[0]
    if group == "api":
        index = words.index("api")
        return _gh_api(words[index + 1 :], cwd=cwd, stdin=stdin, repo_of=repo_of)
    verb = groups[1] if len(groups) > 1 else ""
    if (group, verb) in GH_WRITES:
        index = words.index(verb, words.index(group) + 1)
        return _gh_write(
            group,
            verb,
            words[: words.index(group)] + words[index + 1 :],
            cwd=cwd,
            stdin=stdin,
            repo_of=repo_of,
        )
    if group not in GH_GROUPS:
        return Write(
            f"gh {group}",
            problem=(
                f"gh {group} is an alias or an extension the hook cannot read; the "
                f"operator judges it"
            ),
        )
    if (not verb and group in GH_SINGLE_COMMANDS) or group == "search":
        return None
    if (group, verb) in GH_PUBLISHES:
        return Write(
            f"gh {group} {verb}",
            problem=f"gh {group} {verb} publishes what the hook cannot vet; the operator judges it",
        )
    if verb in GH_READ_VERBS:
        return None
    if verb not in GH_QUIET_VERBS:
        return Write(
            f"gh {group} {verb}".strip(),
            problem=(
                f"gh {group} {verb} changes something and is not a command the hook "
                f"knows; the operator judges it"
            ).replace("  ", " "),
        )
    carries = [
        w
        for w in words
        if w.partition("=")[0] in GH_BODY_FLAGS
        or (not w.startswith("--") and w[:2] in GH_BODY_FLAGS)
    ]
    if carries:
        return Write(
            f"gh {group} {verb}".strip(),
            problem=(
                f"gh {group} {verb} carries text ({carries[0].partition('=')[0]}) and is not a "
                f"command the hook knows; the operator judges it"
            ),
        )
    return None


# ---- reading an MCP tool call ----


def _mcp_write(tool_name: str, tool_input: Mapping[str, Any]) -> Optional[Write]:
    verb = tool_name.rsplit("__", 1)[-1]
    if verb not in ("send", "edit") or "correspond" not in tool_name:
        return None
    if tool_input.get("dry_run"):
        return None
    what = f"correspond MCP {verb}"
    ref = tool_input.get("ref")
    text = tool_input.get("text")
    if not isinstance(ref, str) or not ref.strip():
        return Write(what, problem=f"{NO_BODY}: {what} with no reference")
    if not isinstance(text, str):
        return Write(what, ref=ref, problem=f"{NO_BODY}: {what} with no text")

    def split(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            return tuple(v.strip() for v in value.split(",") if v.strip())
        if isinstance(value, (list, tuple)):
            return tuple(str(v) for v in value)
        return ()

    title = tool_input.get("title")
    return Write(
        what,
        ref=ref,
        text=text,
        title=title if isinstance(title, str) else None,
        cc=split(tool_input.get("cc")),
        bcc=split(tool_input.get("bcc")),
    )


# ---- the answers ----


def _most_restrictive(decisions: Iterable[str]) -> str:
    return max(decisions, key=DECISIONS.index, default=ALLOW)


def judge_writes(
    writes: Sequence[Write],
    *,
    vet_fn: Callable[..., Mapping[str, Any]] = vet,
    root: Union[str, Path, None] = None,
) -> Answer:
    """The hook's answer for ``writes``: the most restrictive of each one's, with every reason.

    A write with a problem is ``ask``. Each other is vetted with the provenance unknown; a
    route of ``send`` is ``allow``, ``draft`` (a ``delay`` included) ``ask``, ``block``
    ``deny``. Vetting that raises is ``ask`` with the error.
    """
    decisions, reasons, records = [], [], []
    for write in writes:
        if write.problem is not None:
            decisions.append(ASK)
            reasons.append(f"{write.what}: {write.problem}")
            continue
        try:
            record = vet_fn(
                write.text or "",
                ref=write.ref,
                cc=write.cc,
                bcc=write.bcc,
                title=write.title,
                tainted=None,
                root=root,
            )
        except Exception as error:  # a write the hook cannot vet goes to the operator
            decisions.append(ASK)
            reasons.append(
                f"{write.what}: liaise could not vet it ({type(error).__name__}: {error})"
            )
            continue
        decision = _FOR_ROUTE[immediate_route(record["flow"])]
        decisions.append(decision)
        records.append(record)
        if decision != ALLOW:
            why = "; ".join(record.get("reasons") or ()) or record["flow"]
            reasons.append(
                f"{write.what} to {record['ref']} ({record['flow']}; audience: {record['audience']}): {why}"
            )
    unread = len(writes) - len(records)
    return Answer(_most_restrictive(decisions), tuple(reasons), tuple(records), unread)


def writes_of(
    payload: Mapping[str, Any],
    *,
    repo_of: Callable[[Optional[str]], Optional[str]] = repo_of_checkout,
) -> list[Write]:
    """The writes a hook payload's tool call makes: a Bash command's, or a correspond MCP tool's."""
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping):
        if tool_name == "Bash" or "correspond" in tool_name:
            return [
                Write(
                    tool_name or "a tool",
                    problem="the hook could not read the tool's input",
                )
            ]
        return []
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return [Write("Bash", problem="the hook could not read the command")]
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
        return writes_in(command, cwd=cwd, repo_of=repo_of)
    if tool_name.startswith("mcp__"):
        write = _mcp_write(tool_name, tool_input)
        return [write] if write is not None else []
    return []


def pending_key(payload: Mapping[str, Any]) -> str:
    """The key a pending ask is kept under: the tool-use id, else a hash of the call.

    >>> pending_key({"tool_use_id": "toolu_1"})
    'toolu_1'
    """
    tool_use_id = payload.get("tool_use_id")
    if isinstance(tool_use_id, str) and tool_use_id.strip():
        return tool_use_id.strip()
    shape = {
        "session_id": payload.get("session_id"),
        "tool_name": payload.get("tool_name"),
        "tool_input": payload.get("tool_input"),
    }
    canonical = json.dumps(shape, sort_keys=True, default=str, separators=(",", ":"))
    return "call-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _pending_record(
    answer: Answer, payload: Mapping[str, Any], *, at: datetime
) -> dict:
    """What a pending ask keeps: never a text, a value found or a command."""
    return {
        "at": at.isoformat(),
        "session_id": payload.get("session_id"),
        "tool_name": payload.get("tool_name"),
        "decision": answer.decision,
        "writes": [
            {
                "ref": record.get("ref"),
                "subject": record.get("subject"),
                "flow": record.get("flow"),
                "rules": list(record.get("rules") or ()),
                "payload_hash": record.get("payload_hash"),
                "audience_hash": record.get("audience_hash"),
            }
            for record in answer.records
        ],
        "unread": answer.unread,
    }


def _prune(ledger: Ledger, *, now: datetime, ttl: timedelta = PENDING_TTL) -> None:
    for key, record in list(ledger.hook_pending()):
        try:
            at = datetime.fromisoformat(str(record.get("at")))
        except ValueError:
            at = None
        if at is None or now - at > ttl:
            ledger.pop_hook_pending(key)


def pre_tool_use(
    payload: Mapping[str, Any],
    *,
    ledger: Optional[Ledger] = None,
    vet_fn: Callable[..., Mapping[str, Any]] = vet,
    root: Union[str, Path, None] = None,
    repo_of: Callable[[Optional[str]], Optional[str]] = repo_of_checkout,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """The PreToolUse answer for ``payload``: Claude Code's JSON for ``ask`` or ``deny``, else None.

    An ``ask`` is kept as pending in ``ledger`` (when there is one), so the PostToolUse hook
    can tell the operator said yes. Nothing is printed for ``allow``.
    """
    answer = judge_writes(writes_of(payload, repo_of=repo_of), vet_fn=vet_fn, root=root)
    if answer.decision == ALLOW:
        return None
    if answer.decision == ASK and ledger is not None:
        at = now or datetime.now(timezone.utc)
        try:
            _prune(ledger, now=at)
            ledger.set_hook_pending(
                pending_key(payload), _pending_record(answer, payload, at=at)
            )
        except Exception:  # the ask stands whether or not it could be kept
            pass
    return {
        "hookSpecificOutput": {
            "hookEventName": PRE_TOOL_USE,
            "permissionDecision": answer.decision,
            "permissionDecisionReason": answer.reason,
        }
    }


def post_tool_use(
    payload: Mapping[str, Any],
    *,
    ledger: Optional[Ledger],
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Record the override when a tool call the hook answered ``ask`` ran; return what was recorded.

    The tool ran, so the operator said yes. The entry holds what the pending ask held (the
    refs, flows, rules and hashes), who and when, never the text.
    """
    if ledger is None:
        return None
    key = pending_key(payload)
    pending = ledger.pop_hook_pending(key)
    if pending is None:
        return None
    at = now or datetime.now(timezone.utc)
    response = payload.get("tool_response")
    succeeded = None
    if isinstance(response, Mapping):
        if "success" in response:
            succeeded = bool(response.get("success"))
        elif "interrupted" in response:
            succeeded = not response.get("interrupted")
    record = {
        **pending,
        "kind": "override",
        "by": "operator (Claude Code prompt)",
        "ran_at": at.isoformat(),
        "succeeded": succeeded,
    }
    ledger.add_override(key, record)
    return record


# ---- installing ----


def _read_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object, so it was left as it is")
    return data


def _is_ours(hook_entry: Any) -> bool:
    """Whether one hook command is liaise's."""
    return isinstance(hook_entry, Mapping) and str(
        hook_entry.get("command", "")
    ).strip().endswith(HOOK_COMMAND)


def _without_ours(entries: list) -> tuple[list, int]:
    """``entries`` with liaise's hook commands taken out of each, and how many were.

    An entry that also holds someone else's hook keeps it; an entry left with no hook is
    dropped.
    """
    kept, removed = [], 0
    for entry in entries:
        hooks = entry.get("hooks") if isinstance(entry, Mapping) else None
        if not isinstance(hooks, list):
            kept.append(entry)
            continue
        others = [h for h in hooks if not _is_ours(h)]
        removed += len(hooks) - len(others)
        if others:
            kept.append(
                {**entry, "hooks": others} if len(others) < len(hooks) else entry
            )
    return kept, removed


def _our_entry(command: str) -> dict:
    return {"matcher": HOOK_MATCHER, "hooks": [{"type": "command", "command": command}]}


def _settings_path(settings: Union[str, os.PathLike]) -> Path:
    """The file to read and write: a symlinked settings file is written through its link."""
    return Path(settings).expanduser().resolve()


def _write_settings(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".liaise-tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _events(data: Mapping[str, Any], path: Path) -> dict:
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path}: 'hooks' is not an object, so it was left as it is")
    for event in HOOK_EVENTS:
        if not isinstance(hooks.get(event, []), list):
            raise ValueError(
                f"{path}: hooks.{event} is not a list, so it was left as it is"
            )
    return hooks


def hook_command() -> str:
    """``liaise vet --hook`` with liaise's absolute path when it is on ``PATH``.

    The hook runs in a shell whose ``PATH`` may not hold liaise; a hook command that is not
    found fails without blocking, so every write would go through unvetted.
    """
    found = shutil.which("liaise")
    return f"{shlex.quote(found)} vet --hook" if found else HOOK_COMMAND


def install_hooks(
    settings: Union[str, os.PathLike] = DFLT_SETTINGS, *, command: Optional[str] = None
) -> str:
    """Write the PreToolUse and PostToolUse hooks into ``settings``, keeping every other hook and setting.

    Idempotent: liaise's own hook commands are replaced, never doubled, and a hook someone
    else put in the same entry stays. Returns what it did.
    """
    path = _settings_path(settings)
    data = _read_settings(path)
    hooks = _events(data, path)
    changed = False
    for event in HOOK_EVENTS:
        entries = list(hooks.get(event, []))
        mine = [
            e
            for e in entries
            if isinstance(e, Mapping) and any(map(_is_ours, e.get("hooks") or []))
        ]
        if (
            command is None
            and len(mine) == 1
            and _our_entry(mine[0]["hooks"][0].get("command", "")) == mine[0]
        ):
            continue  # installed, with the path the user has: keep it
        wanted = _our_entry(command or hook_command())
        if wanted in entries and _without_ours(entries)[1] == 1:
            continue
        kept, _ = _without_ours(entries)
        hooks[event] = [*kept, wanted]
        changed = True
    if not changed:
        return f"the liaise hooks are already installed in {path}"
    data["hooks"] = hooks
    _write_settings(path, data)
    return f"installed the liaise hooks ({', '.join(HOOK_EVENTS)}) in {path}"


def uninstall_hooks(settings: Union[str, os.PathLike] = DFLT_SETTINGS) -> str:
    """Remove liaise's hooks from ``settings``, keeping everything else."""
    path = _settings_path(settings)
    data = _read_settings(path)
    hooks = _events(data, path)
    removed = 0
    for event in HOOK_EVENTS:
        if event not in hooks:
            continue
        kept, count = _without_ours(hooks[event])
        removed += count
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if not removed:
        return f"no liaise hooks in {path}"
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    _write_settings(path, data)
    return f"removed the liaise hooks from {path}"


def hook_status(settings: Union[str, os.PathLike] = DFLT_SETTINGS) -> dict[str, str]:
    """Each hook event's state in ``settings``: ``installed``, ``outdated`` or ``missing``."""
    path = _settings_path(settings)
    hooks = _events(_read_settings(path), path)
    states = {}
    for event in HOOK_EVENTS:
        entries = hooks.get(event, [])
        mine = [
            e
            for e in entries
            if isinstance(e, Mapping) and any(map(_is_ours, e.get("hooks") or []))
        ]
        if not mine:
            states[event] = "missing"
        elif any(e.get("matcher") == HOOK_MATCHER for e in mine):
            states[event] = "installed"
        else:
            states[event] = "outdated (re-run liaise hook install)"
    return states


def run_hook(
    raw: str,
    *,
    ledger_store: Optional[Callable[[], MutableMapping[str, Any]]] = None,
    vet_fn: Callable[..., Mapping[str, Any]] = vet,
    root: Union[str, Path, None] = None,
    repo_of: Callable[[Optional[str]], Optional[str]] = repo_of_checkout,
    now: Optional[datetime] = None,
) -> str:
    """What ``liaise vet --hook`` prints for the hook JSON ``raw``: an answer, or nothing.

    Fails closed: input that is not a hook payload, or anything that goes wrong on the way,
    is ``ask`` with the reason on a PreToolUse event (and on any event the hook cannot
    name). A PostToolUse event never answers; its failure to record is silent.
    """

    def asked(reason: str) -> str:
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": PRE_TOOL_USE,
                    "permissionDecision": ASK,
                    "permissionDecisionReason": f"liaise: {reason}",
                }
            }
        )

    try:
        payload = json.loads(raw)
        if not isinstance(payload, Mapping):
            raise ValueError("the hook input is not a JSON object")
    except ValueError as error:
        return asked(f"the hook could not read its input ({error})")
    event = payload.get("hook_event_name")

    def ledger() -> Optional[Ledger]:
        if ledger_store is None:
            return None
        try:
            return Ledger(ledger_store())
        except Exception:
            return None

    if event == POST_TOOL_USE:
        try:
            post_tool_use(payload, ledger=ledger(), now=now)
        except Exception:
            pass
        return ""
    try:
        answer = pre_tool_use(
            payload, ledger=ledger(), vet_fn=vet_fn, root=root, repo_of=repo_of, now=now
        )
    except KeyboardInterrupt:
        raise
    except (
        BaseException
    ) as error:  # the hook never lets a failure through, SystemExit too
        return asked(f"the hook failed ({type(error).__name__}: {error})")
    return "" if answer is None else json.dumps(answer)
