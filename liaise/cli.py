"""The ``liaise`` command-line interface.

One SSOT list of plain functions (``_dispatch_funcs``), dispatched with ``cw``
(see ``python-dispatching``). Each function takes flat, serializable arguments
and prints its own output — nothing here returns a live object across the CLI
boundary. The list grows as later issues add ``setup``, ``poll``, ``run``,
``status`` and ``schedule``.
"""

from __future__ import annotations

import functools
import time
from pathlib import Path
from typing import MutableMapping, Optional, Sequence

import cw

from liaise.config import ConfigError, GlobalConfig, PartnerConfig, load_config
from liaise.dispatch import (
    ClaudeHeadless,
    Dispatcher,
    daily_dispatch_count,
    default_store,
)
from liaise.github import GhCli, GitHub
from liaise.intake import compute_readiness, find_partner_issues
from liaise.notify import notify as _notify
from liaise.run import last_run_age, run_once
from liaise.schedule import (
    install_schedule,
    schedule_status,
    uninstall_schedule,
)
from liaise.state import STATE_LABELS, current_state, setup as _state_setup


def _notify_fn_for(glob: GlobalConfig):
    """M-4: bind `notify()` to this installation's configured
    `notify.ntfy_topic_env`, rather than every caller silently falling back
    to `notify()`'s own hardcoded default — a custom variable name would
    otherwise mean every notification silently disappears.
    """
    return functools.partial(_notify, topic_env=glob.notify.ntfy_topic_env)


def _format_partner(p: PartnerConfig) -> str:
    lines = [
        f"partner: {p.slug}",
        f"  display_name:   {p.display_name}",
        f"  github_logins:  {', '.join(p.github_logins)}",
        f"  repo:           {p.repo}",
        f"  label:          {p.label}",
        f"  brief:          {p.brief}",
        f"  reply_mode:     {p.reply_mode}",
        f"  quiet_minutes:  {p.quiet_minutes}",
        f"  go_minutes:     {p.go_minutes}",
        f"  markers:        go={p.markers.go!r} wait={p.markers.wait!r}",
        f"  label_prefix:   {p.label_prefix}",
        f"  deploy_per:     {p.deploy_per}",
        f"  budget:         timeout_minutes={p.budget.timeout_minutes} "
        f"max_turns={p.budget.max_turns} daily_dispatches={p.budget.daily_dispatches}",
        f"  verify:         {p.verify or '(none)'}",
        f"  deploy:         {p.deploy or '(none)'}",
        f"  escalate:       money_usd={p.escalate.money_usd} "
        f"max_scope={p.escalate.max_scope!r}",
    ]
    return "\n".join(lines)


def partner_show(slug: str, *, root: Optional[str] = None) -> str:
    """Print the resolved config for partner ``slug``, defaults applied."""
    config = load_config(Path(root) if root else None)
    try:
        return _format_partner(config.partner(slug))
    except ConfigError as e:
        return str(e)


def partner_list(*, root: Optional[str] = None) -> str:
    """Print every configured partner's slug and display name."""
    config = load_config(Path(root) if root else None)
    if not config.partners:
        return "(no partners configured)"
    return "\n".join(
        f"{slug}\t{p.display_name}" for slug, p in sorted(config.partners.items())
    )


def _fmt_countdown(td) -> str:
    total_seconds = int(td.total_seconds())
    if total_seconds <= 0:
        return "0m"
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def poll(*, root: Optional[str] = None, partner: Optional[str] = None, gh: Optional[GitHub] = None) -> str:
    """Report each partner's open issues, their readiness, and a countdown. Changes nothing."""
    config = load_config(Path(root) if root else None)
    partners = [config.partner(partner)] if partner else list(config.partners.values())
    github = gh if gh is not None else GhCli()

    if not partners:
        return "(no partners configured)"

    lines = []
    for p in sorted(partners, key=lambda p: p.slug):
        issues = find_partner_issues(github, p)
        lines.append(f"partner: {p.slug} ({len(issues)} open)")
        for issue in issues:
            readiness = compute_readiness(issue, p)
            if readiness.paused:
                status = "PAUSED"
            elif readiness.ready:
                status = "READY"
            else:
                status = f"in {_fmt_countdown(readiness.countdown)}"
            lines.append(f"  #{issue.number:<5} {status:<10} {issue.title}")
    return "\n".join(lines)


def setup(slug: str, *, root: Optional[str] = None, gh: Optional[GitHub] = None) -> str:
    """Create partner `slug`'s label and every state label in their repo. Idempotent."""
    config = load_config(Path(root) if root else None)
    partner = config.partner(slug)
    github = gh if gh is not None else GhCli()
    _state_setup(github, partner)
    return (
        f"created {partner.label!r}, {len(STATE_LABELS)} state labels, "
        f"and 'discovered' in {partner.repo}"
    )


def run(
    *,
    root: Optional[str] = None,
    once: bool = False,
    dry_run: bool = False,
    partner: Optional[str] = None,
    gh: Optional[GitHub] = None,
    dispatcher: Optional[Dispatcher] = None,
    store: Optional[MutableMapping] = None,
) -> str:
    """Intake, label, dispatch ready issues, batch-deploy, reconcile.

    `--dry-run` prints the plan and changes nothing. Without `--once`, keeps
    running one pass after another — the scheduled job always passes
    `--once` (see `liaise schedule install`); this is for a foreground,
    manual "keep watching" run.
    """
    config = load_config(Path(root) if root else None)
    github = gh if gh is not None else GhCli()
    agent = dispatcher if dispatcher is not None else ClaudeHeadless()
    state_store = store if store is not None else default_store(config.global_.state_dir)
    notify_fn = _notify_fn_for(config.global_)

    def one_pass() -> str:
        report = run_once(
            github, agent, state_store, config,
            partner=partner, dry_run=dry_run, notify_fn=notify_fn,
        )
        lines = [f"plan ({len(report.plan)} item(s)):"]
        for item in report.plan:
            lines.append(f"  {item.partner_slug} #{item.issue_number:<5} {item.action:<18} {item.issue_title}")
        if report.dispatched:
            lines.append(f"dispatched: {len(report.dispatched)}")
        for slug, numbers in report.deployed.items():
            lines.append(f"deployed for {slug}: {', '.join(f'#{n}' for n in numbers)}")
        return "\n".join(lines)

    if once or dry_run:
        return one_pass()

    # L-4: without --once, this runs indefinitely — printing each pass as it
    # happens (rather than accumulating every pass's output into a list
    # returned only at the end) is what makes a long foreground run usable
    # rather than silent and unbounded in memory.
    try:
        while True:
            print(one_pass())
            time.sleep(60)
    except KeyboardInterrupt:
        return "stopped"


def status(*, root: Optional[str] = None, gh: Optional[GitHub] = None, store: Optional[MutableMapping] = None) -> str:
    """Last run stamp, today's dispatches per partner, and anything needing the owner."""
    config = load_config(Path(root) if root else None)
    github = gh if gh is not None else GhCli()
    state_store = store if store is not None else default_store(config.global_.state_dir)

    age = last_run_age(state_store)
    lines = [f"last_run: {f'{int(age)}s ago' if age is not None else 'never'}"]

    for p in sorted(config.partners.values(), key=lambda p: p.slug):
        count = daily_dispatch_count(state_store, p)
        lines.append(f"partner {p.slug}: {count}/{p.budget.daily_dispatches} dispatches today")
        needs_owner = [
            i for i in find_partner_issues(github, p) if current_state(i, p) == "needs-owner"
        ]
        for issue in needs_owner:
            lines.append(f"  needs-owner: #{issue.number} {issue.title}")
    return "\n".join(lines)


def schedule_install(
    *,
    root: Optional[str] = None,
    interval_minutes: int = 2,
    extra_env_vars: Optional[Sequence[str]] = None,
) -> str:
    """Install the scheduled `liaise run --once` job (launchd on macOS, systemd on Linux).

    `extra_env_vars`: names of additional environment variables (beyond
    `PATH`, `HOME`, and the configured ntfy topic variable) a partner's
    dispatch command needs snapshotted into the job's environment — A.7
    ("whatever the dispatch command needs"), which had no way to reach the
    scheduler from the CLI (M-4).
    """
    config = load_config(Path(root) if root else None)
    path = install_schedule(
        root=root,
        interval_minutes=interval_minutes,
        ntfy_topic_env=config.global_.notify.ntfy_topic_env,
        extra_env_vars=extra_env_vars or (),
    )
    return f"installed: {path}"


def schedule_uninstall() -> str:
    """Remove the scheduled job. Idempotent."""
    return uninstall_schedule()


def schedule_status_cmd() -> str:
    """Whether the scheduled job is installed."""
    return schedule_status()


#: SSOT command tree consumed by both ``__main__.py`` and (later) MCP/HTTP surfaces.
#: Named explicitly (not by function `__name__`) so `liaise partner show`, not
#: `liaise partner partner-show`.
_dispatch_funcs = {
    "partner": {"show": partner_show, "list": partner_list},
    "poll": poll,
    "setup": setup,
    "run": run,
    "status": status,
    "schedule": {
        "install": schedule_install,
        "uninstall": schedule_uninstall,
        "status": schedule_status_cmd,
    },
}

#: DI seams (real `GitHub`/`Dispatcher`/store by default) — not CLI-serializable,
#: so hidden from the command line.
_dispatch_config = {
    "poll": {"gh": cw.HIDE},
    "setup": {"gh": cw.HIDE},
    "run": {"gh": cw.HIDE, "dispatcher": cw.HIDE, "store": cw.HIDE},
    "status": {"gh": cw.HIDE, "store": cw.HIDE},
}
