"""Vetting a draft outside any case: the gate's verdict, and nothing sent (discussion 32, §5.8).

:func:`vet` puts a draft through the same gate every message liaise sends passes
(:func:`liaise.gate.run_gate`), with a case-less context (liaise #28), and returns the
verdict as a JSON-ready record: the flow, the route, the reasons, the audience in words and
the tiers consulted. It sends nothing and records nothing, so it is safe to run at any time
and to expose over MCP later. ``liaise vet`` prints it, with the exit code
:data:`EXIT_CODES` gives the route: 0 send, 2 draft-to-operator, 3 block.

**The same gate, less what does not apply.** Discussion 32 rejects a separate gate for
``vet`` (§11). :data:`VET_FILTERS` are :data:`~liaise.gate.DFLT_OUTBOUND_FILTERS` in their
order, less two whose job is liaise's own sending: :func:`~liaise.gate.outside_a_case`
holds every message liaise itself would send outside a case for the operator (its sender
chose the readers), and :func:`~liaise.gate.notify_recipient` adds the ``@mention`` liaise
needs to reach someone on GitHub. Here the sender is whoever asks, and liaise sends
nothing; the hold on a sender-chosen destination is the taint rule's job, since the
provenance of a vetted draft is unknown unless the caller says otherwise (decision 10).
``outbound_filters=`` takes another tuple.

**The subject** is the one whose bindings take the reference in
(:func:`liaise.subjects.subject_for_ref`); when none does, :func:`~liaise.subjects.unbound_subject`, whose
policy has every default: nobody known, the taint rule in force, ``direct`` reply mode.

**Where nothing can be held.** A ``delay`` routes to ``send`` (discussion §5.5), because
liaise's outbox holds it. :func:`before_send` (correspond's check) and the Claude Code hook
(:mod:`liaise.hook`) stand in front of a write that happens at once, with no outbox, so for
them a ``delay`` degrades to draft-to-operator, as §5.5 degrades it where the outbox does
not exist: :func:`immediate_route`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Union

from liaise.config import DFLT_CONFIG_ROOT, load_global_config
from liaise.detect import key_source
from liaise.gate import (
    DFLT_OUTBOUND_FILTERS,
    GateContext,
    GateDecision,
    Outbound,
    OutboundFilter,
    notify_recipient,
    outside_a_case,
    run_gate,
)
from liaise.policy import (
    DELAY,
    ROUTE_BLOCK,
    ROUTE_DRAFT,
    ROUTE_SEND,
    ROUTES,
    Provenance,
    audience_in_words,
)
from liaise.release import audience_of, sendable_ref
from liaise.subjects import (
    UNBOUND_SLUG,
    NoSubjectBinding,
    Subject,
    load_subjects,
    subject_for_ref,
    unbound_subject,
)

#: The gate's filters as ``vet`` runs them: every one, in order, but the two that presuppose
#: liaise as the sender (see the module docstring). Derived by exclusion, so a filter added
#: to the gate reaches ``vet`` too.
VET_FILTERS: tuple[OutboundFilter, ...] = tuple(
    f for f in DFLT_OUTBOUND_FILTERS if f not in (outside_a_case, notify_recipient)
)
#: The exit code of ``liaise vet`` for each route.
EXIT_CODES: Mapping[str, int] = {ROUTE_SEND: 0, ROUTE_DRAFT: 2, ROUTE_BLOCK: 3}
#: The purpose a vetted draft is judged for: the writing card and deslop read it.
VET_PURPOSE = "reply"
#: Why a vetted draft counts as tainted when the caller did not say (decision 10).
UNKNOWN_PROVENANCE = (
    "nobody said what the author of this draft read (--untainted says it read nothing "
    "untrusted)"
)
UNTAINTED = "the caller vouched that the author read nothing untrusted"
TAINTED = "the caller said the author read untrusted content"


def _subject_for(subjects: Mapping[str, Subject], ref: str) -> Subject:
    """The subject whose bindings take ``ref`` in, else :func:`~liaise.subjects.unbound_subject`.

    A reference bound equally closely by two subjects still raises, as it does for a send.
    """
    try:
        return subject_for_ref(subjects, ref)
    except NoSubjectBinding:
        return unbound_subject()


def _normalised_ref(ref: str) -> str:
    """``ref`` stripped; a GitHub issue or repository as :func:`~liaise.release.sendable_ref` keeps it."""
    ref = ref.strip()
    if not ref or ":" not in ref:
        raise ValueError(
            f"{ref!r} is not a conversation reference: give one such as "
            f"github:example/app#12"
        )
    if ref.partition(":")[0].lower() == "github":
        return sendable_ref(ref)
    return ref


def _provenance(tainted: Optional[bool]) -> Provenance:
    if tainted is None:
        return Provenance.unknown(UNKNOWN_PROVENANCE)
    return Provenance.tainted_by(TAINTED) if tainted else Provenance.clean(UNTAINTED)


def _people(to: Union[str, Sequence[str]]) -> tuple[str, ...]:
    people = (to,) if isinstance(to, str) else tuple(to)
    return tuple(p.strip() for p in people if p and p.strip())


def immediate_route(flow: str) -> str:
    """The route of ``flow`` for a write that happens at once: a ``delay`` is draft-to-operator.

    >>> immediate_route("delay"), immediate_route("send"), immediate_route("refuse")
    ('draft', 'send', 'block')
    """
    return ROUTE_DRAFT if flow == DELAY else ROUTES[flow]


def verdict_record(decision: GateDecision, *, subject: Subject, ref: str) -> dict:
    """What ``vet`` answers about ``decision``: JSON-ready, and never the text or a value found.

    ``flow`` and ``route`` are the decision's, ``exit_code`` the route's
    (:data:`EXIT_CODES`), ``reasons`` every standing concern's text, most restrictive first,
    ``rules`` the rules they came from, ``audience`` the audience in words, ``readers`` the
    tier and clearance of each reader consulted, ``notes`` every filter's notes, and
    ``record`` what a ledger ``gate`` entry would keep (:meth:`GateDecision.record`).
    """
    verdict = decision.verdict
    route = ROUTES[decision.flow]
    return {
        "ref": ref,
        "subject": subject.slug,
        "flow": decision.flow,
        "route": route,
        "exit_code": EXIT_CODES[route],
        "reasons": [concern.text for concern in decision.concerns],
        "rules": [concern.rule for concern in decision.concerns if concern.rule],
        "audience": (
            decision.audience_words
            if verdict is not None
            else audience_in_words({"ref": ref})
        ),
        "readers": (
            {}
            if verdict is None
            else {person: dict(entry) for person, entry in verdict.readers.items()}
        ),
        "notes": list(decision.notes),
        "payload_hash": decision.payload_hash,
        "audience_hash": decision.audience_hash,
        "record": decision.record(),
    }


def vet(
    text: str,
    *,
    ref: str,
    to: Union[str, Sequence[str]] = (),
    cc: Sequence[str] = (),
    bcc: Sequence[str] = (),
    title: Optional[str] = None,
    project: Optional[str] = None,
    tainted: Optional[bool] = None,
    root: Union[str, Path, None] = None,
    subjects: Optional[Mapping[str, Subject]] = None,
    registry: Optional[Mapping[str, Any]] = None,
    audience: Any = None,
    now: Optional[datetime] = None,
    fingerprint_key: Union[bytes, Callable[[], bytes], None] = None,
    outbound_filters: Iterable[OutboundFilter] = VET_FILTERS,
) -> dict:
    """The gate's verdict on ``text`` sent to ``ref`` for ``to``, as :func:`verdict_record` gives it. Sends nothing.

    ``to`` is the person (or people) the draft is for: the first is its recipient and the
    rest count as copies, beside ``cc`` and ``bcc``, so each is a reader the policy judges.
    ``project`` names the project it is about. ``tainted`` is what the author read: None
    (unknown, which counts as tainted), True, or False (``--untainted``: nothing untrusted).
    ``root`` is liaise's config root, whose subjects and state directory are read (none is
    needed); ``subjects`` replaces the subjects read from it. ``audience`` is correspond's
    record of who can read ``ref``; None asks correspond now, on ``registry``.
    ``fingerprint_key`` is as :class:`~liaise.gate.GateContext` has it; by default the key
    in the state directory, and none is created.

    Raises ``ValueError`` for a reference that is not one, and
    :class:`~liaise.config.ConfigError` for a configuration that does not load.
    """
    ref = _normalised_ref(ref)
    config_root = Path(root).expanduser() if root is not None else DFLT_CONFIG_ROOT
    if subjects is None:
        subjects = load_subjects(config_root)
    subject = _subject_for(subjects, ref)
    if fingerprint_key is None:
        state_dir = None
        if (config_root / "config.toml").exists():
            state_dir = load_global_config(config_root).state_dir
        fingerprint_key = key_source(state_dir, create=False)
    people = _people(to)
    recipient, *also = people or ("",)
    outbound = Outbound(
        ref=ref,
        channel=ref.partition(":")[0].lower(),
        recipient=recipient,
        purpose=VET_PURPOSE,
        text=text,
        title=(title or "").strip() or None,
        cc=(*also, *_people(cc)),
        bcc=_people(bcc),
        project=project or None,
    )
    if audience is None:
        audience = audience_of(outbound, registry=registry)
    context = GateContext(
        subject=subject,
        now=now or datetime.now(timezone.utc),
        audience=audience,
        provenance=_provenance(tainted),
        fingerprint_key=fingerprint_key,
    )
    decision = run_gate(outbound, context, outbound_filters=outbound_filters)
    return verdict_record(decision, subject=subject, ref=ref)


def before_send(ref: Any, draft: Any, audience: Any, **context: Any) -> None:
    """correspond's ``before_send`` check: :func:`vet`, raising ``Refused`` or ``NeedsApproval``.

    Set it once in correspond's config::

        before_send = "liaise.vet:before_send"

    ``ref`` is correspond's conversation reference, ``draft`` its draft (its text, title and
    copies are vetted) and ``audience`` the audience correspond computed, which the gate
    judges as given. The subject is the one binding the reference, else
    :func:`~liaise.subjects.unbound_subject`, and the provenance is unknown. A block raises
    ``correspond.errors.Refused`` and anything for the operator, a ``delay`` included (the
    write happens at once: :func:`immediate_route`), ``NeedsApproval``, each with the reasons
    joined; a send returns None. The details carry the flow, the rules and the hashes,
    never the text. A draft that cannot be vetted (a configuration that does not load) is
    ``NeedsApproval`` with the reason.
    """
    from correspond.errors import NeedsApproval, Refused

    try:
        record = vet(
            getattr(draft, "text", None) or "",
            ref=ref if isinstance(ref, str) else _encoded(ref),
            cc=tuple(getattr(draft, "cc", ()) or ()),
            bcc=tuple(getattr(draft, "bcc", ()) or ()),
            title=getattr(draft, "title", None),
            audience=audience,
        )
    except Exception as error:  # a check that cannot judge holds the write
        raise NeedsApproval(
            f"liaise could not vet this draft ({type(error).__name__}: {error})"
        ) from error
    route = immediate_route(record["flow"])
    if route == ROUTE_SEND:
        return None
    reasons = list(record["reasons"])
    if record["flow"] == DELAY and route == ROUTE_DRAFT:
        reasons.append("the write happens at once, so nothing can hold it for a delay")
    details = {
        "flow": record["flow"],
        "rules": record["rules"],
        "payload_hash": record["payload_hash"],
        "audience_hash": record["audience_hash"],
    }
    reason = "; ".join(reasons) or f"liaise vet: {record['flow']}"
    if route == ROUTE_BLOCK:
        raise Refused(reason, **details)
    raise NeedsApproval(reason, **details)


def _encoded(ref: Any) -> str:
    """correspond's reference as a string: its ``encoded`` form, ``<channel>:<id>``."""
    encoded = getattr(ref, "encoded", None)
    return encoded if isinstance(encoded, str) else str(ref)
