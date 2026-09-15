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
from contextlib import ExitStack, suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import cw

from liaise import cases, holds, migrate
from liaise.access import Resolver
from liaise.config import (
    DFLT_CONFIG_ROOT,
    ConfigError,
    GlobalConfig,
    load_global_config,
)
from liaise.github import GhCli, GitHub, GitHubError
from liaise.ledger import DFLT_LEDGER_SUBDIR, Ledger, default_ledger_store
from liaise.model import HOLD_MODES, require_one_of
from liaise.processor import ClaudeHeadless
from liaise.projection import setup_labels
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
#: What a case command that writes says, changing nothing, while a tick holds the run lock.
TICK_RUNNING = (
    "a liaise tick is running, so {case_id} was not {done}; try again shortly ({busy})"
)
#: The environment variables that name the operator's editor, the first one set winning.
EDITOR_ENV_VARS = ("VISUAL", "EDITOR")
#: The editor ``liaise case send-draft --edit`` opens when no variable names one.
DFLT_EDITOR = "notepad" if sys.platform == "win32" else "vi"


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
    stack: ExitStack, global_config: GlobalConfig, case_id: str, *, done: str
) -> None:
    """Take the run lock into ``stack``, or refuse in one line while a tick holds it.

    A case command that writes holds the lock, so a tick cannot start meanwhile and write
    over it. It does not wait: the refusal says what was not ``done``.
    """
    lock_path = run_lock_path(Path(global_config.state_dir).expanduser())
    try:
        stack.enter_context(run_lock(lock_path))
    except RunLockHeld as busy:
        message = TICK_RUNNING.format(case_id=case_id, done=done, busy=busy)
        raise cw.CommandError(message) from busy


def edit_in_editor(text: str) -> str:
    """``text`` as the operator leaves it in their editor: ``$VISUAL``, ``$EDITOR``, else vi.

    The text is written to a temporary file only its owner can read, which is removed once
    the editor exits. Raises ``ValueError`` when the editor cannot be started or exits
    nonzero, so nothing is sent.
    """
    command = next(
        (os.environ[name] for name in EDITOR_ENV_VARS if os.environ.get(name)),
        DFLT_EDITOR,
    )
    descriptor, path = tempfile.mkstemp(prefix="liaise-draft-", suffix=".md")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
        try:
            argv = [*shlex.split(command, posix=os.name != "nt"), path]
            exit_code = subprocess.run(argv, check=False).returncode
        except OSError as error:
            raise ValueError(
                f"the editor {command!r} could not be started ({error}); set $EDITOR"
            ) from error
        if exit_code != 0:
            raise ValueError(
                f"the editor {command!r} exited with {exit_code}, so nothing was sent"
            )
        return Path(path).read_text(encoding="utf-8")
    finally:
        with suppress(OSError):
            os.unlink(path)


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
    scheduled job (``liaise schedule install``) passes ``--once``.

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
    """Every configured subject with its bindings, flagging any binding that could never match."""
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
        lines.append(f"{slug}\t{', '.join(subject.bindings)}{flag}")
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

    Those are its claim labels and one ``<label_prefix><state>`` label per case state.
    """
    config_root = _root(root)
    found = _subject_named(load_subjects(config_root), subject, root=config_root)
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


@_expected_errors(ConfigError, ValueError)
def case_send_draft(
    case_id: str,
    *index: int,
    edit: bool = False,
    dry_run: bool = False,
    root: Optional[str] = None,
    registry: Optional[Mapping[str, Any]] = None,
    store: Optional[MutableMapping[str, Any]] = None,
    now: Optional[datetime] = None,
    editor: Optional[Callable[[str], str]] = None,
) -> str:
    """Send a draft you approved: CASE_ID's draft INDEX, or its only one, through the gate.

    ``liaise case show`` numbers the drafts. The gate runs again on the text, with your
    approval recorded: draft reply mode lets it through, and the leak scan, deslop and the
    mention judge it as they judge any message. ``--edit`` opens the text in ``$VISUAL`` or
    ``$EDITOR`` first, and the gate judges what you saved.

    Once sent, the draft leaves the case and the send is recorded as yours. A case in
    needs-owner moves on as a sent message moves it (an ask, to needs-partner). A message
    that is diverted, or that its channel refuses, is not sent: the draft stays on the case,
    holding the text that was judged and the reason, and the command exits nonzero.
    ``--dry-run`` judges and plans, and changes nothing.

    It holds the run lock, as ``set-state`` does, and refuses while a tick runs. It also
    refuses while a hold keeps the case's messages waiting, and while a run of the case is
    in flight.
    """
    config_root = _root(root)
    global_config = load_global_config(config_root)
    subjects = load_subjects(config_root)
    ledger_store = _ledger_store(global_config, store, create=not dry_run)
    ledger = Ledger(ChainMap({}, ledger_store) if dry_run else ledger_store)
    draft_index, text, seen = _one_index(case_id, index), None, None
    if edit:  # before the lock: an editor can stay open far longer than a tick waits
        draft_index, seen = cases.find_draft(ledger, case_id, index=draft_index)
        text = (editor or edit_in_editor)(seen.get("text") or "")
    with ExitStack() as between_ticks:
        if not dry_run:
            _hold_run_lock(between_ticks, global_config, case_id, done="sent")
        release = cases.send_draft(
            ledger,
            subjects,
            case_id,
            index=draft_index,
            text=text,
            seen=seen,
            now=now,
            registry=registry,
            dry_run=dry_run,
        )
    label = f"draft [{release.index}] of {case_id}"
    attempt = release.attempt
    notes = [f"  note: {note}" for note in attempt.decision.notes]
    if not attempt.sent:
        decision = attempt.decision
        why = (
            f"diverted by {decision.diverted_by}: {decision.diverted}"
            if decision.send is None
            else f"send failed: {attempt.failure}"
        )
        kept = (
            ""
            if dry_run
            else f". It stays on the case with that reason; edit it with liaise case "
            f"send-draft {case_id} {release.index} --edit"
        )
        verb = "would not be sent" if dry_run else "was not sent"
        raise cw.CommandError("\n".join([f"{label} {verb}: {why}{kept}", *notes]))
    ref = release.draft.get("ref")
    url = getattr(attempt.result, "url", None)
    head = (
        f"{'would send' if dry_run else 'sent'} {label} on {ref} "
        f"(gate: passed, {release.filters} filters)"
    )
    lines = [head + (f": {url}" if url and not dry_run else ""), *notes]
    if release.moved:
        before, after = release.moved
        verb = "would move" if dry_run else "moved"
        lines.append(
            f"{verb} {case_id} from {before} to {after}; its labels follow on the next "
            f"tick"
        )
    else:
        lines.append(f"{case_id} stays {release.case.state}")
    if release.case.drafts:
        lines.append(f"drafts left on {case_id}: {len(release.case.drafts)}")
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
        "send-draft": ("registry", "store", "now", "editor"),
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
        "send-draft": {"index": {"type": int}},
        "reject-draft": {"index": {"type": int}},
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
