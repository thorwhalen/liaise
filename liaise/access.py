"""Identity and access: who a message is from, and whether they may do what it asks.

**Resolving a sender.** :func:`resolve_person` maps a channel address (``github:pat``)
to a person id. It looks in the subject's ``policy.people`` first, then in acquaint's
people records when acquaint is installed (``liaise[people]``). Any failure there
resolves to nobody; it never raises.

**Label-as-claim.** A routing label is only a claim, since anyone who can label an
issue can write one. :func:`authorize` attributes a message:

1. to its author, by handle, when the author resolves to a person with a role on the
   subject, whatever labels the message carries;
2. otherwise to the person a ``policy.claim_labels`` label names, but only when the
   author is one of ``policy.relays`` (an app filing issues for its users), and at
   the relay's own authenticity grade;
3. otherwise to no one. A claim label from any other author is refused as
   ``label claim by an untrusted author``.

The attributed person's role must grant the permission, at a grade ``policy.grades``
accepts for it.

**Addresses compare without regard to case** (see :func:`liaise.subjects.address_key`),
in ``policy.people`` and ``policy.relays`` alike: GitHub logins are case-insensitive, so
``github:Pat`` is the person ``github:pat`` is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from correspond.model import Message

from liaise.model import PERMISSIONS, require_one_of
from liaise.subjects import Subject, address_key

#: Why :func:`authorize` refused a message. A role or grade refusal names the role,
#: grade and permission instead.
UNTRUSTED_CLAIM = "label claim by an untrusted author"
UNRESOLVED_SENDER = "unresolved sender"
NO_ROLE = "no role on this subject"
CONFLICTING_CLAIMS = "label claims name more than one person"
#: The reason of a decision that allows.
ALLOWED = "allowed"

#: How an :class:`AccessDecision` attributed its person.
VIA_HANDLE = "handle"
VIA_RELAY_LABEL = "relay-label"
VIA_NONE = "none"

#: ``(address, subject) -> person id or None``: the identity-resolver seam.
Resolver = Callable[[str, Subject], Optional[str]]


def _at_address(table: Mapping[str, str], address: str) -> Optional[str]:
    """The value ``table`` keeps under ``address``, its keys compared without regard to case."""
    wanted = address_key(address)
    return next((v for k, v in table.items() if address_key(k) == wanted), None)


def resolve_person(address: str, subject: Subject) -> Optional[str]:
    """The person id ``address`` belongs to on ``subject``, or None.

    ``policy.people`` first, matched without regard to case. Otherwise, when acquaint
    imports, ``acquaint.resolve``, trusted only when it is ``ok`` with exactly one match.
    A missing or broken acquaint, or any error it raises, resolves to None.
    """
    person = _at_address(subject.policy.people, address)
    if person is not None:
        return person
    try:
        import acquaint

        result = acquaint.resolve(address)
        matches = result.get("matches") or ()
        if result.get("ok") and len(matches) == 1:
            return matches[0].get("id") or None
    except Exception:  # acquaint missing, broken, or its store unreadable: nobody
        return None
    return None


def is_relay(address: str, subject: Subject) -> bool:
    """Whether ``address`` is one of ``subject``'s ``policy.relays``, compared without regard to case.

    A relay's routing labels count as claims (see :func:`authorize`), and its comments on
    a case are recorded as the relay's own, not as any person's.
    """
    wanted = address_key(address)
    return any(address_key(relay) == wanted for relay in subject.policy.relays)


@dataclass(frozen=True)
class AccessDecision:
    """What :func:`authorize` decided about one message, and why.

    ``person`` is who the message was attributed to (for a refusal, its resolved author,
    if any). ``via`` says how: ``handle``, ``relay-label``, or ``none`` when the
    decision attributes it to no one.
    """

    person: Optional[str]
    role: Optional[str]
    grade: str
    permission: str
    allowed: bool
    reason: str
    via: str


def authorize(
    message: Message,
    subject: Subject,
    permission: str,
    *,
    resolver: Resolver = resolve_person,
) -> AccessDecision:
    """Whether ``message`` may exercise ``permission`` on ``subject``, by the label-as-claim rule.

    See the module docstring for the rule. ``resolver`` maps the author's address to a
    person id (default :func:`resolve_person`). Raises ``ValueError`` for a permission
    outside :data:`~liaise.model.PERMISSIONS`.
    """
    require_one_of(permission, PERMISSIONS, what="permission")
    policy = subject.policy
    grade = str(
        getattr(message.authenticity.grade, "value", message.authenticity.grade)
    )
    address = message.author.address
    author = resolver(address, subject)

    def attributed(person: str, via: str) -> AccessDecision:
        role = policy.roles.get(person)
        if role is None:
            refusal = NO_ROLE
        elif permission not in subject.permissions_for(role):
            refusal = f"role {role} lacks {permission}"
        elif not subject.accepts(permission, grade):
            refusal = f"grade {grade} not accepted for {permission}"
        else:
            refusal = None
        return AccessDecision(
            person=person,
            role=role,
            grade=grade,
            permission=permission,
            allowed=refusal is None,
            reason=refusal or ALLOWED,
            via=via,
        )

    def refused(reason: str) -> AccessDecision:
        return AccessDecision(
            person=author,
            role=None,
            grade=grade,
            permission=permission,
            allowed=False,
            reason=reason,
            via=VIA_NONE,
        )

    if author is not None and author in policy.roles:
        return attributed(author, VIA_HANDLE)
    labels = message.native.get("labels") or ()
    claimed = {
        policy.claim_labels[label] for label in labels if label in policy.claim_labels
    }
    if claimed and not is_relay(address, subject):
        return refused(UNTRUSTED_CLAIM)
    if len(claimed) > 1:
        return refused(CONFLICTING_CLAIMS)
    if claimed:
        (person,) = claimed
        return attributed(person, VIA_RELAY_LABEL)
    return refused(UNRESOLVED_SENDER if author is None else NO_ROLE)
