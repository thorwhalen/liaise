"""The ``liaise`` command line (liaise 0.1).

One SSOT command tree, ``_dispatch_funcs``, of plain functions dispatched with ``cw``::

    liaise run [--once] [--dry-run] [--subject SLUG]
    liaise status
    liaise hold SCOPE [--mode MODE] [--reason TEXT]
    liaise unhold SCOPE
    liaise case list [--state STATE]
    liaise case show CASE_ID
    liaise case set-state CASE_ID STATE [--reason TEXT] [--dry-run]
    liaise case send-draft CASE_ID [INDEX] [--edit] [--dry-run]
    liaise case reject-draft CASE_ID [INDEX] --reason TEXT [--dry-run]
    liaise message send PERSON --ref REF (--text TEXT | --text-file FILE) [--title TITLE]
        [--purpose PURPOSE] [--dry-run]
    liaise message list [--state STATE]
    liaise message show MESSAGE_ID
    liaise message send-draft MESSAGE_ID [--edit] [--dry-run]
    liaise message reject-draft MESSAGE_ID --reason TEXT [--dry-run]
    liaise subject list
    liaise subject show SLUG
    liaise setup SUBJECT
    liaise migrate-config [--apply]
    liaise schedule install | uninstall | status

Every command takes ``--root``, the config root (``~/.config/liaise`` by default), and
returns the text it prints. The seams (the channel registry, the processor, the labeler,
the ledger store, the notifier, the sessions directory, the clock and the editor) are keyword
arguments with working defaults, hidden from the command line by ``_dispatch_config``:
tests fill them with fakes, and the command line never shows them.

An expected failure, such as a configuration that does not load, an unknown subject or a
bad hold scope, is one line on stderr and a nonzero exit (``cw.CommandError``), not a
traceback.

**Breaking change from 0.0.x.** ``partner list``, ``partner show`` and ``poll`` are gone.
``subject list``, ``subject show`` and ``run --once --dry-run`` replace them, and
``migrate-config`` derives the subject files from a 0.0.x configuration.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections import ChainMap
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple, Optional

import cw

from liaise import cases, holds, messages, migrate
from liaise.access import Resolver
from liaise.config import (
    DFLT_CONFIG_ROOT,
    ConfigError,
    GlobalConfig,
    load_global_config,
)
from liaise.detect import fingerprint_key, link_urls, visible
from liaise.github import GhCli, GitHub, GitHubError
from liaise.ledger import DFLT_LEDGER_SUBDIR, Ledger, default_ledger_store
from liaise.model import HOLD_MODES, require_one_of
from liaise.notify import notify
from liaise.processor import ClaudeHeadless
from liaise.projection import setup_labels
from liaise.release import DraftSentNotRecorded
from liaise.schedule import (
    DFLT_INTERVAL_MINUTES,
    install_schedule,
    schedule_status,
    uninstall_schedule,
)
from liaise.subjects import DFLT_SUBJECTS_SUBDIR, Subject, check_bindings, load_subjects
from liaise.tick import (
    DFLT_RUNS_SUBDIR,
    RunLockHeld,
    Triage,
    WorkspaceFactory,
    run_lock,
    run_lock_path,
    run_once,
    status_lines,
)

#: Seconds between two ticks of ``liaise run`` without ``--once``. The scheduled job
#: passes ``--once`` and leaves the interval to the scheduler.
DFLT_LOOP_SECONDS = 60
#: What ``liaise run`` without ``--once`` returns once it is interrupted.
STOPPED = "stopped"
#: How ``liaise subject show`` prints an empty or unset value.
NONE_SHOWN = cases.NONE_SHOWN
#: What a command that writes a case or a message says, changing nothing, while a tick
#: holds the run lock.
TICK_RUNNING = (
    "a liaise tick is running, so {what} was not {done}; try again shortly ({busy})"
)
#: The ``--text-file`` that reads a message's text from standard input.
STDIN_FILE_NAME = "-"
#: How ``--edit`` sets a message's title apart from its text: a first line, then a rule.
TITLE_LINE_PREFIX = "Title: "
TITLE_RULE = "---"
#: The environment variables that name the operator's editor, the first one set winning.
EDITOR_ENV_VARS = ("VISUAL", "EDITOR")
#: The editor ``liaise case send-draft --edit`` opens when no variable names one.
DFLT_EDITOR = "notepad" if sys.platform == "win32" else "vi"
#: The file ``--edit`` puts the draft in, inside a temporary directory of its own.
DRAFT_FILE_NAME = "draft.md"
#: What ``liaise case send-draft`` asks at the terminal, and the answers that send.
CONFIRM_PROMPT = "send it? [y/N] "
CONFIRM_ANSWERS = ("y", "yes")
#: The exit code of a draft command whose message the gate diverted: the message is held
#: for the operator, as ``liaise vet`` is planned to say (discussion 32, §5.8).
DIVERTED_EXIT_CODE = 2
#: Why ``send-draft`` sends nothing without a terminal to ask at.
NO_TERMINAL = (
    "a held message is sent only once you confirm it at a terminal, and there is no "
    "terminal here, so nothing was sent: run it in your own shell (--dry-run asks nothing)"
)


def _expected_errors(*kinds: type[Exception]) -> Callable[[Callable], Callable]:
    """Report the ``kinds`` a command raises as ``cw.CommandError``: one line, no traceback."""

    def decorate(command: Callable) -> Callable:
        @functools.wraps(command)
        def reported(*args: Any, **kwargs: Any) -> Any:
            try:
                return command(*args, **kwargs)
            except kinds as error:
                raise cw.CommandError(str(error)) from error

        return reported

    return decorate


def _root(root: Optional[str]) -> Path:
    return Path(root).expanduser() if root else DFLT_CONFIG_ROOT


def _ledger_store(
    global_config: GlobalConfig,
    store: Optional[MutableMapping[str, Any]],
    *,
    create: bool,
) -> MutableMapping[str, Any]:
    """``store`` when given, else the ledger under ``state_dir``.

    A command that writes nothing (``status``, a dry run) creates nothing either: before
    the ledger directory exists there is nothing to read, so it reads an empty store.
    """
    if store is not None:
        return store
    ledger_dir = Path(global_config.state_dir).expanduser() / DFLT_LEDGER_SUBDIR
    if not create and not ledger_dir.is_dir():
        return {}
    return default_ledger_store(global_config.state_dir)


def _subject_named(
    subjects: Mapping[str, Subject], slug: str, *, root: Path
) -> Subject:
    """``subjects[slug]``, or a :class:`ConfigError` naming the subjects there are."""
    if slug in subjects:
        return subjects[slug]
    known = ", ".join(sorted(subjects)) or "(none)"
    path = root / DFLT_SUBJECTS_SUBDIR / f"{slug}.toml"
    raise ConfigError(
        f"no subject {slug!r} is configured; the subjects are: {known}. A subject is a "
        f"file such as {path}."
    )


def _hold_run_lock(
    stack: ExitStack, global_config: GlobalConfig, what: str, *, done: str
) -> None:
    """Take the run lock into ``stack``, or refuse in one line while a tick holds it.

    A command that writes a case or a message holds the lock, so a tick cannot start
    meanwhile and write over it. It does not wait: the refusal says ``what`` was not
    ``done``.
    """
    lock_path = run_lock_path(Path(global_config.state_dir).expanduser())
    try:
        stack.enter_context(run_lock(lock_path))
    except RunLockHeld as busy:
        message = TICK_RUNNING.format(what=what, done=done, busy=busy)
        raise cw.CommandError(message) from busy


def edit_in_editor(text: str) -> str:
    """``text`` as the operator leaves it in their editor: ``$VISUAL``, ``$EDITOR``, else vi.

    The text goes in a file in a temporary directory only its owner can read, and the
    directory is removed once the editor exits, with any backup the editor left there. An
    editor that returns before the operator has saved, such as a GUI editor started
    without its wait flag (``code --wait``), hands the text back unchanged, and the
    confirmation says so. On Windows the command runs through the shell, which a ``.cmd``
    editor needs. Raises ``ValueError`` when the editor cannot be started or exits
    nonzero, so nothing is sent.
    """
    command = next(
        (os.environ[name] for name in EDITOR_ENV_VARS if os.environ.get(name)),
        DFLT_EDITOR,
    )
    with tempfile.TemporaryDirectory(prefix="liaise-draft-") as folder:
        path = Path(folder) / DRAFT_FILE_NAME
        path.write_text(text, encoding="utf-8")
        try:
            if os.name == "nt":
                edit = subprocess.run(f'{command} "{path}"', shell=True, check=False)
            else:
                edit = subprocess.run([*shlex.split(command), str(path)], check=False)
        except OSError as error:
            raise ValueError(
                f"the editor {command!r} could not be started ({error}); set $EDITOR"
            ) from error
        if edit.returncode != 0:
            raise ValueError(
                f"the editor {command!r} exited with {edit.returncode}, so nothing was "
                f"sent"
            )
        return path.read_text(encoding="utf-8")


def confirm_at_terminal(preview: str) -> bool:
    """Show ``preview`` and ask, at the operator's terminal, whether to send it; True for yes.

    A draft is released by a person at a terminal, not by whatever can run a command, so
    this raises ``ValueError`` when standard input is not a terminal: a processor run, or
    an agent's shell. It is a check on the ordinary way of running the command, not a
    sandbox: the hook of discussion 32, §5.8, is the defence for commands an agent writes.
    """
    if not sys.stdin.isatty():
        raise ValueError(NO_TERMINAL)
    print(preview, flush=True)
    try:
        answer = input(CONFIRM_PROMPT)
    except EOFError:
        return False
    return answer.strip().casefold() in CONFIRM_ANSWERS


# ---- the loop ----


@_expected_errors(ConfigError, RunLockHeld)
def run(
    *,
    root: Optional[str] = None,
    once: bool = False,
    dry_run: bool = False,
    subject: Optional[str] = None,
    registry: Optional[Mapping[str, Any]] = None,
    processor: Optional[Any] = None,
    labeler: Optional[GitHub] = None,
    store: Optional[MutableMapping[str, Any]] = None,
    notify_fn: Optional[Callable[..., Any]] = None,
    sessions_dir: Optional[str] = None,
    now: Optional[datetime] = None,
    resolver: Optional[Resolver] = None,
    workspace: Optional[WorkspaceFactory] = None,
    triage: Optional[Triage] = None,
) -> str:
    """One tick: take in what arrived, collect finished runs, start ready cases, deploy, label.

    Prints the tick's plan, a line per event, case, run and decision. ``--dry-run`` prints
    the same plan and changes nothing: nothing is sent, labelled, started, cancelled,
    deployed, locked or written. ``--subject`` ticks one subject alone. Without ``--once``
    or ``--dry-run``, it ticks every minute, printing each plan, until interrupted; the
    scheduled job (``liaise schedule install``) passes ``--once``. An inactive subject
    (``active = false``) is not ticked; ``--subject`` may name one with ``--dry-run``
    alone, to see what a tick would do.

    ``resolver``, ``workspace`` and ``triage`` are the tick's seams of those names (see
    :func:`liaise.tick.run_once`); None keeps the tick's own default.
    """
    seams = {
        name: value
        for name, value in (("resolver", resolver), ("workspace", workspace))
        if value is not None
    }
    config_root = _root(root)
    global_config = load_global_config(config_root)
    subjects = load_subjects(config_root)
    ledger_store = _ledger_store(global_config, store, create=not dry_run)
    if processor is None:
        # One processor for every tick of a loop: it reaps the runs it spawned, which a
        # new instance each tick could not, so a finished run never reads as a live one.
        state_dir = Path(global_config.state_dir).expanduser()
        processor = ClaudeHeadless(runs_dir=state_dir / DFLT_RUNS_SUBDIR)
    hint = (
        ()
        if subjects
        else (
            f"no subjects are configured: add {config_root / DFLT_SUBJECTS_SUBDIR}"
            "/<slug>.toml, or derive them from a 0.0.x configuration with "
            "liaise migrate-config",
        )
    )

    def tick() -> str:
        report = run_once(
            subjects,
            ledger_store,
            global_config=global_config,
            registry=registry,
            processor=processor,
            labeler=labeler,
            notify_fn=notify_fn,
            sessions_dir=sessions_dir,
            now=now,
            dry_run=dry_run,
            only=subject,
            triage=triage,
            **seams,
        )
        return "\n".join((*hint, *report.plan_lines))

    if once or dry_run:
        return tick()
    try:
        while True:
            try:
                print(tick(), flush=True)
            except RunLockHeld as busy:  # the scheduled tick is running: try the next
                print(busy, flush=True)
            time.sleep(DFLT_LOOP_SECONDS)
    except KeyboardInterrupt:
        return STOPPED


@_expected_errors(ConfigError)
def status(
    *,
    root: Optional[str] = None,
    store: Optional[MutableMapping[str, Any]] = None,
    now: Optional[datetime] = None,
) -> str:
    """What the ledger says, changing nothing: the last run, holds, runs, cases, what waits on you.

    That is the run stamps (``running``, ``interrupted`` or ``finished``), the holds, the
    runs in flight, each subject's cases by state and dispatches today, the unrouted
    queue, the drafts waiting for the operator, and the latest digest notes.
    """
    config_root = _root(root)
    global_config = load_global_config(config_root)
    lines = status_lines(
        load_subjects(config_root),
        _ledger_store(global_config, store, create=False),
        global_config=global_config,
        now=now,
    )
    return "\n".join(lines)


# ---- holds ----


@_expected_errors(ConfigError, ValueError)
def hold(
    scope: str,
    *,
    mode: str = holds.DFLT_HOLD_MODE,
    reason: str = "",
    root: Optional[str] = None,
    store: Optional[MutableMapping[str, Any]] = None,
) -> str:
    """Stop work in SCOPE until ``liaise unhold``.

    SCOPE is ``global``, ``processor``, ``effect:<kind>``, ``subject:<slug>``,
    ``person:<id>``, ``repo:<owner/repo>`` or ``checkout:<path>``. ``--mode block`` (the
    default) starts nothing new and keeps a finished run's messages as drafts; ``drain``
    starts nothing new and lets work already running finish and send; ``cancel`` also
    stops running runs, which stay resumable.
    """
    scope = holds.canonical_scope(scope)
    require_one_of(mode, HOLD_MODES, what="hold mode")
    global_config = load_global_config(_root(root))
    ledger = Ledger(_ledger_store(global_config, store, create=True))
    placed = holds.hold(ledger, scope, mode=mode, reason=reason)
    because = f": {placed.reason}" if placed.reason else ""
    return f"held {placed.scope} ({placed.mode}){because}"


@_expected_errors(ConfigError, ValueError)
def unhold(
    scope: str,
    *,
    root: Optional[str] = None,
    store: Optional[MutableMapping[str, Any]] = None,
) -> str:
    """Lift the hold on SCOPE, whoever set it."""
    scope = holds.canonical_scope(scope)
    global_config = load_global_config(_root(root))
    ledger = Ledger(_ledger_store(global_config, store, create=False))
    if holds.unhold(ledger, scope):
        return f"lifted the hold on {scope}"
    return f"no hold on {scope}"


# ---- subjects ----


@_expected_errors(ConfigError)
def subject_list(*, root: Optional[str] = None) -> str:
    """Every configured subject with its bindings, flagging an inactive one and any binding that could never match."""
    config_root = _root(root)
    subjects = load_subjects(config_root)
    if not subjects:
        return f"(no subjects under {config_root / DFLT_SUBJECTS_SUBDIR})"
    lines = []
    for slug, subject in subjects.items():
        problems = check_bindings(subject)
        flag = (
            f"  [{len(problems)} binding problem(s): liaise subject show {slug}]"
            if problems
            else ""
        )
        inert = "" if subject.active else "  [inactive: active = false]"
        lines.append(f"{slug}\t{', '.join(subject.bindings)}{inert}{flag}")
    return "\n".join(lines)


@_expected_errors(ConfigError)
def subject_show(slug: str, *, root: Optional[str] = None) -> str:
    """The subject SLUG as liaise reads it, every default applied, and its binding problems."""

    def field_lines(fields: Mapping[str, Any], *, prefix: str = "") -> list[str]:
        lines = []
        for key, value in fields.items():
            name = f"{prefix}{key}"
            if isinstance(value, Mapping) and value:
                lines += field_lines(value, prefix=f"{name}.")
            elif isinstance(value, (list, tuple)):
                lines.append(f"  {name}: {', '.join(map(str, value)) or NONE_SHOWN}")
            else:
                shown = NONE_SHOWN if value is None or value in ("", {}) else value
                lines.append(f"  {name}: {shown}")
        return lines

    config_root = _root(root)
    subject = _subject_named(load_subjects(config_root), slug, root=config_root)
    fields = dataclasses.asdict(subject)
    del fields["slug"]
    problems = check_bindings(subject)
    return "\n".join(
        [
            f"subject: {slug}",
            *field_lines(fields),
            f"binding problems: {len(problems) or 'none'}",
            *(f"  {problem}" for problem in problems),
        ]
    )


@_expected_errors(ConfigError, GitHubError)
def setup(
    subject: str, *, root: Optional[str] = None, labeler: Optional[GitHub] = None
) -> str:
    """Create SUBJECT's labels in each GitHub repository it binds. Idempotent.

    Those are its claim labels and one ``<label_prefix><state>`` label per case state. An
    inactive subject (``active = false``) is refused, since creating labels changes its
    repositories.
    """
    config_root = _root(root)
    found = _subject_named(load_subjects(config_root), subject, root=config_root)
    if not found.active:
        raise ConfigError(
            f"subject {subject!r} is inactive (active = false), so liaise setup creates "
            f"no labels in its repositories; set active = true in its file first"
        )
    return "\n".join(setup_labels(labeler if labeler is not None else GhCli(), found))


@_expected_errors(ConfigError)
def migrate_config(*, root: Optional[str] = None, apply: bool = False) -> str:
    """Derive 0.1 subject files from a 0.0.x configuration, and print the plan.

    Writes nothing without ``--apply``, which creates each missing
    ``subjects/<slug>.toml`` and never overwrites one. Nothing else under the config root
    is touched.
    """
    return "\n".join(migrate.migrate_config(_root(root), apply=apply).lines())


# ---- the scheduled job ----


@_expected_errors(ConfigError)
def schedule_install(
    *,
    root: Optional[str] = None,
    interval_minutes: int = DFLT_INTERVAL_MINUTES,
    extra_env_vars: Optional[Sequence[str]] = None,
) -> str:
    """Install the scheduled `liaise run --once` job (launchd on macOS, systemd on Linux).

    `extra_env_vars`: names of additional environment variables (beyond `PATH`, `HOME`,
    and the configured ntfy topic variable) the job needs snapshotted into its
    environment, such as one a deploy command reads.
    """
    global_config = load_global_config(_root(root))
    path = install_schedule(
        root=root,
        interval_minutes=interval_minutes,
        ntfy_topic_env=global_config.notify.ntfy_topic_env,
        extra_env_vars=extra_env_vars or (),
    )
    return f"installed: {path}"


def schedule_uninstall() -> str:
    """Remove the scheduled job. Idempotent."""
    return uninstall_schedule()


def schedule_status_cmd() -> str:
    """Whether the scheduled job is installed."""
    return schedule_status()


# ---- cases ----


@_expected_errors(ConfigError, ValueError)
def case_list(
    *,
    state: Optional[str] = None,
    root: Optional[str] = None,
    store: Optional[MutableMapping[str, Any]] = None,
) -> str:
    """Every case in the ledger, a line each: its id, its state and its conversations.

    ``--state`` lists only the cases in that state, such as ``needs-owner``, what waits
    on you. It changes nothing.
    """
    global_config = load_global_config(_root(root))
    ledger_store = _ledger_store(global_config, store, create=False)
    return "\n".join(cases.case_lines(ledger_store, state=state))


@_expected_errors(ConfigError, ValueError)
def case_show(
    case_id: str,
    *,
    root: Optional[str] = None,
    store: Optional[MutableMapping[str, Any]] = None,
) -> str:
    """CASE_ID as the ledger holds it: what a notification from liaise leaves out.

    Its state, the reason of its last escalation, its last failed deploy with the command's
    output, each draft waiting for you with its text, and its latest entries. A
    notification names the case and points here: nothing a case holds goes to the
    notification service. It changes nothing.
    """
    global_config = load_global_config(_root(root))
    ledger_store = _ledger_store(global_config, store, create=False)
    return "\n".join(cases.case_show_lines(ledger_store, case_id))


@_expected_errors(ConfigError, ValueError)
def case_set_state(
    case_id: str,
    state: str,
    *,
    reason: str = "",
    dry_run: bool = False,
    root: Optional[str] = None,
    store: Optional[MutableMapping[str, Any]] = None,
    now: Optional[datetime] = None,
) -> str:
    """Move CASE_ID to STATE, as you: how a case in needs-owner, or deployed, moves on.

    STATE is a case state other than ``working``, which only a run makes true. ``intake``
    has the tick start the case again once it is ready, resuming its session. The move is
    recorded on the case, with ``--reason``. The case's GitHub label follows on the next
    tick: a label is a projection of the ledger, so relabelling the issue by hand is
    overwritten. ``--dry-run`` says what would change, and changes nothing.

    The move holds the run lock, so a tick cannot start meanwhile and overwrite it. It does
    not wait for one: while a tick is running, it refuses in one line and changes nothing.
    A dry run takes no lock.
    """
    global_config = load_global_config(_root(root))
    ledger_store = _ledger_store(global_config, store, create=not dry_run)
    ledger = Ledger(ChainMap({}, ledger_store) if dry_run else ledger_store)
    with ExitStack() as between_ticks:
        if not dry_run:
            _hold_run_lock(between_ticks, global_config, case_id, done="moved")
        before = ledger.get_case(case_id)
        moved = cases.set_case_state(ledger, case_id, state, reason=reason, now=now)
    if before is not None and before.state == moved.state:
        return f"{case_id} is already {moved.state}"
    verb = "would move" if dry_run else "moved"
    return (
        f"{verb} {case_id} from {before.state} to {moved.state}; its labels follow on "
        f"the next tick"
    )


def _one_index(case_id: str, index: Sequence[int]) -> Optional[int]:
    """The one INDEX a draft command was given, or None without one. Raises ``ValueError`` for more.

    INDEX is ``*index`` in the signature, since cw makes a parameter with a default an
    option, and the command line spells it ``[INDEX]``.
    """
    if len(index) > 1:
        given = ", ".join(map(str, index))
        raise ValueError(f"name one draft of {case_id} at a time, not {given}")
    return index[0] if index else None


class _Held(NamedTuple):
    """A held message as a release command names it."""

    #: How its lines and refusals name it: ``draft [0] of example-app-1``.
    label: str
    #: Where it stays when it is not sent: ``on the case``, ``held``.
    stays: str
    #: The command that edits and sends it again.
    command: str
    #: What a refusal for a running tick names.
    owner: str


def _not_sent(held: _Held, release: Any, *, dry_run: bool) -> cw.CommandError:
    """The refusal for a held message the gate diverted or its channel refused.

    A divert exits :data:`DIVERTED_EXIT_CODE`, and a refusal cw's usual error code.
    """
    attempt = release.attempt
    decision = attempt.decision
    diverted = decision.send is None
    why = (
        f"diverted by {decision.diverted_by} ({decision.flow}): {decision.diverted}"
        if diverted
        else f"send failed: {attempt.failure}"
    )
    kept = (
        ""
        if dry_run
        else f". It stays {held.stays} with that reason; edit it with {held.command} --edit"
    )
    verb = "would not be sent" if dry_run else "was not sent"
    notes = [f"  note: {note}" for note in decision.notes]
    message = "\n".join([f"{held.label} {verb}: {why}{kept}", *notes])
    return cw.CommandError(
        message, **({"code": DIVERTED_EXIT_CODE} if diverted else {})
    )


def _key_for(global_config: GlobalConfig, *, dry_run: bool) -> Callable[[], bytes]:
    """The fingerprint key in ``global_config``'s state directory; a dry run creates none."""
    return functools.partial(
        fingerprint_key, global_config.state_dir, create=not dry_run
    )


def _preview(held: _Held, release: Any, *, edit: bool, then: Sequence[str]) -> str:
    """What the operator reads before confirming (discussion §5.7).

    Where it goes and who can read it there; what the gate holds it back for, which the
    operator's answer releases it past; the exact text, invisible characters made visible;
    and every link in full.
    """
    outbound = release.attempt.outbound
    decision = release.attempt.decision
    lines = [
        f"{held.label}: {outbound.purpose} to {outbound.recipient} on {outbound.ref}",
        f"audience: {decision.audience_words or cases.AUDIENCE_UNKNOWN}",
    ]
    if decision.settled:
        lines.append(
            f"gate: sends once you release it past {len(decision.settled)} concern(s):"
        )
        lines += [f"  [{c.flow}] {c.rule}: {c.text}" for c in decision.settled]
    else:
        lines.append(f"gate: passed ({release.filters} filters)")
    lines += [f"  note: {note}" for note in decision.notes]
    if edit and not release.edited:
        lines.append("your edit changed nothing: this is the draft as it was")
    lines += then
    if outbound.title:
        lines.append(f"title: {visible(outbound.title)}")
    lines += [
        "--- the message, as it would be sent (invisible characters as <U+XXXX>) ---",
        visible(outbound.text),
        "---",
    ]
    urls = link_urls(outbound.text)
    if urls:
        lines += ["links, in full:", *(f"  {visible(url)}" for url in urls)]
    return "\n".join(lines)


def _release_after_confirmation(
    release: Callable[..., Any],
    *,
    first: Mapping[str, Any],
    bind: Callable[[Any], Mapping[str, Any]],
    held: Callable[[Any], _Held],
    then: Callable[[Any], Sequence[str]],
    ledger_store: MutableMapping[str, Any],
    global_config: GlobalConfig,
    dry_run: bool,
    edit: bool,
    confirm: Callable[[str], bool],
) -> tuple[Any, bool]:
    """Judge a held message, show the operator the verdict and the text, send it once confirmed.

    ``release(ledger, **kwargs)`` is :func:`liaise.cases.send_draft` or
    :func:`liaise.messages.send_held_message`, with all but the ledger bound. It is judged
    first, as a dry run on an overlay of the ledger, with ``first``. A message the gate
    diverts, or its channel refuses, is never shown: outside a dry run its reason is
    recorded, sending nothing, and the command fails. One the gate passes is shown to
    ``confirm`` and, once confirmed, sent under the run lock, bound by ``bind`` to what the
    operator was shown. This is the one path both ``send-draft`` commands take.

    Returns ``(release, True)`` for the release made, or the judgement in a dry run, and
    ``(judgement, False)`` when the operator declined.
    """
    judged = release(Ledger(ChainMap({}, ledger_store)), dry_run=True, **first)
    names = held(judged)
    if not judged.attempt.sent:
        if dry_run:
            raise _not_sent(names, judged, dry_run=True)
        with ExitStack() as between_ticks:  # record why, sending nothing
            _hold_run_lock(between_ticks, global_config, names.owner, done="changed")
            recorded = release(Ledger(ledger_store), send=False, **bind(judged))
        if recorded.attempt.sent:
            raise cw.CommandError(
                f"the gate's verdict on {names.label} changed while it was judged, so "
                f"nothing was sent; run the command again"
            )
        raise _not_sent(names, recorded, dry_run=False)
    if dry_run:
        return judged, True
    if not confirm(_preview(names, judged, edit=edit, then=then(judged))):
        return judged, False
    with ExitStack() as between_ticks:
        _hold_run_lock(between_ticks, global_config, names.owner, done="sent")
        released = release(Ledger(ledger_store), **bind(judged))
    if not released.attempt.sent:
        raise _not_sent(names, released, dry_run=False)
    return released, True


@_expected_errors(ConfigError, ValueError, DraftSentNotRecorded)
def case_send_draft(
    case_id: str,
    *index: int,
    edit: bool = False,
    justification: str = "",
    dry_run: bool = False,
    root: Optional[str] = None,
    registry: Optional[Mapping[str, Any]] = None,
    store: Optional[MutableMapping[str, Any]] = None,
    now: Optional[datetime] = None,
    editor: Optional[Callable[[str], str]] = None,
    confirm: Optional[Callable[[str], bool]] = None,
) -> str:
    """Send a draft you approved: CASE_ID's draft INDEX, or its only one, through the gate.

    ``liaise case show`` numbers the drafts. The gate judges the text again, every filter of
    it, against the audience its channel reports now. ``--edit`` opens the text in
    ``$VISUAL`` or ``$EDITOR`` first, and the gate judges what you saved.

    It then shows you where the message goes and who can read it there, what the gate holds
    it back for, and the message exactly as it would be sent, and sends it only once you
    answer ``y`` at a terminal. Your answer is an approval bound to that text and that
    audience, recorded with ``--justification``: it releases the message past what it
    showed you, never past a refusal, and if the text or the audience changes before it
    goes out, nothing is sent and you see the new verdict. Without a terminal, as in an
    agent's shell or a processor run, it sends nothing.

    Once sent, the draft leaves the case and the send is recorded as yours. When no draft
    is left, a case in needs-owner moves on as a sent message moves it: an ask, a reply or
    a proposal, to needs-partner. A message the gate diverts is not sent: the draft stays
    on the case with the reason, and the command exits 2. A message its channel refuses
    stays the same way, and exits 1. ``--dry-run`` judges and plans, asks nothing, and
    records nothing.

    It holds the run lock, as ``set-state`` does, and refuses while a tick runs. It also
    refuses while a hold keeps the case's messages waiting, while a run of the case is in
    flight, and for a delivery message whose delivery a hold kept from running.
    """
    config_root = _root(root)
    global_config = load_global_config(config_root)
    ledger_store = _ledger_store(global_config, store, create=not dry_run)
    draft_index, text, opened = _one_index(case_id, index), None, None
    if edit:  # before the lock: an editor can stay open far longer than a tick waits
        ledger = Ledger(ledger_store)
        draft_index, opened = cases.find_draft(ledger, case_id, index=draft_index)
        text = (editor or edit_in_editor)(opened.get("text") or "")

    def moves(judged: cases.DraftRelease) -> list[str]:
        if not judged.moved:
            return []
        return [f"then {case_id} moves from {judged.moved[0]} to {judged.moved[1]}"]

    done, confirmed = _release_after_confirmation(
        functools.partial(
            cases.send_draft,
            subjects=load_subjects(config_root),
            case_id=case_id,
            text=text,
            by=cases.OPERATOR_ACTOR,
            now=now,
            registry=registry,
            justification=justification,
            fingerprint_key=_key_for(global_config, dry_run=dry_run),
        ),
        first=dict(index=draft_index, seen=opened),
        bind=lambda judged: dict(
            index=judged.index, seen=judged.draft, approval=judged.approval
        ),
        held=lambda judged: _Held(
            label=f"draft [{judged.index}] of {case_id}",
            stays="on the case",
            command=f"liaise case send-draft {case_id} {judged.index}",
            owner=case_id,
        ),
        then=moves,
        ledger_store=ledger_store,
        global_config=global_config,
        dry_run=dry_run,
        edit=edit,
        confirm=confirm or confirm_at_terminal,
    )
    label = f"draft [{done.index}] of {case_id}"
    if not confirmed:
        return f"nothing sent: {label} stays on the case"
    url = getattr(done.attempt.result, "url", None)
    head = (
        f"{'would send' if dry_run else 'sent'} {label} on {done.draft.get('ref')} "
        f"(gate: passed, {done.filters} filters)"
    )
    notes = [f"  note: {note}" for note in done.attempt.decision.notes]
    lines = [head + (f": {url}" if url and not dry_run else ""), *notes]
    if done.moved:
        before, after = done.moved
        verb = "would move" if dry_run else "moved"
        lines.append(
            f"{verb} {case_id} from {before} to {after}; its labels follow on the next "
            f"tick"
        )
    else:
        lines.append(f"{case_id} stays {done.case.state}")
    if done.case.drafts:
        lines.append(f"drafts left on {case_id}: {len(done.case.drafts)}")
    return "\n".join(lines)


@_expected_errors(ConfigError, ValueError)
def case_reject_draft(
    case_id: str,
    *index: int,
    reason: str = "",
    dry_run: bool = False,
    root: Optional[str] = None,
    store: Optional[MutableMapping[str, Any]] = None,
    now: Optional[datetime] = None,
) -> str:
    """Decline CASE_ID's draft INDEX, or its only one, recording ``--reason``.

    Nothing is sent. The draft leaves the case, and your refusal is recorded on the case with
    its reason and the draft's text. The case's state stays as it is: move it on with
    ``liaise case set-state``. It holds the run lock, as ``set-state`` does. ``--dry-run``
    changes nothing.
    """
    global_config = load_global_config(_root(root))
    ledger_store = _ledger_store(global_config, store, create=not dry_run)
    ledger = Ledger(ChainMap({}, ledger_store) if dry_run else ledger_store)
    with ExitStack() as between_ticks:
        if not dry_run:
            _hold_run_lock(between_ticks, global_config, case_id, done="changed")
        rejection = cases.reject_draft(
            ledger,
            case_id,
            index=_one_index(case_id, index),
            reason=reason,
            now=now,
            dry_run=dry_run,
        )
    draft = rejection.draft
    to = draft.get("ref") or draft.get("recipient")
    verb = "would reject" if dry_run else "rejected"
    return (
        f"{verb} draft [{rejection.index}] of {case_id} ({draft.get('outcome')} to {to}): "
        f"{reason.strip()}\n{case_id} stays {rejection.case.state}; move it on with "
        f"liaise case set-state {case_id} STATE"
    )


# ---- messages outside a case ----


def _edit_title_and_text(
    draft: Mapping[str, Any], editor: Callable[[str], str]
) -> tuple[Optional[str], str]:
    """The title and text the operator leaves in ``editor``; a draft with no title edits its text.

    A titled draft opens as ``Title: <title>``, a ``---`` line, then its text, so a title
    the gate diverted can be fixed as its text can. Raises ``ValueError``, sending nothing,
    when the first two lines are not left in that shape.
    """
    text = draft.get("text") or ""
    if not draft.get("title"):
        return None, editor(text)
    edited = editor(f"{TITLE_LINE_PREFIX}{draft['title']}\n{TITLE_RULE}\n{text}")
    head, _, rest = edited.partition("\n")
    rule, _, body = rest.partition("\n")
    if not head.startswith(TITLE_LINE_PREFIX) or rule.strip() != TITLE_RULE:
        raise ValueError(
            f"the edit must keep {TITLE_LINE_PREFIX!r} and the title on its first line, and "
            f"{TITLE_RULE} on its second, so nothing was sent"
        )
    return head[len(TITLE_LINE_PREFIX) :], body


def _message_text(text: str, text_file: str) -> str:
    """The message's text: ``--text``, or the file ``--text-file`` names (``-``: standard input).

    Raises ``ValueError`` unless exactly one is given, and for a file that cannot be read.
    """
    if bool(text) == bool(text_file):
        raise ValueError(
            "give the message's text with exactly one of --text and --text-file "
            f"(--text-file {STDIN_FILE_NAME} reads standard input)"
        )
    if text:
        return text
    if text_file == STDIN_FILE_NAME:
        return sys.stdin.read()
    try:
        return Path(text_file).expanduser().read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"cannot read --text-file {text_file}: {error}") from error


@_expected_errors(ConfigError, ValueError, DraftSentNotRecorded)
def message_send(
    recipient: str,
    *,
    ref: str = "",
    text: str = "",
    text_file: str = "",
    title: str = "",
    purpose: str = messages.DFLT_MESSAGE_PURPOSE,
    dry_run: bool = False,
    root: Optional[str] = None,
    registry: Optional[Mapping[str, Any]] = None,
    store: Optional[MutableMapping[str, Any]] = None,
    now: Optional[datetime] = None,
    notify_fn: Optional[Callable[..., Any]] = None,
) -> str:
    """Send a message to PERSON outside any case, through the gate, or hold it for the operator.

    ``--ref`` is the conversation it goes to, and a subject must bind it: an issue
    (``github:example/app#12``), or a repository with ``--title`` to open an issue. That
    subject's policy judges it, through the filters every message passes: the hold on a
    message outside a case, the outbound policy (of the title too), the writing card,
    deslop and the mention. The text is
    ``--text`` or ``--text-file`` (``-`` reads standard input). ``--purpose`` is ``ask``,
    the default, ``reply`` or ``propose``.

    In 0.1 the message is held for the operator: its sender chose where it goes, and only
    the operator's release lets such a message out. A hold on the subject, the person or
    the repository keeps it too. It is recorded with its reason, the operator is told a
    message waits (never what it says), and the command exits 2, or 1 when its channel
    refused it. The operator sends it with ``liaise message send-draft``, and the gate
    judges it again then. Nothing opens a case or sets a label. ``--dry-run`` judges and
    plans, and records and tells nothing.
    """
    if not ref:
        raise ValueError(
            "a message needs --ref, the conversation it goes to: an issue such as "
            "github:example/app#12, or a repository with --title to open an issue"
        )
    body = _message_text(text, text_file)
    config_root = _root(root)
    global_config = load_global_config(config_root)
    ledger_store = _ledger_store(global_config, store, create=not dry_run)
    topic_env = global_config.notify.ntfy_topic_env
    result = messages.send_message(
        Ledger(ledger_store),
        load_subjects(config_root),
        recipient,
        ref=ref,
        text=body,
        title=title or None,
        purpose=purpose,
        now=now,
        registry=registry,
        notify_fn=notify_fn or functools.partial(notify, topic_env=topic_env),
        dry_run=dry_run,
        fingerprint_key=_key_for(global_config, dry_run=dry_run),
    )
    message, attempt = result.message, result.attempt
    notes = [f"  note: {note}" for note in (attempt.decision.notes if attempt else ())]
    if result.sent:
        url = getattr(attempt.result, "url", None)
        verb = "would send a message" if dry_run else f"sent message {message.id}"
        head = (
            f"{verb} to {recipient} on {ref} (gate: passed, {result.filters} filters)"
        )
        return "\n".join([head + (f": {url}" if url and not dry_run else ""), *notes])
    if result.hold is not None:
        why, held_back = f"the hold on {result.hold.scope} ({result.hold.mode})", True
    elif attempt.decision.send is None:
        decision = attempt.decision
        why, held_back = (
            f"diverted by {decision.diverted_by} ({decision.flow}): {decision.diverted}",
            True,
        )
    else:
        why, held_back = f"send failed: {attempt.failure}", False
    head = (
        f"the message to {recipient} on {ref} would be held: {why}"
        if dry_run
        else f"the message to {recipient} on {ref} is held as {message.id}: {why}. "
        f"liaise status lists it for the operator, who sends it with liaise message "
        f"send-draft {message.id}"
    )
    code = {"code": DIVERTED_EXIT_CODE} if held_back else {}
    raise cw.CommandError("\n".join([head, *notes]), **code)


@_expected_errors(ConfigError, ValueError)
def message_list(
    *,
    state: Optional[str] = None,
    root: Optional[str] = None,
    store: Optional[MutableMapping[str, Any]] = None,
) -> str:
    """Every message sent or held outside a case, a line each: id, state, and where it goes.

    ``--state held`` lists what waits on you. It changes nothing.
    """
    global_config = load_global_config(_root(root))
    ledger_store = _ledger_store(global_config, store, create=False)
    return "\n".join(messages.message_lines(ledger_store, state=state))


@_expected_errors(ConfigError, ValueError)
def message_show(
    message_id: str,
    *,
    root: Optional[str] = None,
    store: Optional[MutableMapping[str, Any]] = None,
) -> str:
    """MESSAGE_ID as the ledger holds it: where it goes, why it is held, its text, its entries.

    It changes nothing.
    """
    global_config = load_global_config(_root(root))
    ledger_store = _ledger_store(global_config, store, create=False)
    return "\n".join(messages.message_show_lines(ledger_store, message_id))


@_expected_errors(ConfigError, ValueError, DraftSentNotRecorded)
def message_send_draft(
    message_id: str,
    *,
    edit: bool = False,
    justification: str = "",
    dry_run: bool = False,
    root: Optional[str] = None,
    registry: Optional[Mapping[str, Any]] = None,
    store: Optional[MutableMapping[str, Any]] = None,
    now: Optional[datetime] = None,
    editor: Optional[Callable[[str], str]] = None,
    confirm: Optional[Callable[[str], bool]] = None,
) -> str:
    """Send a held message you approved, through the gate, as ``liaise case send-draft`` does.

    The gate judges it again, and your answer is an approval bound to the text and the
    audience it showed you, recorded with ``--justification``. ``--edit`` opens it in your
    editor first. It shows where the message goes, the verdict and the exact text, and sends
    once you answer ``y`` at a terminal. A message the gate diverts stays held with the
    reason and exits 2; one its channel refuses exits 1. ``--dry-run`` judges and plans,
    asks nothing, and records nothing. It holds the run lock, and refuses while a hold keeps
    the message waiting.
    """
    config_root = _root(root)
    global_config = load_global_config(config_root)
    ledger_store = _ledger_store(global_config, store, create=not dry_run)
    title, text, opened = None, None, None
    if edit:  # before the lock: an editor can stay open far longer than a tick waits
        held = messages.held_message(Ledger(ledger_store), message_id)
        opened = messages.message_draft(held)
        title, text = _edit_title_and_text(opened, editor or edit_in_editor)
    names = _Held(
        label=f"message {message_id}",
        stays="held",
        command=f"liaise message send-draft {message_id}",
        owner=message_id,
    )
    done, confirmed = _release_after_confirmation(
        functools.partial(
            messages.send_held_message,
            subjects=load_subjects(config_root),
            message_id=message_id,
            by=messages.OPERATOR_ACTOR,
            text=text,
            title=title,
            now=now,
            registry=registry,
            justification=justification,
            fingerprint_key=_key_for(global_config, dry_run=dry_run),
        ),
        first=dict(seen=opened),
        bind=lambda judged: dict(seen=judged.draft, approval=judged.approval),
        held=lambda judged: names,
        then=lambda judged: (),
        ledger_store=ledger_store,
        global_config=global_config,
        dry_run=dry_run,
        edit=edit,
        confirm=confirm or confirm_at_terminal,
    )
    if not confirmed:
        return f"nothing sent: message {message_id} stays held"
    outbound = done.attempt.outbound
    url = getattr(done.attempt.result, "url", None)
    head = (
        f"{'would send' if dry_run else 'sent'} message {message_id} to "
        f"{outbound.recipient} on {outbound.ref} (gate: passed, {done.filters} filters)"
    )
    notes = [f"  note: {note}" for note in done.attempt.decision.notes]
    return "\n".join([head + (f": {url}" if url and not dry_run else ""), *notes])


@_expected_errors(ConfigError, ValueError)
def message_reject_draft(
    message_id: str,
    *,
    reason: str = "",
    dry_run: bool = False,
    root: Optional[str] = None,
    store: Optional[MutableMapping[str, Any]] = None,
    now: Optional[datetime] = None,
) -> str:
    """Decline a held message, recording ``--reason``.

    Nothing is sent: the message is recorded as rejected, with its reason and its text. It
    holds the run lock. ``--dry-run`` changes nothing.
    """
    global_config = load_global_config(_root(root))
    ledger_store = _ledger_store(global_config, store, create=not dry_run)
    ledger = Ledger(ChainMap({}, ledger_store) if dry_run else ledger_store)
    with ExitStack() as between_ticks:
        if not dry_run:
            _hold_run_lock(between_ticks, global_config, message_id, done="changed")
        rejected = messages.reject_message(
            ledger, message_id, reason=reason, now=now, dry_run=dry_run
        )
    verb = "would reject" if dry_run else "rejected"
    return (
        f"{verb} message {message_id} ({rejected.purpose} to {rejected.recipient} on "
        f"{rejected.ref}): {reason.strip()}"
    )


#: SSOT command tree consumed by ``__main__.py`` and any later surface (MCP, HTTP). Named
#: explicitly, so the commands read ``liaise subject show`` and ``liaise migrate-config``.
_dispatch_funcs = {
    "run": run,
    "status": status,
    "hold": hold,
    "unhold": unhold,
    "case": {
        "list": case_list,
        "show": case_show,
        "set-state": case_set_state,
        "send-draft": case_send_draft,
        "reject-draft": case_reject_draft,
    },
    "message": {
        "send": message_send,
        "list": message_list,
        "show": message_show,
        "send-draft": message_send_draft,
        "reject-draft": message_reject_draft,
    },
    "subject": {"list": subject_list, "show": subject_show},
    "setup": setup,
    "migrate-config": migrate_config,
    "schedule": {
        "install": schedule_install,
        "uninstall": schedule_uninstall,
        "status": schedule_status_cmd,
    },
}

#: Each command's seams: keyword arguments with working defaults that no command line can
#: spell (a registry, a processor, a store, a clock). A group's are nested once more.
_SEAMS = {
    "run": (
        "registry",
        "processor",
        "labeler",
        "store",
        "notify_fn",
        "sessions_dir",
        "now",
        "resolver",
        "workspace",
        "triage",
    ),
    "status": ("store", "now"),
    "hold": ("store",),
    "unhold": ("store",),
    "case": {
        "list": ("store",),
        "show": ("store",),
        "set-state": ("store", "now"),
        "send-draft": ("registry", "store", "now", "editor", "confirm"),
        "reject-draft": ("store", "now"),
    },
    "message": {
        "send": ("registry", "store", "now", "notify_fn"),
        "list": ("store",),
        "show": ("store",),
        "send-draft": ("registry", "store", "now", "editor", "confirm"),
        "reject-draft": ("store", "now"),
    },
    "setup": ("labeler",),
}


def _hidden(seams: Mapping[str, Any]) -> dict[str, Any]:
    """``seams`` as ``cw`` config: every seam hidden, keeping its default; a group's nested."""
    return {
        name: (
            _hidden(params)
            if isinstance(params, Mapping)
            else dict.fromkeys(params, cw.HIDE)
        )
        for name, params in seams.items()
    }


#: What a command's arguments need beyond their signature, nested as :data:`_SEAMS` is: a
#: draft's INDEX is a number (see :func:`_one_index`).
_ARGUMENTS = {
    "case": {
        "send-draft": {"index": {"type": int, "metavar": "INDEX"}},
        "reject-draft": {"index": {"type": int, "metavar": "INDEX"}},
    }
}


def _merged(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """``base`` with ``extra`` laid over it, a command group's entries merged, not replaced."""
    merged = dict(base)
    for name, value in extra.items():
        if isinstance(value, Mapping) and isinstance(merged.get(name), Mapping):
            merged[name] = _merged(merged[name], value)
        else:
            merged[name] = value
    return merged


#: The seams, hidden from the command line and each keeping its default, and the arguments.
_dispatch_config = _merged(_hidden(_SEAMS), _ARGUMENTS)
