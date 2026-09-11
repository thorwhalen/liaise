"""Derive 0.1 subject files from a 0.0.x configuration: ``liaise migrate-config``.

0.0.x configured one partner per file (``partners/<slug>.toml``, plus the shared
``config.toml``). 0.1 configures one subject per body of work (``subjects/<slug>.toml``,
see :mod:`liaise.subjects`). :func:`migrate_config` groups the partners by repo, one
subject per repo, maps every field, and returns a :class:`MigrationPlan`: the TOML each
subject file would hold, the files it came from, and what the operator should look at.

It writes nothing unless ``apply=True``, and then only creates missing subject files: it
never overwrites one, and never touches ``config.toml``, ``partners/`` or ``briefs/``.

- **Chosen, not defaulted.** A value the operator set (in a partner file or in
  ``config.toml``) is carried. A value 0.0.x only defaulted is left to 0.1's default,
  which is the same constant.
- **Partners sharing a repo must agree** on the subject's settings (verify, readiness,
  delivery, workspace...). The first partner's value is kept, and every other value is
  listed as a conflict. Budgets keep the strictest value instead.
- **Briefs stay per person.** Each partner's brief goes to ``policy.briefs``, and the
  subject's own ``brief`` is set only when every partner has the same one.
- **Bindings match by label only.** 0.0.x also took any issue a partner's login opened,
  which claimed issues never meant for liaise. In 0.1, matching a login is opt-in, and a
  note on the subject names the binding that opts in.
- **Custom command templates are not carried**: 0.1 builds the ``claude`` command itself.
"""

from __future__ import annotations

import math
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from correspond import check_binding

from liaise.config import (
    DFLT_CONFIG_ROOT,
    DFLT_DISPATCH_COMMAND,
    DFLT_PERMISSION_MODE,
    DFLT_RESUME_COMMAND,
    ConfigError,
    PartnerConfig,
    load_config,
)
from liaise.subjects import (
    DFLT_CONCURRENT_RUNS,
    DFLT_DELIVERY_KIND,
    DFLT_SUBJECTS_SUBDIR,
    DFLT_WORKSPACE_KIND,
    REPLY_MODES,
    load_subject,
)

#: The 0.0.x global config file, under the config root.
GLOBAL_CONFIG_FILE = "config.toml"
#: The 0.0.x partner files' directory, under the config root.
PARTNERS_SUBDIR = "partners"
#: The channel every 0.0.x partner was on.
GITHUB_CHANNEL = "github"
#: The role every migrated partner gets on its subject.
PARTNER_ROLE = "partner"
#: 0.0.x posted directly for any ``reply_mode`` but draft; 0.1 names that mode.
POSTING_REPLY_MODE = "direct"
#: The budget limits carried, each keeping the strictest partner's value.
BUDGET_LIMITS = ("timeout_minutes", "max_turns", "daily_dispatches")


@dataclass(frozen=True)
class PlannedSubject:
    """One subject file :func:`migrate_config` would write, and what went into it."""

    slug: str
    path: Path
    toml_text: str
    #: The 0.0.x files its values came from.
    sources: tuple[Path, ...]
    #: Where the subject's partners disagreed, and which value was kept.
    conflicts: tuple[str, ...] = ()
    #: What the operator should check before relying on the file.
    warnings: tuple[str, ...] = ()
    #: What :func:`correspond.check_binding` says would keep a binding from matching.
    binding_problems: tuple[str, ...] = ()
    #: What 0.1 does differently from 0.0.x for this subject, and how to change that.
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MigrationPlan:
    """What ``liaise migrate-config`` would write, and what it wrote once applied."""

    subjects: tuple[PlannedSubject, ...]
    warnings: tuple[str, ...] = ()
    applied: bool = False
    written: tuple[Path, ...] = ()

    def lines(self) -> list[str]:
        """The printable plan, subject by subject, ending with what was written.

        For each subject: its target path, its sources, the TOML, then its conflicts,
        warnings, binding problems and notes. Then the plan's own warnings, then
        ``nothing written (dry run)`` or ``wrote N subject file(s)``.
        """
        out: list[str] = []
        for subject in self.subjects:
            out.append(f"subject {subject.slug}: {subject.path}")
            out.append("  from: " + ", ".join(map(str, subject.sources)))
            out += [
                f"    {line}" if line else "" for line in subject.toml_text.splitlines()
            ]
            out += [f"  conflict: {conflict}" for conflict in subject.conflicts]
            out += [f"  warning: {warning}" for warning in subject.warnings]
            out += [
                f"  binding problem: {problem}" for problem in subject.binding_problems
            ]
            out += [f"  note: {note}" for note in subject.notes]
        out += [f"warning: {warning}" for warning in self.warnings]
        if self.applied:
            out.append(f"wrote {len(self.written)} subject file(s)")
        else:
            out.append("nothing written (dry run)")
        return out


def migrate_config(
    root: Optional[Path] = None,
    *,
    apply: bool = False,
    registry: Optional[Mapping[str, Any]] = None,
) -> MigrationPlan:
    """Plan the 0.1 subject files for the 0.0.x config under ``root``; write them if ``apply``.

    ``root`` defaults to ``~/.config/liaise``. A dry run (the default) writes nothing.
    ``apply=True`` creates ``subjects/<slug>.toml`` for each subject whose file does not
    exist yet (see :func:`apply_plan`). ``registry`` is correspond's channel registry,
    used only to check bindings (its default when None; that check runs no adapter).
    Raises :class:`~liaise.config.ConfigError` when the 0.0.x config does not load.
    """
    plan = plan_migration(root, registry=registry)
    return apply_plan(plan) if apply else plan


def plan_migration(
    root: Optional[Path] = None, *, registry: Optional[Mapping[str, Any]] = None
) -> MigrationPlan:
    """The dry-run :class:`MigrationPlan` for the 0.0.x config under ``root``, writing nothing.

    One subject per repo (compared case-insensitively, as GitHub does), in slug order.
    """
    root = Path(root) if root is not None else DFLT_CONFIG_ROOT
    config = load_config(root)
    global_raw = _read_toml(root / GLOBAL_CONFIG_FILE)
    partners_dir = root / PARTNERS_SUBDIR
    by_repo: dict[str, list[_PartnerSource]] = {}
    for path in sorted(partners_dir.glob("*.toml")):
        source = _PartnerSource(config.partner(path.stem), _read_toml(path), path)
        by_repo.setdefault(source.config.repo.lower(), []).append(source)
    if not by_repo:
        return MigrationPlan(
            subjects=(),
            warnings=(f"no partner files under {partners_dir}: nothing to migrate",),
        )
    slugs, warnings = _subject_slugs(by_repo)
    subjects = sorted(
        (
            _plan_subject(
                slugs[repo],
                repo,
                sources,
                root=root,
                owner_login=config.global_.owner_login,
                global_raw=global_raw,
                registry=registry,
            )
            for repo, sources in by_repo.items()
        ),
        key=lambda subject: subject.slug,
    )
    return MigrationPlan(subjects=tuple(subjects), warnings=tuple(warnings))


def apply_plan(plan: MigrationPlan) -> MigrationPlan:
    """Write each planned subject file that does not exist yet, and return ``plan`` as applied.

    An existing file is never overwritten: its subject gets a warning instead. Each file
    written is loaded back with :func:`~liaise.subjects.load_subject`, and one that does
    not load gets a warning naming the error. Nothing outside ``subjects/`` is touched.
    """
    subjects, written = [], []
    for subject in plan.subjects:
        subject.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(subject.path, "x", encoding="utf-8") as f:
                f.write(subject.toml_text)
        except FileExistsError:
            subjects.append(_warned(subject, _exists_warning(subject.path)))
            continue
        written.append(subject.path)
        subjects.append(_warned(subject, _load_error(subject.path)))
    return replace(plan, subjects=tuple(subjects), applied=True, written=tuple(written))


# ---- one subject from the partners on its repo ----


@dataclass(frozen=True)
class _PartnerSource:
    """A resolved 0.0.x partner with its raw file, to tell a chosen value from a default."""

    config: PartnerConfig
    raw: Mapping[str, Any]
    path: Path


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Missing config file: {path}") from None


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _subject_slugs(repos: Iterable[str]) -> tuple[dict[str, str], list[str]]:
    """Each repo's subject slug, and a warning for each slug collision resolved.

    A slug is the repo slugified (``example/app`` gives ``example-app``). Repos that
    slugify alike each add ``--<owner>``; any still alike add ``-2``, ``-3``, and so on.
    """
    repos = list(repos)
    base = {repo: _slugify(repo) for repo in repos}
    counts = Counter(base.values())
    slugs: dict[str, str] = {}
    for repo in repos:
        slug = base[repo]
        if counts[slug] > 1:
            slug = f"{slug}--{_slugify(repo.partition('/')[0])}"
        unique, n = slug, 1
        while unique in slugs.values():
            n += 1
            unique = f"{slug}-{n}"
        slugs[repo] = unique
    warnings = [
        f"{', '.join(r for r in repos if base[r] == slug)} all slugify to {slug!r}, "
        f"so each subject slug adds --<owner>"
        for slug, count in counts.items()
        if count > 1
    ]
    return slugs, warnings


def _plan_subject(
    slug: str,
    repo: str,
    sources: list[_PartnerSource],
    *,
    root: Path,
    owner_login: str,
    global_raw: Mapping[str, Any],
    registry: Optional[Mapping[str, Any]],
) -> PlannedSubject:
    conflicts: list[str] = []
    warnings: list[str] = []
    settings = [_settings(source, global_raw) for source in sources]

    def merged(key: str, *, strictest: bool = False) -> Any:
        """The subject's value for ``key``, or None when no partner chose one."""
        entries = [
            (source.config.slug, *setting[key])
            for source, setting in zip(sources, settings)
        ]
        value, chosen = _merge(key, entries, strictest=strictest, conflicts=conflicts)
        return value if chosen else None

    bindings, identity = _identity(
        repo, sources, owner_login=owner_login, conflicts=conflicts, warnings=warnings
    )
    shared_brief, briefs = _briefs(sources)
    doc = {
        "bindings": bindings,
        "workspace": InlineTable(
            kind=DFLT_WORKSPACE_KIND, path=merged("workspace.path")
        ),
        "brief": shared_brief,
        "verify": merged("verify"),
        "delivery": InlineTable(
            kind=DFLT_DELIVERY_KIND,
            per=merged("delivery.per"),
            command=merged("delivery.command"),
        ),
        "label_prefix": merged("label_prefix"),
        "policy": {
            **identity,
            "briefs": InlineTable(briefs) if briefs else None,
            "deployed_nudge_days": merged("policy.deployed_nudge_days"),
            "readiness": {
                key: merged(f"policy.readiness.{key}")
                for key in ("quiet_minutes", "go_minutes", "markers")
            },
            "escalate": {
                key: merged(f"policy.escalate.{key}")
                for key in ("money_usd", "max_scope")
            },
            "budget": {
                "concurrent": DFLT_CONCURRENT_RUNS,
                **{
                    key: merged(f"policy.budget.{key}", strictest=True)
                    for key in BUDGET_LIMITS
                },
            },
        },
        "processor": {"permission_mode": merged("processor.permission_mode")},
    }
    for source in sources:
        warnings += _partner_warnings(source)
    path = root / DFLT_SUBJECTS_SUBDIR / f"{slug}.toml"
    if path.exists():
        warnings.append(_exists_warning(path))
    return PlannedSubject(
        slug=slug,
        path=path,
        toml_text=dumps_toml(doc),
        sources=(root / GLOBAL_CONFIG_FILE, *(source.path for source in sources)),
        conflicts=tuple(conflicts),
        warnings=tuple(warnings),
        binding_problems=tuple(
            f"binding {binding!r}: {problem}"
            for binding in bindings
            for problem in check_binding(binding, registry=registry)
        ),
        notes=tuple(_author_notes(repo, sources)),
    )


def _settings(
    source: _PartnerSource, global_raw: Mapping[str, Any]
) -> dict[str, tuple[Any, bool]]:
    """The partner's resolved value for each subject setting, and whether it was chosen.

    Chosen means set in the partner file, or in ``config.toml`` for the settings 0.0.x
    inherited from it. The permission mode also counts as chosen when 0.0.x derived it
    from a mode a custom ``dispatch.command`` hardcodes.
    """
    partner, raw = source.config, source.raw

    def chosen(key: str, *, table: Optional[str] = None, inherits: bool = True) -> bool:
        layers = (raw, global_raw) if inherits else (raw,)
        return any(
            key in ((layer.get(table) or {}) if table else layer) for layer in layers
        )

    dispatch = partner.dispatch
    markers = InlineTable(go=partner.markers.go, wait=partner.markers.wait)
    return {
        "workspace.path": (dispatch.cwd, True),
        "verify": (partner.verify, chosen("verify", inherits=False)),
        "delivery.per": (partner.deploy_per, True),
        "delivery.command": (partner.deploy, True),
        "label_prefix": (partner.label_prefix, chosen("label_prefix")),
        "policy.deployed_nudge_days": (
            partner.deployed_nudge_days,
            chosen("deployed_nudge_days"),
        ),
        "policy.readiness.quiet_minutes": (
            partner.quiet_minutes,
            chosen("quiet_minutes"),
        ),
        "policy.readiness.go_minutes": (partner.go_minutes, chosen("go_minutes")),
        "policy.readiness.markers": (markers, chosen("markers")),
        "policy.escalate.money_usd": (
            partner.escalate.money_usd,
            chosen("money_usd", table="escalate", inherits=False),
        ),
        "policy.escalate.max_scope": (
            partner.escalate.max_scope,
            chosen("max_scope", table="escalate", inherits=False),
        ),
        **{
            f"policy.budget.{key}": (
                getattr(partner.budget, key),
                chosen(key, table="budget"),
            )
            for key in BUDGET_LIMITS
        },
        "processor.permission_mode": (
            dispatch.permission_mode,
            chosen("permission_mode", table="dispatch", inherits=False)
            or dispatch.permission_mode != DFLT_PERMISSION_MODE,
        ),
    }


def _merge(
    key: str,
    entries: list[tuple[str, Any, bool]],
    *,
    strictest: bool,
    conflicts: list[str],
) -> tuple[Any, bool]:
    """One value from each partner's ``(slug, value, chosen)``, and whether any chose it.

    Keeps the first partner's value, or the smallest when ``strictest``, and adds a
    conflict naming every partner whose value differs.
    """
    (first, first_value, _), *_rest = entries
    kept = min(value for _, value, _ in entries) if strictest else first_value
    others = [
        f"{slug} has {value!r}{'' if chosen else ' (a default)'}"
        for slug, value, chosen in entries
        if value != kept
    ]
    if others:
        how = f"the strictest, {kept!r}" if strictest else f"{first}'s {kept!r}"
        conflicts.append(f"{key}: kept {how}; " + "; ".join(others))
    return kept, any(chosen for *_, chosen in entries)


def _identity(
    repo: str,
    sources: list[_PartnerSource],
    *,
    owner_login: str,
    conflicts: list[str],
    warnings: list[str],
) -> tuple[list[str], dict[str, Any]]:
    """The subject's bindings, and its policy's who-is-who: people, roles, claims, replies."""
    bindings: list[str] = []
    people: dict[str, str] = {}
    roles: dict[str, str] = {}
    claim_labels: dict[str, str] = {}
    notify: dict[str, str] = {}
    reply_modes: dict[str, str] = {}
    default_reply_mode: Optional[str] = None

    def assign(table: dict[str, str], key: str, person: str, *, what: str) -> None:
        if table.setdefault(key, person) != person:
            conflicts.append(
                f"{what}: {key!r} belongs to both {table[key]} and {person}; "
                f"kept {table[key]}"
            )

    for source in sources:
        partner = source.config
        person = partner.slug
        binding = f"{GITHUB_CHANNEL}:{repo}?labels={_exact_glob(partner.label)}"
        if binding not in bindings:
            bindings.append(binding)
        for login in partner.github_logins:
            assign(people, f"{GITHUB_CHANNEL}:{login}", person, what="policy.people")
        roles[person] = PARTNER_ROLE
        assign(claim_labels, partner.label, person, what="policy.claim_labels")
        if partner.notify_login:
            notify[person] = f"{GITHUB_CHANNEL}:{partner.notify_login}"
        mode = _reply_mode(partner, warnings=warnings)
        if default_reply_mode is None:
            default_reply_mode = mode
        elif mode != default_reply_mode:
            reply_modes[person] = mode
    return bindings, {
        "default_reply_mode": default_reply_mode,
        "reply_modes": InlineTable(reply_modes) if reply_modes else None,
        "people": InlineTable(people),
        "roles": InlineTable(roles),
        "relays": [f"{GITHUB_CHANNEL}:{owner_login}"],
        "claim_labels": InlineTable(claim_labels),
        "notify": InlineTable(notify),
    }


def _exact_glob(value: str) -> str:
    """``value`` as a binding condition's glob (``labels=``, ``author=``) matching it only.

    correspond splits conditions on ``&``, decodes ``%``-escapes, then matches a glob, so
    glob characters are bracketed and ``%`` and ``&`` are percent-encoded.
    """
    globbed = re.sub(r"([*?\[])", r"[\1]", value)
    return globbed.replace("%", "%25").replace("&", "%26")


def _briefs(sources: list[_PartnerSource]) -> tuple[Optional[str], dict[str, str]]:
    """Each partner's brief, keyed by partner slug, and the one they all share, if they do.

    A run reads its reporter's own brief first (see
    :meth:`~liaise.subjects.Subject.brief_for`), so partners sharing a repo keep their
    briefs apart. A shared brief becomes the subject's ``brief`` only when every partner
    has it.
    """
    briefs = {s.config.slug: s.config.brief for s in sources if s.config.brief}
    distinct = set(briefs.values())
    everyone = len(briefs) == len(sources) and len(distinct) == 1
    return (distinct.pop() if everyone else None), briefs


def _author_notes(repo: str, sources: list[_PartnerSource]) -> list[str]:
    """For each partner login: what 0.1 no longer picks up, and the binding that would.

    0.0.x took any issue a partner's login opened, labelled or not. 0.1 binds by label,
    so matching the login is the operator's opt-in, never a binding the migration writes.
    """
    return [
        f'issues {login} opens without the "{source.config.label}" label are not '
        f"picked up (0.0.x matched them by author); to opt in, add "
        f'"{GITHUB_CHANNEL}:{repo}?author={_exact_glob(login)}" to bindings'
        for source in sources
        for login in source.config.github_logins
    ]


def _reply_mode(partner: PartnerConfig, *, warnings: list[str]) -> str:
    if partner.reply_mode in REPLY_MODES:
        return partner.reply_mode
    warnings.append(
        f"{partner.slug}: reply_mode {partner.reply_mode!r} is not one of "
        f"{', '.join(REPLY_MODES)}; 0.0.x posted directly for anything but draft, so "
        f"it becomes {POSTING_REPLY_MODE}"
    )
    return POSTING_REPLY_MODE


def _partner_warnings(source: _PartnerSource) -> list[str]:
    """What the operator should check about one partner's migration (``partners/<slug>.toml``)."""
    partner, dispatch = source.config, source.raw.get("dispatch") or {}
    where = f"{partner.slug} ({source.path.name})"
    warnings = []
    if not partner.deploy.strip():
        warnings.append(
            f"{where}: deploy is empty, so 0.0.x moved every landed issue to "
            f'needs-owner. Set delivery.command, or delivery.kind = "pr_only" to '
            f"stop at a pull request."
        )
    if "cwd" not in dispatch:
        warnings.append(
            f"{where}: dispatch.cwd is not set, so 0.0.x worked in whatever directory "
            f"liaise ran from. Set workspace.path to the checkout."
        )
    for key, dflt in (
        ("command", DFLT_DISPATCH_COMMAND),
        ("resume_command", DFLT_RESUME_COMMAND),
    ):
        template = dispatch.get(key, dflt)
        if template == dflt:
            continue
        no_mode = (
            " (this one passed no permission mode at all)"
            if "{permission_mode}" not in template
            and "--permission-mode" not in template
            else ""
        )
        warnings.append(
            f"{where}: dispatch.{key} is custom and is not carried: 0.1 builds the "
            f"claude command itself, with [processor] permission_mode{no_mode}. Carry "
            f"anything else it passed by hand: {template!r}"
        )
    return warnings


def _exists_warning(path: Path) -> str:
    return f"{path} already exists and is left as it is: a subject file is never overwritten"


def _warned(subject: PlannedSubject, warning: Optional[str]) -> PlannedSubject:
    if warning is None or warning in subject.warnings:
        return subject
    return replace(subject, warnings=(*subject.warnings, warning))


def _load_error(path: Path) -> Optional[str]:
    try:
        load_subject(path)
    except ConfigError as error:
        return f"{path} was written but does not load: {error}"
    return None


# ---- TOML writing (the standard library reads TOML but does not write it) ----


class InlineTable(dict):
    """A table :func:`dumps_toml` writes on one line, as ``k = { a = 1 }``, not under a header."""


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}
#: TOML basic strings must escape every control character (tab is in `_ESCAPES`).
_CONTROL_CHARS = frozenset(map(chr, (*range(0x20), 0x7F)))


def dumps_toml(doc: Mapping[str, Any]) -> str:
    """``doc`` as TOML text, which :func:`tomllib.loads` reads back as ``doc``.

    Writes strings (escaped), ints, floats, bools, arrays and tables. A nested mapping
    becomes a ``[section]`` (dotted, as in ``[policy.readiness]``), unless it is an
    :class:`InlineTable` or sits in an array, which are written inline. A key that is not
    bare, such as ``github:pat``, is quoted. ``None`` values are left out, and so is a
    section with nothing left in it.

    >>> print(dumps_toml({
    ...     "bindings": ["github:example/app"],
    ...     "policy": {"people": InlineTable({"github:pat": "pat"}), "note": None},
    ... }), end="")
    bindings = ["github:example/app"]
    <BLANKLINE>
    [policy]
    people = { "github:pat" = "pat" }
    """
    return "\n".join(_table_lines(doc, ())).lstrip("\n") + "\n"


def _is_section(value: Any) -> bool:
    return isinstance(value, Mapping) and not isinstance(value, InlineTable)


def _table_lines(table: Mapping[str, Any], path: tuple[str, ...]) -> list[str]:
    """A table's own ``key = value`` lines under its header, then its sections'."""
    items = [(key, value) for key, value in table.items() if value is not None]
    plain = [f"{_key(k)} = {_value(v)}" for k, v in items if not _is_section(v)]
    nested = [
        line
        for key, value in items
        if _is_section(value)
        for line in _table_lines(value, (*path, key))
    ]
    header = ["", f"[{'.'.join(map(_key, path))}]"] if path and plain else []
    return header + plain + nested


def _key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _string(key)


def _string(text: str) -> str:
    def escaped(char: str) -> str:
        if char in _ESCAPES:
            return _ESCAPES[char]
        return f"\\u{ord(char):04X}" if char in _CONTROL_CHARS else char

    return '"' + "".join(map(escaped, text)) + '"'


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, Mapping):
        pairs = [f"{_key(k)} = {_value(v)}" for k, v in value.items() if v is not None]
        return ("{ " + ", ".join(pairs) + " }") if pairs else "{}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(map(_value, value)) + "]"
    raise TypeError(
        f"dumps_toml cannot write {value!r}: a TOML value is a string, int, float, "
        f"bool, array or table, not a {type(value).__name__}."
    )
