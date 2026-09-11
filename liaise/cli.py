"""The ``liaise`` command line (liaise 0.1).

One SSOT command tree, ``_dispatch_funcs``, of plain functions dispatched with ``cw``::

    liaise run [--once] [--dry-run] [--subject SLUG]
    liaise status
    liaise hold SCOPE [--mode MODE] [--reason TEXT]
    liaise unhold SCOPE
    liaise subject list
    liaise subject show SLUG
    liaise setup SUBJECT
    liaise migrate-config [--apply]
    liaise schedule install | uninstall | status

Every command takes ``--root``, the config root (``~/.config/liaise`` by default), and
returns the text it prints. The seams (the channel registry, the processor, the labeler,
the ledger store, the notifier, the sessions directory and the clock) are keyword
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
import time
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import cw

from liaise import holds, migrate
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
from liaise.tick import DFLT_RUNS_SUBDIR, RunLockHeld, run_once, status_lines

#: Seconds between two ticks of ``liaise run`` without ``--once``. The scheduled job
#: passes ``--once`` and leaves the interval to the scheduler.
DFLT_LOOP_SECONDS = 60
#: What ``liaise run`` without ``--once`` returns once it is interrupted.
STOPPED = "stopped"
#: How ``liaise subject show`` prints an empty or unset value.
NONE_SHOWN = "(none)"


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
) -> str:
    """One tick: take in what arrived, collect finished runs, start ready cases, deploy, label.

    Prints the tick's plan, a line per event, case, run and decision. ``--dry-run`` prints
    the same plan and changes nothing: nothing is sent, labelled, started, cancelled,
    deployed, locked or written. ``--subject`` ticks one subject alone. Without ``--once``
    or ``--dry-run``, it ticks every minute, printing each plan, until interrupted; the
    scheduled job (``liaise schedule install``) passes ``--once``.
    """
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


#: SSOT command tree consumed by ``__main__.py`` and any later surface (MCP, HTTP). Named
#: explicitly, so the commands read ``liaise subject show`` and ``liaise migrate-config``.
_dispatch_funcs = {
    "run": run,
    "status": status,
    "hold": hold,
    "unhold": unhold,
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
#: spell (a registry, a processor, a store, a clock).
_SEAMS = {
    "run": (
        "registry",
        "processor",
        "labeler",
        "store",
        "notify_fn",
        "sessions_dir",
        "now",
    ),
    "status": ("store", "now"),
    "hold": ("store",),
    "unhold": ("store",),
    "setup": ("labeler",),
}
#: The seams, hidden from the command line; each keeps its default.
_dispatch_config = {
    command: dict.fromkeys(params, cw.HIDE) for command, params in _SEAMS.items()
}
