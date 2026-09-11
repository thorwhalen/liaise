"""Holds: stops on work, by scope, set by the operator or by the tick itself.

A hold sits on one scope and stops the work that falls under it. The scopes::

    global              everything
    processor           the processor, for every subject
    effect:<kind>       one kind of effect of finished work, such as effect:deploy
    subject:<slug>      one subject
    person:<id>         one person's cases
    repo:<owner/repo>   one repository
    checkout:<path>     one checkout, in any spelling (compared resolved)

Its mode says what it stops (see :data:`BLOCKING_MODES`):

- ``block``: no new starts. Running runs continue and are collected, and the effects of
  their outcomes wait on the case until unhold.
- ``drain``: no new starts. In-flight work finishes, and its effects execute.
- ``cancel``: no new starts. Running runs are cancelled gracefully, so they can resume,
  and effects wait.

Operator notifications always go out, whatever the mode.

The tick asks :func:`blocking_hold` before starting a case (before budget and preflight),
before executing a finished run's effects, and about each running run. When several
holds apply it gets the most specific one, the narrowest reason there is.

The holds the tick sets itself (``processor`` on ``config_error`` or ``auth_expired``,
``effect:deploy`` on ``effect_blocked``) come from :func:`auto_hold`, recorded with
``set_by="auto:<error class>"``, which also says whether it placed a new hold.
:func:`release_auto_holds` lifts those, and never an operator's hold.

Everything goes through a :class:`~liaise.ledger.Ledger`, so a dry-run ledger
(``Ledger(ChainMap({}, store))``) holds and lifts without touching the real one.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, NamedTuple, Optional

from liaise.ledger import Ledger
from liaise.model import HOLD_MODES, Hold, require_one_of
from liaise.workspace import resolve_checkout

#: Every kind of scope. ``global`` and ``processor`` stand alone; the rest are ``kind:value``.
SCOPE_KINDS = ("global", "subject", "person", "repo", "checkout", "processor", "effect")
#: The scope kinds that take no value.
BARE_SCOPE_KINDS = ("global", "processor")
#: Scope kinds from the most specific to the broadest: the order :func:`blocking_hold` prefers.
SCOPE_SPECIFICITY = (
    "checkout",
    "repo",
    "person",
    "subject",
    "effect",
    "processor",
    "global",
)
#: For each check :func:`blocking_hold` makes, the hold modes that stop it: any hold stops
#: a start, ``drain`` lets the effects of in-flight work execute, and only ``cancel``
#: stops a run already going.
BLOCKING_MODES = MappingProxyType(
    {
        "start": HOLD_MODES,
        "effect": ("block", "cancel"),
        "running": ("cancel",),
    }
)
DFLT_HOLD_MODE = "block"
#: Who :func:`hold` records as having set a hold, unless told otherwise.
DFLT_SET_BY = "operator"
#: How ``set_by`` starts on a hold the tick set itself: ``auto:<error class>``.
AUTO_SET_BY_PREFIX = "auto:"

_VALUE_PLACEHOLDERS = MappingProxyType(
    {
        "subject": "<slug>",
        "person": "<id>",
        "repo": "<owner/repo>",
        "checkout": "<path>",
        "effect": "<kind>",
    }
)
_ACCEPTED_FORMS = ", ".join(
    kind if kind in BARE_SCOPE_KINDS else f"{kind}:{_VALUE_PLACEHOLDERS[kind]}"
    for kind in SCOPE_KINDS
)


def parse_scope(scope: Any) -> tuple[str, Optional[str]]:
    """``(kind, value)`` for a hold scope; the value is None for ``global`` and ``processor``.

    >>> parse_scope("effect:deploy")
    ('effect', 'deploy')
    >>> parse_scope("global")
    ('global', None)

    Only the first ``:`` separates, so a value may hold more (a Windows checkout path).
    Raises ``ValueError`` naming the accepted forms for anything else.
    """
    kind, separator, value = (
        scope.partition(":") if isinstance(scope, str) else ("",) * 3
    )
    if kind in BARE_SCOPE_KINDS and not separator:
        return kind, None
    if kind in _VALUE_PLACEHOLDERS and value.strip():
        return kind, value.strip()
    raise ValueError(
        f"hold scope {scope!r} is not one of the accepted forms: {_ACCEPTED_FORMS}"
    )


def canonical_scope(scope: str) -> str:
    """``scope`` as holds are stored and matched: validated, and a checkout path resolved.

    Raises ``ValueError`` as :func:`parse_scope` does.
    """
    kind, value = parse_scope(scope)
    if value is None:
        return kind
    if kind == "checkout":
        value = str(resolve_checkout(value))
    return f"{kind}:{value}"


def hold(
    ledger: Ledger,
    scope: str,
    *,
    mode: str = DFLT_HOLD_MODE,
    reason: str = "",
    set_by: str = DFLT_SET_BY,
    now: Optional[datetime] = None,
) -> Hold:
    """Put a ``mode`` hold on ``scope``, replacing any hold already there, and return it.

    Raises ``ValueError``, writing nothing, for a scope outside the accepted forms or a
    mode outside ``HOLD_MODES``.
    """
    new = Hold(
        scope=canonical_scope(scope),
        mode=mode,
        reason=reason,
        set_by=set_by,
        set_at=now if now is not None else datetime.now(timezone.utc),
    )
    ledger.set_hold(new)
    return new


def unhold(ledger: Ledger, scope: str) -> bool:
    """Lift the hold on ``scope``, whoever set it; True when there was one.

    Raises ``ValueError`` for a scope outside the accepted forms.
    """
    scope = canonical_scope(scope)
    if ledger.get_hold(scope) is None:
        return False
    ledger.clear_hold(scope)
    return True


def scopes_for(
    *,
    subject: Optional[str] = None,
    person: Optional[str] = None,
    repo: Optional[str] = None,
    checkout: Optional[Any] = None,
    effect: Optional[str] = None,
    processor: bool = True,
) -> tuple[str, ...]:
    """The scopes a piece of work falls under, most specific first and ``global`` last.

    One scope per argument given, plus ``processor`` unless ``processor=False`` (for an
    effect the processor has no part in), plus ``global``.

    >>> scopes_for(person="pat", effect="deploy")
    ('person:pat', 'effect:deploy', 'processor', 'global')

    Raises ``ValueError`` for an empty value.
    """
    values = {
        "checkout": checkout,
        "repo": repo,
        "person": person,
        "subject": subject,
        "effect": effect,
    }
    scopes = []
    for kind in SCOPE_SPECIFICITY:
        if kind == "processor":
            if processor:
                scopes.append(kind)
        elif kind in BARE_SCOPE_KINDS:
            scopes.append(kind)
        elif values[kind] is not None:
            scopes.append(canonical_scope(f"{kind}:{values[kind]}"))
    return tuple(scopes)


def blocking_hold(
    ledger: Ledger, scopes: Iterable[str], *, for_: str
) -> Optional[Hold]:
    """The most specific hold on one of ``scopes`` that stops ``for_``, or None.

    ``for_`` is the check, one of :data:`BLOCKING_MODES`:

    - ``"start"``: may a new run start? Any hold stops it.
    - ``"effect"``: may a finished run's effects execute? ``block`` and ``cancel`` keep
      them waiting, while ``drain`` lets them go.
    - ``"running"``: must a running run be cancelled? Only ``cancel`` says so.

    Build ``scopes`` with :func:`scopes_for`. Raises ``ValueError`` for an unknown check or
    a scope outside the accepted forms.
    """
    check = require_one_of(for_, tuple(BLOCKING_MODES), what="hold check")
    modes = BLOCKING_MODES[check]
    held = map(ledger.get_hold, dict.fromkeys(map(canonical_scope, scopes)))
    stopping = [found for found in held if found is not None and found.mode in modes]
    return min(
        stopping,
        key=lambda found: SCOPE_SPECIFICITY.index(parse_scope(found.scope)[0]),
        default=None,
    )


class AutoHold(NamedTuple):
    """What :func:`auto_hold` did: the hold now in force on the scope, and whether it is new."""

    hold: Hold
    #: True when the scope had no hold before. False when an earlier automatic hold was
    #: replaced, or an operator's hold was kept. The tick tells the operator only when True.
    created: bool


def auto_hold(
    ledger: Ledger, scope: str, *, error_class: str, now: Optional[datetime] = None
) -> AutoHold:
    """Block ``scope`` because the tick met ``error_class``: the hold in force, and whether it is new.

    The hold is recorded with ``set_by="auto:<error_class>"``, so
    :func:`release_auto_holds` can lift it, and it replaces an earlier automatic hold. An
    operator's hold already on the scope is kept, unchanged, and returned instead:
    replacing it would let a later release lift the operator's hold.
    """
    if not error_class:
        raise ValueError(
            "auto_hold needs the error class that caused it, as in "
            "error_class='auth_expired'"
        )
    existing = ledger.get_hold(canonical_scope(scope))
    if existing is not None and not _is_auto(existing):
        return AutoHold(existing, created=False)
    placed = hold(
        ledger,
        scope,
        mode="block",
        reason=f"set automatically after {error_class}",
        set_by=f"{AUTO_SET_BY_PREFIX}{error_class}",
        now=now,
    )
    return AutoHold(placed, created=existing is None)


def release_auto_holds(ledger: Ledger, scope: str) -> list[Hold]:
    """Lift the tick's own hold on ``scope`` and return what was lifted; an operator's stays.

    For the tick to call once the cause has cleared, as when preflight passes again after
    ``auth_expired``.
    """
    scope = canonical_scope(scope)
    existing = ledger.get_hold(scope)
    if existing is None or not _is_auto(existing):
        return []
    ledger.clear_hold(scope)
    return [existing]


def _is_auto(found: Hold) -> bool:
    return (found.set_by or "").startswith(AUTO_SET_BY_PREFIX)
