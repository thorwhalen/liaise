"""Subjects: the bodies of work liaise runs, each loaded from ``subjects/<slug>.toml``.

A subject is one app or site. Its file says which conversations belong to it
(``bindings``, as correspond binding patterns), where the work happens, how it is
delivered, and its policy. The policy covers who is who (``people``), what each person
may do (``roles``, ``permissions``) and at what authenticity grade (``grades``), whose
routing labels count as claims (``relays``, ``claim_labels``), which brief a run reads
for each person (``briefs``), and how replies go out.

**One subject per polled conversation.** correspond keeps one listen cursor per
conversation, so two subjects polling the same one would starve each other:
:func:`load_subjects` refuses that. Several bindings of one subject may share a
conversation (see :func:`poll_ref`).

Like :mod:`liaise.config`, this module knows only the file's shape and defaults.
Every real value lives under ``~/.config/liaise/subjects/``, never in this package.
Every table has defaults, so a minimal subject file needs only::

    bindings = ["github:example/app?labels=partner:pat"]

    [policy]
    people = { "github:pat" = "pat" }
    roles = { pat = "partner" }
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Union

from correspond import check_binding
from correspond.model import Grade

from liaise.config import (
    DFLT_CONFIG_ROOT,
    DFLT_DAILY_DISPATCHES,
    DFLT_DEPLOY_PER,
    DFLT_DEPLOYED_NUDGE_DAYS,
    DFLT_GO_MINUTES,
    DFLT_LABEL_PREFIX,
    DFLT_MAX_TURNS,
    DFLT_PERMISSION_MODE,
    DFLT_QUIET_MINUTES,
    DFLT_REPLY_MODE,
    DFLT_TIMEOUT_MINUTES,
    ConfigError,
    EscalateConfig,
    Markers,
)
from liaise.model import PERMISSIONS, require_one_of

#: Subject files live in this directory under the config root.
DFLT_SUBJECTS_SUBDIR = "subjects"
#: `direct` posts replies to the conversation; `draft` holds them for the operator.
REPLY_MODES = ("direct", "draft")
#: `deploy` runs the delivery command; `pr_only` stops at a pull request.
DELIVERY_KINDS = ("deploy", "pr_only")
#: Where a run works. v0.1 has one checkout, shared by the subject's runs.
WORKSPACE_KINDS = ("shared",)
#: Authenticity grades, weakest first, as correspond names them.
GRADES = tuple(grade.value for grade in Grade)
#: What makes a binding's conversation part a glob, which v0.1 cannot poll ("?" starts
#: a binding's conditions, so it never gets that far).
REF_WILDCARDS = frozenset("*[")

DFLT_WORKSPACE_KIND = "shared"
DFLT_DELIVERY_KIND = "deploy"
DFLT_CONCURRENT_RUNS = 1
DFLT_PUBLIC_CHANNELS = ("github",)
DFLT_ROLE_PERMISSIONS = MappingProxyType(
    {
        "partner": ("report", "request_work", "approve_candidate"),
        "observer": ("report",),
    }
)
DFLT_PERMISSION_GRADES = MappingProxyType(
    {
        "report": ("claimed", "platform", "domain", "bound", "crypto"),
        "request_work": ("platform", "domain", "bound", "crypto"),
        "approve_candidate": ("platform", "domain", "bound", "crypto"),
    }
)

_MINIMAL_SUBJECT = (
    "A minimal subject file needs at least:\n\n"
    '  bindings = ["github:example/app?labels=partner:pat"]\n\n'
    "  [policy]\n"
    '  people = { "github:pat" = "pat" }\n'
    '  roles = { pat = "partner" }\n'
)


@dataclass(frozen=True)
class Workspace:
    """Where a subject's runs do their work."""

    kind: str = DFLT_WORKSPACE_KIND
    path: str = ""


@dataclass(frozen=True)
class Delivery:
    """How finished work reaches the partner: ``deploy`` runs ``command`` per ``per``."""

    kind: str = DFLT_DELIVERY_KIND
    per: str = DFLT_DEPLOY_PER
    command: str = ""


@dataclass(frozen=True)
class ReadinessPolicy:
    """When a case is ready to dispatch (see :mod:`liaise.readiness`)."""

    quiet_minutes: int = DFLT_QUIET_MINUTES
    go_minutes: int = DFLT_GO_MINUTES
    markers: Markers = field(default_factory=Markers)


@dataclass(frozen=True)
class BudgetPolicy:
    """Limits on a subject's runs. Mandatory, never unlimited."""

    concurrent: int = DFLT_CONCURRENT_RUNS
    timeout_minutes: int = DFLT_TIMEOUT_MINUTES
    max_turns: int = DFLT_MAX_TURNS
    daily_dispatches: int = DFLT_DAILY_DISPATCHES


@dataclass(frozen=True)
class ProcessorConfig:
    """How the subject's processor runs."""

    permission_mode: str = DFLT_PERMISSION_MODE


@dataclass(frozen=True)
class Policy:
    """Who is who on a subject, what each may do, and how liaise answers them.

    ``people`` maps a channel address (``github:pat``) to a person id and ``roles`` a
    person to a role. ``permissions`` maps a role to the permissions it grants, and
    ``grades`` a permission to the authenticity grades it accepts. ``relays`` are
    authors whose ``claim_labels`` (routing label to person) count as claims. See
    :mod:`liaise.access`.
    """

    people: Mapping[str, str]
    roles: Mapping[str, str]
    default_reply_mode: str = DFLT_REPLY_MODE
    reply_modes: Mapping[str, str] = field(default_factory=dict)
    relays: tuple[str, ...] = ()
    claim_labels: Mapping[str, str] = field(default_factory=dict)
    notify: Mapping[str, str] = field(default_factory=dict)
    leak_terms: tuple[str, ...] = ()
    public_channels: tuple[str, ...] = DFLT_PUBLIC_CHANNELS
    permissions: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DFLT_ROLE_PERMISSIONS)
    )
    grades: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DFLT_PERMISSION_GRADES)
    )
    readiness: ReadinessPolicy = field(default_factory=ReadinessPolicy)
    escalate: EscalateConfig = field(default_factory=EscalateConfig)
    budget: BudgetPolicy = field(default_factory=BudgetPolicy)
    #: Days a `deployed` case may stay quiet before the partner is nudged, once.
    deployed_nudge_days: int = DFLT_DEPLOYED_NUDGE_DAYS
    #: Person id to the brief a run on their case reads (see :meth:`Subject.brief_for`).
    briefs: Mapping[str, str] = field(default_factory=dict)


def address_key(address: str) -> str:
    """How two channel addresses compare: without regard to case.

    GitHub logins are case-insensitive, so ``github:Pat`` is ``github:pat``.

    >>> address_key("github:Pat") == address_key("github:pat")
    True
    """
    return address.casefold()


def poll_ref(binding: str) -> Optional[str]:
    """The conversation ``binding`` is polled on, or None when v0.1 cannot poll it.

    That is the binding without its ``?conditions``. A conversation part holding a
    wildcard is a glob, not a conversation, so it gives None.

    >>> poll_ref("github:example/app?labels=partner:pat")
    'github:example/app'
    >>> poll_ref("github:example/*") is None
    True
    """
    ref = binding.partition("?")[0]
    return None if REF_WILDCARDS & set(ref) else ref


def ref_key(ref: str) -> str:
    """How two polled conversations compare: without regard to case, as their cursors do.

    correspond keys a cursor on the reference its adapter normalizes. GitHub's lower-cases
    it, and a web inbox's site names are lower-case, so ``github:Example/App`` is polled
    on the cursor of ``github:example/app``.

    >>> ref_key("github:Example/App") == ref_key("github:example/app")
    True
    """
    return ref.casefold()


@dataclass(frozen=True)
class Subject:
    """A resolved subject: ``subjects/<slug>.toml`` with every default applied."""

    slug: str
    bindings: tuple[str, ...]
    policy: Policy
    display_name: str = ""
    workspace: Workspace = field(default_factory=Workspace)
    brief: str = ""
    verify: str = ""
    delivery: Delivery = field(default_factory=Delivery)
    label_prefix: str = DFLT_LABEL_PREFIX
    processor: ProcessorConfig = field(default_factory=ProcessorConfig)
    #: The file this subject was loaded from, when it was.
    source: Optional[str] = None

    def reply_mode_for(self, person: Optional[str]) -> str:
        """``direct`` or ``draft``: the person's override, else the subject's default."""
        return self.policy.reply_modes.get(person, self.policy.default_reply_mode)

    def brief_for(self, person: Optional[str]) -> Optional[str]:
        """The brief a run on ``person``'s case reads: theirs, else the subject's, else None.

        ``policy.briefs[person]`` when set, else ``brief``. An empty path counts as unset.
        """
        return self.policy.briefs.get(person) or self.brief or None

    def notify_address_for(
        self, person: str, *, channel: Optional[str] = None
    ) -> Optional[str]:
        """The address to notify ``person`` at, or None when the policy has none.

        ``policy.notify[person]`` when set, else the first ``policy.people`` address that
        maps to ``person``. ``channel`` (as in ``github``) keeps only addresses on that
        channel, so a GitHub mention is never handed a web-inbox user id.
        """
        candidates = (
            self.policy.notify.get(person),
            *(address for address, who in self.policy.people.items() if who == person),
        )
        return next(
            (
                address
                for address in candidates
                if address and (channel is None or address.partition(":")[0] == channel)
            ),
            None,
        )

    def permissions_for(self, role: Optional[str]) -> tuple[str, ...]:
        """The permissions ``role`` grants on this subject (none for an unknown role)."""
        return tuple(self.policy.permissions.get(role, ()))

    def accepts(self, permission: str, grade: Union[Grade, str]) -> bool:
        """Whether ``permission`` may be used at authenticity ``grade`` on this subject."""
        require_one_of(permission, PERMISSIONS, what="permission")
        return str(getattr(grade, "value", grade)) in self.policy.grades.get(
            permission, ()
        )


def check_bindings(
    subject: Subject, *, registry: Optional[Mapping[str, Any]] = None
) -> list[str]:
    """What would make one of ``subject``'s bindings never match; empty when nothing would.

    Runs :func:`correspond.check_binding` on each binding, which reports a pattern with
    no channel, an unknown channel, or a condition on a field that channel's messages
    never carry. ``registry`` is correspond's channel registry (its default when None).
    Each problem names the subject's file and the binding.
    """
    where = subject.source or subject.slug
    return [
        f"{where}: binding {pattern!r}: {problem}"
        for pattern in subject.bindings
        for problem in check_binding(pattern, registry=registry)
    ]


def load_subject(path: Union[str, os.PathLike]) -> Subject:
    """Load and resolve one subject file into a :class:`Subject`, its slug the file's stem.

    Raises :class:`~liaise.config.ConfigError` naming the file and the fix for a missing
    file, invalid TOML, a missing ``bindings``, ``policy.people`` or ``policy.roles``,
    and any value outside its vocabulary (reply modes, permissions, grades, roles).
    """
    path = Path(path)
    raw = _read_toml(path)
    if "bindings" not in raw:
        raise ConfigError(
            f"{path} is missing required field 'bindings'. {_MINIMAL_SUBJECT}"
        )
    bindings = _strings(raw, "bindings", path=path, dotted="bindings")
    if not bindings:
        raise ConfigError(
            f"{path}: bindings is empty, so no conversation would ever reach this "
            f"subject. {_MINIMAL_SUBJECT}"
        )
    workspace = _table(raw, "workspace", path=path, dotted="workspace")
    delivery = _table(raw, "delivery", path=path, dotted="delivery")
    processor = _table(raw, "processor", path=path, dotted="processor")
    return Subject(
        slug=path.stem,
        bindings=bindings,
        policy=_policy_from(
            _table(raw, "policy", path=path, dotted="policy"), path=path
        ),
        display_name=raw.get("display_name", path.stem),
        workspace=Workspace(
            kind=_choice(
                workspace.get("kind", DFLT_WORKSPACE_KIND),
                WORKSPACE_KINDS,
                path=path,
                dotted="workspace.kind",
            ),
            path=workspace.get("path", ""),
        ),
        brief=raw.get("brief", ""),
        verify=raw.get("verify", ""),
        delivery=Delivery(
            kind=_choice(
                delivery.get("kind", DFLT_DELIVERY_KIND),
                DELIVERY_KINDS,
                path=path,
                dotted="delivery.kind",
            ),
            per=delivery.get("per", DFLT_DEPLOY_PER),
            command=delivery.get("command", ""),
        ),
        label_prefix=raw.get("label_prefix", DFLT_LABEL_PREFIX),
        processor=ProcessorConfig(
            permission_mode=processor.get("permission_mode", DFLT_PERMISSION_MODE)
        ),
        source=str(path),
    )


def load_subjects(root: Optional[Path] = None) -> dict[str, Subject]:
    """Every subject under ``<root>/subjects/*.toml``, keyed by slug, in slug order.

    ``root`` defaults to ``~/.config/liaise``. No ``subjects`` directory means no subjects.
    Raises :class:`~liaise.config.ConfigError` for a file that does not load, and, naming
    both files, for two subjects whose bindings are polled on the same conversation.
    """
    root = Path(root) if root is not None else DFLT_CONFIG_ROOT
    subjects_dir = root / DFLT_SUBJECTS_SUBDIR
    if not subjects_dir.is_dir():
        return {}
    subjects = map(load_subject, sorted(subjects_dir.glob("*.toml")))
    subjects = {subject.slug: subject for subject in subjects}
    _refuse_shared_poll_refs(subjects.values())
    return subjects


def _refuse_shared_poll_refs(subjects: Iterable[Subject]) -> None:
    """Raise :class:`ConfigError` when two subjects' bindings are polled on one conversation.

    Conversations compare as :func:`ref_key` says. A binding :func:`poll_ref` cannot poll
    is not polled, so it shares nothing.
    """
    polled_by: dict[str, tuple[Subject, str]] = {}
    for subject in subjects:
        for binding in subject.bindings:
            ref = poll_ref(binding)
            if ref is None:
                continue
            owner, owner_binding = polled_by.setdefault(
                ref_key(ref), (subject, binding)
            )
            if owner.slug != subject.slug:
                raise ConfigError(
                    f"{owner.source or owner.slug} and {subject.source or subject.slug} "
                    f"both poll {ref} (bindings {owner_binding!r} and {binding!r}). "
                    f"correspond keeps one cursor per conversation, so the first subject "
                    f"to poll it would take every event and the other would hear none. "
                    f"Bind {ref} in one subject only: that subject may hold several "
                    f"bindings on it."
                )


# ---- loading helpers: each error names the file and what would fix it ----


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Missing subject file: {path}. {_MINIMAL_SUBJECT}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(
            f"{path} is not valid TOML: {error}. A key holding ':' must be quoted, "
            'as in people = { "github:pat" = "pat" }.'
        ) from None


def _table(
    raw: Mapping[str, Any], name: str, *, path: Path, dotted: str
) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(
            f"{path}: {dotted} must be a table, as in [{dotted}]; got {value!r}."
        )
    return value


def _strings(
    raw: Mapping[str, Any],
    name: str,
    *,
    path: Path,
    dotted: str,
    default: tuple[str, ...] = (),
) -> tuple[str, ...]:
    value = raw.get(name, list(default))
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(
            f'{path}: {dotted} must be a list of strings, as in {name} = ["..."]; '
            f"got {value!r}."
        )
    return tuple(value)


def _string_table(
    raw: Mapping[str, Any], name: str, *, path: Path, dotted: str
) -> dict[str, str]:
    value = _table(raw, name, path=path, dotted=dotted)
    if not all(isinstance(v, str) for v in value.values()):
        raise ConfigError(
            f"{path}: every value in {dotted} must be a string, as in "
            f'{name} = {{ key = "value" }}; got {value!r}.'
        )
    return dict(value)


def _choice(value: Any, allowed: tuple[str, ...], *, path: Path, dotted: str) -> str:
    if value not in allowed:
        raise ConfigError(
            f"{path}: {dotted} has {value!r}, which is not one of: "
            f"{', '.join(allowed)}."
        )
    return value


def _policy_from(raw: Mapping[str, Any], *, path: Path) -> Policy:
    for required in ("people", "roles"):
        if required not in raw:
            raise ConfigError(
                f"{path} is missing required field 'policy.{required}'. "
                f"{_MINIMAL_SUBJECT}"
            )

    permissions = dict(DFLT_ROLE_PERMISSIONS)
    permissions_raw = _table(raw, "permissions", path=path, dotted="policy.permissions")
    for role in permissions_raw:
        dotted = f"policy.permissions.{role}"
        permissions[role] = _strings(permissions_raw, role, path=path, dotted=dotted)
        for permission in permissions[role]:
            _choice(permission, PERMISSIONS, path=path, dotted=dotted)

    grades = dict(DFLT_PERMISSION_GRADES)
    grades_raw = _table(raw, "grades", path=path, dotted="policy.grades")
    for permission in grades_raw:
        _choice(permission, PERMISSIONS, path=path, dotted="policy.grades")
        dotted = f"policy.grades.{permission}"
        grades[permission] = _strings(grades_raw, permission, path=path, dotted=dotted)
        for grade in grades[permission]:
            _choice(grade, GRADES, path=path, dotted=dotted)

    roles = _string_table(raw, "roles", path=path, dotted="policy.roles")
    for person, role in roles.items():
        if role not in permissions:
            raise ConfigError(
                f"{path}: policy.roles gives {person!r} the role {role!r}, which "
                f"policy.permissions does not define (it defines "
                f"{', '.join(sorted(permissions))}). Add it under "
                f'[policy.permissions], as in {role} = ["report"].'
            )

    claim_labels = _string_table(
        raw, "claim_labels", path=path, dotted="policy.claim_labels"
    )
    for label, person in claim_labels.items():
        if person not in roles:
            raise ConfigError(
                f"{path}: policy.claim_labels maps {label!r} to {person!r}, who has "
                f"no entry in policy.roles, so that claim could never be authorized. "
                f"Give {person!r} a role, or remove the label."
            )

    briefs = _string_table(raw, "briefs", path=path, dotted="policy.briefs")
    for person in briefs:
        if person not in roles:
            raise ConfigError(
                f"{path}: policy.briefs has a brief for {person!r}, who has no entry in "
                f"policy.roles, so no case could ever be theirs. Give {person!r} a role, "
                f"or remove the brief."
            )

    reply_modes = _string_table(
        raw, "reply_modes", path=path, dotted="policy.reply_modes"
    )
    for mode in reply_modes.values():
        _choice(mode, REPLY_MODES, path=path, dotted="policy.reply_modes")

    people = _string_table(raw, "people", path=path, dotted="policy.people")
    first_spelling: dict[str, str] = {}
    for address, person in people.items():
        other = first_spelling.setdefault(address_key(address), address)
        if people[other] != person:
            raise ConfigError(
                f"{path}: policy.people maps {other!r} to {people[other]!r} and "
                f"{address!r} to {person!r}. Addresses match without regard to case, "
                f"so these are one address: keep one of them."
            )

    readiness = _table(raw, "readiness", path=path, dotted="policy.readiness")
    markers = _string_table(
        readiness, "markers", path=path, dotted="policy.readiness.markers"
    )
    escalate = _table(raw, "escalate", path=path, dotted="policy.escalate")
    budget = _table(raw, "budget", path=path, dotted="policy.budget")
    dflt_markers, dflt_escalate = Markers(), EscalateConfig()

    deployed_nudge_days = raw.get("deployed_nudge_days", DFLT_DEPLOYED_NUDGE_DAYS)
    if (
        isinstance(deployed_nudge_days, bool)
        or not isinstance(deployed_nudge_days, int)
        or deployed_nudge_days < 1
    ):
        raise ConfigError(
            f"{path}: policy.deployed_nudge_days must be a whole number of days, 1 or "
            f"more, as in deployed_nudge_days = {DFLT_DEPLOYED_NUDGE_DAYS}; "
            f"got {deployed_nudge_days!r}."
        )

    return Policy(
        people=people,
        roles=roles,
        default_reply_mode=_choice(
            raw.get("default_reply_mode", DFLT_REPLY_MODE),
            REPLY_MODES,
            path=path,
            dotted="policy.default_reply_mode",
        ),
        reply_modes=reply_modes,
        relays=_strings(raw, "relays", path=path, dotted="policy.relays"),
        claim_labels=claim_labels,
        notify=_string_table(raw, "notify", path=path, dotted="policy.notify"),
        leak_terms=_strings(raw, "leak_terms", path=path, dotted="policy.leak_terms"),
        public_channels=_strings(
            raw,
            "public_channels",
            path=path,
            dotted="policy.public_channels",
            default=DFLT_PUBLIC_CHANNELS,
        ),
        permissions=permissions,
        grades=grades,
        readiness=ReadinessPolicy(
            quiet_minutes=readiness.get("quiet_minutes", DFLT_QUIET_MINUTES),
            go_minutes=readiness.get("go_minutes", DFLT_GO_MINUTES),
            markers=Markers(
                go=markers.get("go", dflt_markers.go),
                wait=markers.get("wait", dflt_markers.wait),
            ),
        ),
        escalate=EscalateConfig(
            money_usd=escalate.get("money_usd", dflt_escalate.money_usd),
            max_scope=escalate.get("max_scope", dflt_escalate.max_scope),
        ),
        budget=BudgetPolicy(
            concurrent=budget.get("concurrent", DFLT_CONCURRENT_RUNS),
            timeout_minutes=budget.get("timeout_minutes", DFLT_TIMEOUT_MINUTES),
            max_turns=budget.get("max_turns", DFLT_MAX_TURNS),
            daily_dispatches=budget.get("daily_dispatches", DFLT_DAILY_DISPATCHES),
        ),
        deployed_nudge_days=deployed_nudge_days,
        briefs=briefs,
    )
