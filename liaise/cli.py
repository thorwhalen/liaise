"""The ``liaise`` command-line interface.

One SSOT list of plain functions (``_dispatch_funcs``), dispatched with ``cw``
(see ``python-dispatching``). Each function takes flat, serializable arguments
and prints its own output — nothing here returns a live object across the CLI
boundary. The list grows as later issues add ``setup``, ``poll``, ``run``,
``status`` and ``schedule``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from liaise.config import ConfigError, PartnerConfig, load_config


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


#: SSOT command tree consumed by both ``__main__.py`` and (later) MCP/HTTP surfaces.
#: Named explicitly (not by function `__name__`) so `liaise partner show`, not
#: `liaise partner partner-show`.
_dispatch_funcs = {"partner": {"show": partner_show, "list": partner_list}}
