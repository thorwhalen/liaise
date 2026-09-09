"""Config model and loading for liaise.

Everything partner-specific — identities, repos, briefs, commands, hosts — lives
under ``~/.config/liaise/`` (see :func:`load_config`), never in this package's
code, tests, docs, fixtures or examples. This module only knows the *shape* of
that configuration and how to resolve it into frozen dataclasses with defaults
applied; it never hardcodes a real partner.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - liaise requires >=3.11
    import tomli as tomllib

DFLT_CONFIG_ROOT = Path.home() / ".config" / "liaise"

DFLT_QUIET_MINUTES = 10
DFLT_GO_MINUTES = 2
DFLT_GO_MARKER = "#startwork#"
DFLT_WAIT_MARKER = "#wait#"
DFLT_LABEL_PREFIX = "liaise:"
DFLT_TIMEOUT_MINUTES = 60
DFLT_MAX_TURNS = 200
DFLT_DAILY_DISPATCHES = 6
DFLT_DEPLOY_PER = "batch"
DFLT_REPLY_MODE = "draft"
DFLT_NTFY_TOPIC_ENV = "LIAISE_NTFY_TOPIC"
DFLT_DISPATCH_COMMAND = (
    "claude -p {prompt_file} --permission-mode auto --output-format json"
)
DFLT_RESUME_COMMAND = (
    "claude --resume {session_id} -p {prompt_file} --output-format json"
)


class ConfigError(Exception):
    """Raised when configuration is missing or malformed.

    Always names the path involved and, for a missing file, the minimal content
    that would fix it — an agent (or a human) reading the error should not have
    to go spelunking in the design docs to recover.
    """


@dataclass(frozen=True)
class Markers:
    """Literal, case-insensitive substrings a partner can drop in a comment."""

    go: str = DFLT_GO_MARKER
    wait: str = DFLT_WAIT_MARKER


@dataclass(frozen=True)
class Budget:
    """Per-dispatch and per-day limits. Mandatory, never unlimited."""

    timeout_minutes: int = DFLT_TIMEOUT_MINUTES
    max_turns: int = DFLT_MAX_TURNS
    daily_dispatches: int = DFLT_DAILY_DISPATCHES


@dataclass(frozen=True)
class NotifyConfig:
    """Where to send owner notifications. See :mod:`liaise.notify`."""

    ntfy_topic_env: str = DFLT_NTFY_TOPIC_ENV


@dataclass(frozen=True)
class DispatchConfig:
    """Command templates used to run (and resume) the coding agent."""

    command: str = DFLT_DISPATCH_COMMAND
    cwd: str = "."
    resume_command: str = DFLT_RESUME_COMMAND


@dataclass(frozen=True)
class EscalateConfig:
    """Thresholds beyond which the agent must route a decision to the owner."""

    money_usd: float = 50.0
    max_scope: str = "about a day of work"


#: Fields every partner inherits from the global config unless overridden.
_INHERITABLE_DEFAULTS = (
    "quiet_minutes",
    "go_minutes",
    "markers",
    "label_prefix",
    "budget",
    "deploy_per",
)


@dataclass(frozen=True)
class GlobalConfig:
    """``~/.config/liaise/config.toml`` — settings every partner inherits."""

    owner_login: str
    state_dir: str
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    quiet_minutes: int = DFLT_QUIET_MINUTES
    go_minutes: int = DFLT_GO_MINUTES
    markers: Markers = field(default_factory=Markers)
    label_prefix: str = DFLT_LABEL_PREFIX
    budget: Budget = field(default_factory=Budget)
    deploy_per: str = DFLT_DEPLOY_PER


@dataclass(frozen=True)
class PartnerConfig:
    """A resolved partner: ``~/.config/liaise/partners/<slug>.toml`` plus defaults."""

    slug: str
    display_name: str
    github_logins: tuple[str, ...]
    repo: str
    brief: str
    label: str
    reply_mode: str = DFLT_REPLY_MODE
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    verify: str = ""
    deploy: str = ""
    escalate: EscalateConfig = field(default_factory=EscalateConfig)
    quiet_minutes: int = DFLT_QUIET_MINUTES
    go_minutes: int = DFLT_GO_MINUTES
    markers: Markers = field(default_factory=Markers)
    label_prefix: str = DFLT_LABEL_PREFIX
    budget: Budget = field(default_factory=Budget)
    deploy_per: str = DFLT_DEPLOY_PER


@dataclass(frozen=True)
class Config:
    """Everything :func:`load_config` resolved: the global config and every partner."""

    global_: GlobalConfig
    partners: Mapping[str, PartnerConfig]

    def partner(self, slug: str) -> PartnerConfig:
        """Return the resolved partner config for ``slug``, or raise :class:`ConfigError`."""
        try:
            return self.partners[slug]
        except KeyError:
            known = ", ".join(sorted(self.partners)) or "(none configured)"
            raise ConfigError(
                f"No partner {slug!r} configured. Known partners: {known}. "
                f"Add {DFLT_CONFIG_ROOT / 'partners' / (slug + '.toml')}."
            ) from None


def _load_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Missing config file: {path}") from None


def _missing_global_config_error(path: Path) -> ConfigError:
    return ConfigError(
        f"Missing global config at {path}. Create it with at least:\n\n"
        '  owner_login = "your-github-login"\n'
        '  state_dir = "~/.local/share/liaise"\n'
    )


def _missing_partner_field_error(path: Path, field_name: str) -> ConfigError:
    return ConfigError(
        f"{path} is missing required field {field_name!r}. A minimal partner "
        f"file needs at least:\n\n"
        '  display_name = "Pat"\n'
        '  github_logins = ["pat"]\n'
        '  repo = "example/app"\n'
        '  brief = "~/.config/liaise/briefs/pat.md"\n'
    )


def _markers_from(raw: Optional[dict], *, dflt: Markers) -> Markers:
    if not raw:
        return dflt
    return Markers(go=raw.get("go", dflt.go), wait=raw.get("wait", dflt.wait))


def _budget_from(raw: Optional[dict], *, dflt: Budget) -> Budget:
    if not raw:
        return dflt
    return Budget(
        timeout_minutes=raw.get("timeout_minutes", dflt.timeout_minutes),
        max_turns=raw.get("max_turns", dflt.max_turns),
        daily_dispatches=raw.get("daily_dispatches", dflt.daily_dispatches),
    )


def _load_global_config(root: Path) -> GlobalConfig:
    path = root / "config.toml"
    if not path.exists():
        raise _missing_global_config_error(path)
    raw = _load_toml(path)
    for required in ("owner_login", "state_dir"):
        if required not in raw:
            raise ConfigError(
                f"{path} is missing required field {required!r}."
            )
    dflt_markers = Markers()
    dflt_budget = Budget()
    notify_raw = raw.get("notify") or {}
    return GlobalConfig(
        owner_login=raw["owner_login"],
        state_dir=raw["state_dir"],
        notify=NotifyConfig(
            ntfy_topic_env=notify_raw.get("ntfy_topic_env", DFLT_NTFY_TOPIC_ENV)
        ),
        quiet_minutes=raw.get("quiet_minutes", DFLT_QUIET_MINUTES),
        go_minutes=raw.get("go_minutes", DFLT_GO_MINUTES),
        markers=_markers_from(raw.get("markers"), dflt=dflt_markers),
        label_prefix=raw.get("label_prefix", DFLT_LABEL_PREFIX),
        budget=_budget_from(raw.get("budget"), dflt=dflt_budget),
        deploy_per=raw.get("deploy_per", DFLT_DEPLOY_PER),
    )


def _load_partner_config(path: Path, *, glob: GlobalConfig) -> PartnerConfig:
    raw = _load_toml(path)
    slug = path.stem
    for required in ("display_name", "github_logins", "repo", "brief"):
        if required not in raw:
            raise _missing_partner_field_error(path, required)

    dispatch_raw = raw.get("dispatch") or {}
    dispatch = DispatchConfig(
        command=dispatch_raw.get("command", DFLT_DISPATCH_COMMAND),
        cwd=dispatch_raw.get("cwd", "."),
        resume_command=dispatch_raw.get("resume_command", DFLT_RESUME_COMMAND),
    )
    escalate_raw = raw.get("escalate") or {}
    escalate = EscalateConfig(
        money_usd=escalate_raw.get("money_usd", 50.0),
        max_scope=escalate_raw.get("max_scope", "about a day of work"),
    )

    return PartnerConfig(
        slug=slug,
        display_name=raw["display_name"],
        github_logins=tuple(raw["github_logins"]),
        repo=raw["repo"],
        brief=raw["brief"],
        label=raw.get("label", f"partner:{slug}"),
        reply_mode=raw.get("reply_mode", DFLT_REPLY_MODE),
        dispatch=dispatch,
        verify=raw.get("verify", ""),
        deploy=raw.get("deploy", ""),
        escalate=escalate,
        quiet_minutes=raw.get("quiet_minutes", glob.quiet_minutes),
        go_minutes=raw.get("go_minutes", glob.go_minutes),
        markers=_markers_from(raw.get("markers"), dflt=glob.markers),
        label_prefix=raw.get("label_prefix", glob.label_prefix),
        budget=_budget_from(raw.get("budget"), dflt=glob.budget),
        deploy_per=raw.get("deploy_per", glob.deploy_per),
    )


def load_config(root: Optional[Path] = None) -> Config:
    """Load and resolve liaise's configuration into frozen dataclasses.

    ``root`` defaults to ``~/.config/liaise``. A missing global config raises
    :class:`ConfigError` naming the path and the minimal content needed. Every
    partner under ``partners/*.toml`` is resolved with the global defaults
    applied, then overridden by whatever the partner file sets explicitly.
    """
    root = Path(root) if root is not None else DFLT_CONFIG_ROOT
    glob = _load_global_config(root)

    partners: dict[str, PartnerConfig] = {}
    partners_dir = root / "partners"
    if partners_dir.is_dir():
        for path in sorted(partners_dir.glob("*.toml")):
            partner = _load_partner_config(path, glob=glob)
            partners[partner.slug] = partner

    return Config(global_=glob, partners=partners)
