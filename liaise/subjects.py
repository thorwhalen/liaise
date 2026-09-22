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

**An inert subject.** ``active = false`` declares a subject that no tick acts on. It is
loaded, validated, shown and used as gate context, but :func:`liaise.tick.run_once` polls
none of its bindings and starts, delivers, nudges and labels none of its cases, and
``liaise setup`` refuses it. It is part of the file, not a hold, so ``liaise unhold
global`` does not lift it, and a reader of the file sees it.

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
from typing import Any, Collection, Iterable, Mapping, Optional, Union

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
from liaise.model import CASE_STATES, PERMISSIONS, require_one_of
from liaise.policy import ENFORCE, MODES, TAINTED_RUNS_APPROVE, TAINTED_RUNS_SEND

#: What ``policy.tainted_runs`` may say: ``approve`` (a run that read untrusted input needs
#: the operator for any audience wider than them) or ``send`` (the subject waives that).
TAINTED_RUNS = (TAINTED_RUNS_APPROVE, TAINTED_RUNS_SEND)

#: Minutes a ``delay`` verdict holds a message in the outbox before the tick sends it
#: (``policy.delay_minutes``; liaise #38); 0 sends it at once. Unset by default: the outbox
#: sends without a person, so a subject turns it on explicitly, and until then a ``delay``
#: waits for the operator as a draft, as it did before the outbox existed.
DFLT_DELAY_MINUTES = None
#: The window a subject that turns the outbox on is advised to use (liaise ADR 0003).
RECOMMENDED_DELAY_MINUTES = 10
#: Minutes past its release after which a held message goes to the operator instead of out
#: (``policy.delay_stale_minutes``): nobody watched the window it relied on. 0 never lapses.
DFLT_DELAY_STALE_MINUTES = 24 * 60

#: Subject files live in this directory under the config root.
DFLT_SUBJECTS_SUBDIR = "subjects"
#: `direct` posts replies to the conversation; `draft` holds them for the operator.
REPLY_MODES = ("direct", "draft")
#: `deploy` runs the delivery command; `pr_only` stops at a pull request.
DELIVERY_KINDS = ("deploy", "pr_only")
#: When a `deploy` runs its command: `batch` once per tick, for every case delivered in
#: it; `issue` for each case, right after that case's outcomes.
DELIVERY_PERS = ("batch", "issue")
#: Where a run works. v0.1 has one checkout, shared by the subject's runs.
WORKSPACE_KINDS = ("shared",)
#: Authenticity grades, weakest first, as correspond names them.
GRADES = tuple(grade.value for grade in Grade)
#: What makes a binding's conversation part a glob, which v0.1 cannot poll ("?" starts
#: a binding's conditions, so it never gets that far).
REF_WILDCARDS = frozenset("*[")
#: Channels whose conversation references ignore case, so their bindings load lower-cased
#: (see :func:`normalize_binding`).
CASE_INSENSITIVE_REF_CHANNELS = ("github",)

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
    """How finished work reaches the partner.

    ``deploy`` runs ``command`` in the workspace: once per tick for every case delivered
    in it (``per = "batch"``), or for each case right after its outcomes
    (``per = "issue"``). Nothing tells the partner it is live without a run that
    succeeded. ``pr_only`` stops at a pull request and runs nothing.
    """

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
        :mod:`liaise.access`. ``waiting_labels`` (person to label) is the mirror of
        ``claim_labels``: a label liaise writes on a case's issues while the case waits on that
        person, where a claim label is one it reads (see :mod:`liaise.projection`).

        The outbound gate's policy (liaise ADR 0002) reads four more: ``tainted_runs``
        (``approve``, or ``send`` to waive the taint rule), ``link_allowlist`` (hosts a link
        may point at besides the channel's own), ``canary_terms`` (terms planted in private
        context, never to be sent) and ``mode`` (``enforce``, or ``shadow``, recorded on every
        verdict, counted by ``liaise gate report``, and enforced alike until its sending
        semantics are decided, liaise #51). ``delay_minutes`` turns the delay
    outbox on (liaise #38): how long a ``delay`` verdict (an irreversible send to an
    organisation-wide or public place) waits, cancellable, before the tick sends it; 0 sends
    it at once, and None (the default) keeps it for the operator as a draft. A held
    message the tick reaches more than ``delay_stale_minutes`` after its release goes to the
    operator as a draft instead (0: never).
    ``leak_terms`` are scanned for as
        a label no reader is cleared for; ``public_channels`` is still read, and decides
        nothing: the audience correspond computes does. Both go after one release.
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
    #: Person id to the label a case's issues carry while the case waits on them.
    waiting_labels: Mapping[str, str] = field(default_factory=dict)
    tainted_runs: str = TAINTED_RUNS_APPROVE
    link_allowlist: tuple[str, ...] = ()
    canary_terms: tuple[str, ...] = ()
    mode: str = ENFORCE
    delay_minutes: Optional[int] = DFLT_DELAY_MINUTES
    delay_stale_minutes: int = DFLT_DELAY_STALE_MINUTES


#: Each person's waiting label when a subject sets ``policy.waiting_labels = true``.
DFLT_WAITING_LABEL = "needs-{person}"


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


def normalize_binding(binding: str) -> str:
    """``binding`` as a subject keeps it: on a GitHub channel, its channel and conversation lower-cased.

    GitHub references ignore case, but correspond's ``binding_matches`` compares a binding's
    conversation case-sensitively with the lower-cased reference its GitHub adapter gives a
    message, so ``github:Example/App`` would match nothing. The ``?conditions`` stay as
    written, and a binding on any other channel is returned unchanged.

    >>> normalize_binding("github:Example/App?labels=partner:Pat")
    'github:example/app?labels=partner:Pat'
    >>> normalize_binding("webinbox:Example-Site")
    'webinbox:Example-Site'
    """
    head, separator, conditions = binding.partition("?")
    if head.partition(":")[0].casefold() in CASE_INSENSITIVE_REF_CHANNELS:
        head = head.lower()
    return head + separator + conditions


def ref_key(ref: str) -> str:
    """How two polled conversations compare: without regard to case, as their cursors do.

    correspond keys a cursor on the reference its adapter normalizes. GitHub's lower-cases
    it, and a web inbox's site names are lower-case, so ``github:Example/App`` is polled
    on the cursor of ``github:example/app``.

    >>> ref_key("github:Example/App") == ref_key("github:example/app")
    True
    """
    return ref.casefold()


#: What joins an issue's number to its repository in a GitHub reference.
_ISSUE_SEPARATOR = "#"


def subject_for_ref(subjects: Mapping[str, Subject], ref: str) -> Subject:
    """The subject whose bindings take in ``ref``, the conversation a message outside a case goes to.

    A binding takes in the conversation it polls and, on GitHub, every issue of a repository
    it binds, so ``github:example/app#12`` is the subject's that binds
    ``github:example/app``. Conversations compare as :func:`ref_key` says. A binding's
    ``?conditions`` are not consulted, since they sort what comes in, not where a message
    may go, and a wildcard binding polls nothing, so it takes in nothing (see
    :func:`poll_ref`). When two subjects take ``ref`` in, the one whose binding names it most
    closely wins: an issue's own binding over its repository's.

    No caller may choose another subject: the subject's policy is what the gate judges a
    message by, so a message goes only where its subject binds.

    >>> heron = Subject("heron", ("github:example/heron?labels=partner:pat",),
    ...     Policy(people={}, roles={}))
    >>> subject_for_ref({"heron": heron}, "github:Example/Heron#12").slug
    'heron'

    Raises ``ValueError`` naming the subjects there are when none takes ``ref`` in, and
    naming each when several name it equally closely.
    """
    key = ref_key(ref)
    closeness: dict[str, int] = {}
    for slug, subject in subjects.items():
        for binding in subject.bindings:
            polled = poll_ref(binding)
            if polled is None:
                continue
            polled_key = ref_key(polled)
            github = polled_key.partition(":")[0] in CASE_INSENSITIVE_REF_CHANNELS
            if key == polled_key:
                score = 2
            elif github and key.startswith(polled_key + _ISSUE_SEPARATOR):
                score = 1
            else:
                continue
            closeness[slug] = max(closeness.get(slug, 0), score)
    if not closeness:
        known = ", ".join(sorted(subjects)) or "(none)"
        raise ValueError(
            f"no subject binds {ref}, so there is no policy to judge a message to it by "
            f"(the subjects are: {known}); bind it in a subject's file first"
        )
    best = max(closeness.values())
    closest = sorted(slug for slug, score in closeness.items() if score == best)
    if len(closest) > 1:
        raise ValueError(
            f"{ref} is bound equally closely by the subjects {', '.join(closest)}; bind it "
            f"in one of them only"
        )
    return subjects[closest[0]]


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
    #: False for a subject declared but inert: loaded, shown and used as gate context, but
    #: no tick polls, starts, delivers, nudges or labels anything of it.
    active: bool = True

    def reply_mode_for(self, person: Optional[str]) -> str:
        """``direct`` or ``draft``: the person's override, else the subject's default."""
        return self.policy.reply_modes.get(person, self.policy.default_reply_mode)

    def brief_for(self, person: Optional[str]) -> Optional[str]:
        """The brief a run on ``person``'s case reads: theirs, else the subject's, else None.

        ``policy.briefs[person]`` when set, else ``brief``. An empty path counts as unset.
        """
        return self.policy.briefs.get(person) or self.brief or None

    def notify_addresses_for(
        self, person: str, *, channels: Union[str, Collection[str], None] = None
    ) -> tuple[str, ...]:
        """Every address ``person`` can be notified at on this subject, best first.

        ``policy.notify[person]`` comes first when set, then each ``policy.people``
        address that maps to ``person``, in file order, each once. A string with no
        channel part (the ``github`` of ``github:pat``) is not an address, so it is left
        out. ``channels``, one channel or several, keeps only the addresses on them, so a
        GitHub mention is never handed a web-inbox user id.
        """
        wanted = (channels,) if isinstance(channels, str) else channels

        def is_wanted(address: str) -> bool:
            channel, separator, _ = address.partition(":")
            return bool(separator and channel) and (wanted is None or channel in wanted)

        policy = self.policy
        explicit = (policy.notify[person],) if person in policy.notify else ()
        handles = (address for address, who in policy.people.items() if who == person)
        return tuple(filter(is_wanted, dict.fromkeys((*explicit, *handles))))

    def notify_address_for(
        self, person: str, *, channels: Union[str, Collection[str], None] = None
    ) -> Optional[str]:
        """The best address to notify ``person`` at, or None when the policy has none.

        That is the first of :meth:`notify_addresses_for`, which says how ``channels``
        filters.
        """
        return next(iter(self.notify_addresses_for(person, channels=channels)), None)

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

    Each binding is kept as :func:`normalize_binding` gives it. Raises
    :class:`~liaise.config.ConfigError` naming the file and the fix for a missing file,
    invalid TOML, a missing ``bindings``, ``policy.people`` or ``policy.roles``, an
    ``active`` that is not true or false, and any value outside its vocabulary (workspace
    and delivery kinds, ``delivery.per``, reply modes, permissions, grades, roles).
    """
    path = Path(path)
    raw = _read_toml(path)
    if "bindings" not in raw:
        raise ConfigError(
            f"{path} is missing required field 'bindings'. {_MINIMAL_SUBJECT}"
        )
    bindings = tuple(
        map(
            normalize_binding,
            _strings(raw, "bindings", path=path, dotted="bindings"),
        )
    )
    if not bindings:
        raise ConfigError(
            f"{path}: bindings is empty, so no conversation would ever reach this "
            f"subject. {_MINIMAL_SUBJECT}"
        )
    workspace = _table(raw, "workspace", path=path, dotted="workspace")
    delivery = _table(raw, "delivery", path=path, dotted="delivery")
    processor = _table(raw, "processor", path=path, dotted="processor")
    policy = _policy_from(_table(raw, "policy", path=path, dotted="policy"), path=path)
    label_prefix = raw.get("label_prefix", DFLT_LABEL_PREFIX)
    state_labels = {f"{label_prefix}{state}" for state in CASE_STATES}
    clashing = sorted(set(policy.waiting_labels.values()) & state_labels)
    if clashing:
        raise ConfigError(
            f"{path}: policy.waiting_labels uses {', '.join(map(repr, clashing))}, which "
            f"is a state label, so projecting a state would take it off. Choose another "
            f"label."
        )
    return Subject(
        slug=path.stem,
        bindings=bindings,
        policy=policy,
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
            per=_choice(
                delivery.get("per", DFLT_DEPLOY_PER),
                DELIVERY_PERS,
                path=path,
                dotted="delivery.per",
            ),
            command=delivery.get("command", ""),
        ),
        label_prefix=label_prefix,
        processor=ProcessorConfig(
            permission_mode=processor.get("permission_mode", DFLT_PERMISSION_MODE)
        ),
        source=str(path),
        active=_boolean(raw, "active", default=True, path=path, dotted="active"),
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


def _boolean(
    raw: Mapping[str, Any], name: str, *, default: bool, path: Path, dotted: str
) -> bool:
    value = raw.get(name, default)
    if not isinstance(value, bool):
        raise ConfigError(
            f"{path}: {dotted} must be true or false, as in {name} = false; got {value!r}."
        )
    return value


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

    waiting_labels = _waiting_labels(
        raw, roles=roles, claim_labels=claim_labels, path=path
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

    def minutes(key: str, default: Optional[int], example: int) -> Optional[int]:
        value = raw.get(key, default)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError(
                f"{path}: policy.{key} must be a whole number of minutes, 0 or more, "
                f"as in {key} = {example}; got {value!r}."
            )
        return value

    delay_minutes = minutes(
        "delay_minutes", DFLT_DELAY_MINUTES, RECOMMENDED_DELAY_MINUTES
    )
    delay_stale_minutes = minutes(
        "delay_stale_minutes", DFLT_DELAY_STALE_MINUTES, DFLT_DELAY_STALE_MINUTES
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
        waiting_labels=waiting_labels,
        tainted_runs=_choice(
            raw.get("tainted_runs", TAINTED_RUNS_APPROVE),
            TAINTED_RUNS,
            path=path,
            dotted="policy.tainted_runs",
        ),
        link_allowlist=_strings(
            raw, "link_allowlist", path=path, dotted="policy.link_allowlist"
        ),
        canary_terms=_strings(
            raw, "canary_terms", path=path, dotted="policy.canary_terms"
        ),
        mode=_choice(raw.get("mode", ENFORCE), MODES, path=path, dotted="policy.mode"),
        delay_minutes=delay_minutes,
        delay_stale_minutes=delay_stale_minutes,
    )


def _waiting_labels(
    raw: Mapping[str, Any],
    *,
    roles: Mapping[str, str],
    claim_labels: Mapping[str, str],
    path: Path,
) -> dict[str, str]:
    """``policy.waiting_labels``: off, a table of person to label, or ``true`` for everyone.

    ``true`` gives every person with a role :data:`DFLT_WAITING_LABEL`. Each label must be
    a person's with a role, not empty, not another person's, and not a claim label, which
    projecting it would take off.
    """
    dotted = "policy.waiting_labels"
    value = raw.get("waiting_labels", False)
    if value is False:
        return {}
    if value is True:
        labels = {person: DFLT_WAITING_LABEL.format(person=person) for person in roles}
    elif isinstance(value, dict):
        labels = _string_table(raw, "waiting_labels", path=path, dotted=dotted)
    else:
        raise ConfigError(
            f"{path}: {dotted} must be true, false or a table of person to label, as in "
            f'waiting_labels = {{ pat = "needs-pat" }}; got {value!r}.'
        )
    owners: dict[str, str] = {}
    for person, label in labels.items():
        if person not in roles:
            raise ConfigError(
                f"{path}: {dotted} has a label for {person!r}, who has no entry in "
                f"policy.roles, so no case could ever wait on them. Give {person!r} a "
                f"role, or remove the label."
            )
        if not label.strip():
            raise ConfigError(f"{path}: {dotted} gives {person!r} an empty label.")
        if label in claim_labels:
            raise ConfigError(
                f"{path}: {dotted} gives {person!r} the label {label!r}, which is also a "
                f"claim label, so projecting it would take the claim off. Choose another "
                f"label."
            )
        other = owners.setdefault(label, person)
        if other != person:
            raise ConfigError(
                f"{path}: {dotted} gives {other!r} and {person!r} the same label "
                f"{label!r}, so it could not say which of them a case waits on."
            )
    return labels
