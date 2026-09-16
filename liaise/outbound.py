"""What the outbound gate's policy filter gathers before the policy decides.

:func:`liaise.policy.evaluate` is pure: it takes a message, an audience, a disclosure, the
findings and the run's provenance, and returns a verdict. This module gathers those inputs
for one message (liaise discussion 32, §5.3 and §5.6; liaise ADR 0002), and
:func:`liaise.gate.outbound_policy` hands them over through :func:`judge`:

- **The audience** is what the gate's context carries, correspond's record for the
  destination. None is unknown, which resolves to public (:func:`audience_snapshot`).
- **The disclosure** comes through the ``disclosure=`` seam: :func:`acquaint_disclosure`,
  which is ``acquaint.disclosure`` when acquaint imports and, without it, every reader at
  ``need-to-know`` (:func:`need_to_know_disclosure`). The subject's ``policy.leak_terms``
  join its vocabulary as a label no reader is cleared for (:func:`with_leak_terms`).
- **The readers' identities**: each copy and listed reader of the audience, resolved to a
  person by the subject's resolver (:func:`identities_for`).
- **The findings** of the detectors, over the text, the title and each attachment name
  (:func:`findings_in`).
- **The provenance**: what the run that wrote the message read. :func:`case_provenance`
  judges a case's messages by the access pairs the ledger records.

After a message goes out, :func:`record_disclosure` appends an ``interaction`` entry to each
recipient's acquaint record, naming the labelled records the message identified, never its
text (discussion §4.8).

Nothing here sends, and nothing here imports the gate.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Optional, Union

from liaise.access import Resolver, resolve_person
from liaise.detect import DFLT_DETECTORS, LABELS, Detector, Finding, detect
from liaise.intake import SELF_ROLE
from liaise.model import Case, LedgerEntry
from liaise.policy import (
    SEND,
    OutboundPolicy,
    Provenance,
    Verdict,
    audience_in_words,
    audience_record,
    canonical_json,
    evaluate,
)
from liaise.subjects import Subject

#: The entity ``policy.leak_terms`` are scanned as, and its label: the most restrictive,
#: so no reader of any channel but the operator's own devices is cleared for it, as the 0.1
#: leak scan held a leak term back from every public channel.
LEAK_TERMS_ENTITY = "policy.leak_terms"
LEAK_TERMS_LABEL = LABELS[-1]
#: A reader's standing when nobody can say more (discussion §4.1): the default tier.
NEED_TO_KNOW = "need-to-know"
CLEAR = LABELS[0]
#: Where a disclosure came from, as the ledger records it.
FROM_ACQUAINT = "acquaint"
FROM_FALLBACK = "no acquaint: every reader at need-to-know"
#: The permission a message's author must hold, at a grade it accepts, for a run that read
#: the message to count as clean (discussion decision 10).
REQUEST_WORK = "request_work"
#: The ledger entry kind a case's inbound messages are recorded as.
MESSAGE_KIND = "message"
#: The hosts a link may point at on a channel, beside the subject's ``link_allowlist``.
CHANNEL_HOSTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {"github": ("github.com",)}
)
#: The finding kinds that identify a labelled record, and so what "already told" counts.
IDENTIFYING_KINDS = frozenset({"vocabulary", "third_party"})
#: Who an ``interaction`` entry liaise appends to an acquaint record is sourced to.
DISCLOSURE_SOURCE = "liaise"
UNKNOWN_PROVENANCE = "nobody said what the author of this message read"
#: How a finding names the part of a message it was found in, when that is not the text.
TITLE_PART = "title"
ATTACHMENT_PART = "attachment name"

#: ``(people, *, projects, audience, today) -> disclosure``: the ``disclosure=`` seam. The
#: answer is ``acquaint.disclosure``'s JSON; ``audience`` is correspond's record, as a dict.
DisclosureSource = Callable[..., Mapping[str, Any]]


# ---- the audience ----


def audience_snapshot(audience: Any, ref: str) -> dict:
    """``audience`` as the policy and the hashes see it; None is the unknown audience of ``ref``.

    >>> snapshot = audience_snapshot(None, "github:example/app#12")
    >>> snapshot["scope"], snapshot["defaulted"], snapshot["ref"]
    ('public', True, 'github:example/app#12')
    """
    return audience_record(audience if audience is not None else {"ref": ref})


# ---- the disclosure ----


def need_to_know_disclosure(
    people: Iterable[str], *, today: Optional[str] = None
) -> dict:
    """A disclosure without acquaint: every named reader at ``need-to-know``, cleared to ``clear``.

    >>> need_to_know_disclosure(["ada"])["people"]["ada"]["clearance"]
    'clear'
    """
    return {
        "as_of": today,
        "people": {
            person: {
                "tier": NEED_TO_KNOW,
                "clearance": CLEAR,
                "lapsed": False,
                "source": None,
                "involved_in": [],
                "already_told": [],
            }
            for person in dict.fromkeys(p for p in people if p)
        },
        "least_clearance": CLEAR,
        "audience": None,
        "entities": {},
        "seals": [],
        "vocabulary": [],
        "gaps": {},
        "source": FROM_FALLBACK,
    }


def acquaint_disclosure(
    people: Sequence[str],
    *,
    projects: Sequence[str] = (),
    audience: Optional[Mapping[str, Any]] = None,
    today: Optional[str] = None,
) -> Mapping[str, Any]:
    """What each of ``people`` may be told, from acquaint; everyone at ``need-to-know`` without it.

    The default of the ``disclosure=`` seam. acquaint that imports and then fails raises,
    so the gate flags the message rather than judging it with less than it should know.
    """
    try:
        import acquaint
    except ImportError:
        return need_to_know_disclosure(people, today=today)
    answer = acquaint.disclosure(
        list(people),
        projects=list(projects) or None,
        audience=None if audience is None else json.dumps(audience),
        today=today,
    )
    return {**answer, "source": answer.get("source") or FROM_ACQUAINT}


def with_leak_terms(disclosure: Mapping[str, Any], leak_terms: Iterable[str]) -> dict:
    """``disclosure`` with each of ``leak_terms`` added to its vocabulary, at :data:`LEAK_TERMS_LABEL`.

    >>> found = with_leak_terms({"vocabulary": []}, ["the-bird-board", " "])["vocabulary"]
    >>> [(term["term"], term["label"]) for term in found]
    [('the-bird-board', 'red')]
    """
    terms = [term for term in leak_terms if term.strip()]
    vocabulary = list(disclosure.get("vocabulary") or ())
    vocabulary += [
        {
            "term": term,
            "entity": LEAK_TERMS_ENTITY,
            "label": LEAK_TERMS_LABEL,
            "sealed_from": [],
        }
        for term in terms
    ]
    return {**disclosure, "vocabulary": vocabulary}


def consulted_of(
    disclosure: Mapping[str, Any],
    *,
    identities: Mapping[str, Optional[str]],
    provenance: Provenance,
) -> dict:
    """What the ledger keeps of a judgement's inputs: the labels and seals consulted, never a term.

    The tiers and clearances of the readers are the verdict's own (``Verdict.readers``).
    """
    labels = {
        str(entry.get("entity")): entry.get("label")
        for entry in disclosure.get("vocabulary") or ()
        if isinstance(entry, Mapping)
    }
    return {
        "disclosure": {
            "source": disclosure.get("source"),
            "as_of": disclosure.get("as_of"),
            "least_clearance": disclosure.get("least_clearance"),
            "labels": dict(sorted(labels.items())),
            "seals": list(disclosure.get("seals") or ()),
            "gaps": {
                str(name): list(values or ())
                for name, values in (disclosure.get("gaps") or {}).items()
                if values
            },
        },
        "identities": dict(identities),
        "provenance": provenance.to_dict(),
    }


# ---- readers, findings, provenance ----


def identities_for(
    outbound: Any,
    audience: Mapping[str, Any],
    *,
    subject: Subject,
    resolver: Resolver = resolve_person,
) -> dict[str, Optional[str]]:
    """The person each copy and each listed reader of ``audience`` resolves to on ``subject``; None for nobody.

    ``audience`` is a snapshot (:func:`audience_snapshot`). A resolver that raises resolves
    to nobody, which the policy counts at ``clear``.
    """
    addresses = [
        *getattr(outbound, "cc", ()),
        *getattr(outbound, "bcc", ()),
        *(reader["address"] for reader in audience["readers"] if not reader["is_self"]),
    ]

    def resolved(address: str) -> Optional[str]:
        try:
            return resolver(address, subject)
        except Exception:  # a broken resolver knows nobody
            return None

    return {address: resolved(address) for address in dict.fromkeys(addresses)}


def allowlist_for(outbound: Any, subject: Subject) -> tuple[str, ...]:
    """The hosts a link in ``outbound`` may point at: its channel's own and ``policy.link_allowlist``."""
    return (
        *CHANNEL_HOSTS.get(outbound.channel, ()),
        *subject.policy.link_allowlist,
    )


def findings_in(
    outbound: Any,
    disclosure: Mapping[str, Any],
    *,
    subject: Subject,
    key: Union[bytes, Callable[[], bytes], None] = None,
    detectors: Iterable[Detector] = DFLT_DETECTORS,
) -> tuple[Finding, ...]:
    """What ``detectors`` find in the text, the title and each attachment name of ``outbound``.

    A finding's offsets are into the part it was found in, which its ``part`` names when
    that is not the text.
    """
    detectors = tuple(detectors)
    parts = (
        (None, outbound.text),
        (TITLE_PART, outbound.title or ""),
        *((ATTACHMENT_PART, name) for name in outbound.attachments),
    )
    found: list[Finding] = []
    for part, content in parts:
        if not content:
            continue
        findings = detect(
            content,
            disclosure=disclosure,
            allowlist=allowlist_for(outbound, subject),
            canary_terms=subject.policy.canary_terms,
            key=key,
            detectors=detectors,
        )
        found += [f if part is None else replace(f, part=part) for f in findings]
    return tuple(found)


def _distrust(entry: LedgerEntry, subject: Subject) -> Optional[str]:
    """Why ``entry``, an inbound message, taints a run that read it; None when it does not."""
    if entry.detail.get("role") == SELF_ROLE:
        return None  # the channel's own account: what liaise, or the operator, sent
    who = entry.actor or "an unattributed sender"
    role = subject.policy.roles.get(entry.actor) if entry.actor else None
    if role is None:
        return f"a message from {who}, who has no role on {subject.slug}"
    if REQUEST_WORK not in subject.permissions_for(role):
        return f"a message from {who}, whose role {role} does not grant {REQUEST_WORK}"
    if entry.grade is None or not subject.accepts(REQUEST_WORK, entry.grade):
        return f"a message from {who} at grade {entry.grade}, which {REQUEST_WORK} does not accept"
    return None


def _refused_readings(case: Case, subject: Subject, ledger: Any) -> Iterable[str]:
    """Why each message intake refused on one of ``case``'s conversations taints a run.

    A message from someone with no role never becomes an entry: intake queues it as
    unrouted and the case never sees it (:mod:`liaise.intake`). The run reads the
    conversation itself, so it read that message all the same.
    """
    conversations = {str(ref) for ref in case.conversations}
    try:
        queued = list(ledger.unrouted())
    except (
        Exception
    ) as error:  # a queue nobody can read is a reading nobody can vouch for
        return [
            f"the unrouted queue could not be read ({type(error).__name__}: {error})"
        ]
    return [
        f"a message from {item.get('author') or 'an unnamed sender'} on "
        f"{item.get('conversation')}, which intake refused ({item.get('reason')}), and a "
        f"run reads the conversation itself"
        for item in queued
        if isinstance(item, Mapping)
        and item.get("subject") == subject.slug
        and str(item.get("conversation")) in conversations
    ]


def case_provenance(case: Case, subject: Subject, ledger: Any) -> Provenance:
    """Whether a run on ``case`` read anything ``subject`` does not trust for ``request_work``.

    Every ``message`` entry of the case counts, not only those a run had read when it
    started: the ledger does not say which a resumed session saw, and counting one it did
    not can only hold a message back. A message is trusted when the channel's own account
    wrote it, or its author has a role that grants ``request_work`` at the grade it was
    received at.

    ``ledger`` is read for the same reason: a message whose author has no role at all is
    refused at intake and queued as unrouted, so it is on the conversation the run reads
    and on no entry of the case. It counts too, which is what makes the rule cover the
    stranger it exists for (liaise discussion 32, §5.3).

    >>> from datetime import datetime, timezone
    >>> from liaise.ledger import Ledger
    >>> from liaise.subjects import Policy
    >>> subject = Subject("app", ("github:example/app",), Policy(people={}, roles={"pat": "partner", "obi": "observer"}))
    >>> at = datetime(2026, 9, 15, tzinfo=timezone.utc)
    >>> case = Case(id="app-1", subject="app", conversations=(), reporter="pat", state="working", created_at=at, updated_at=at,
    ...             entries=(LedgerEntry(at=at, kind="message", actor="pat", grade="platform"),))
    >>> case_provenance(case, subject, Ledger({})).tainted
    False
    >>> case = case.with_entry(LedgerEntry(at=at, kind="message", actor="obi", grade="platform"))
    >>> case_provenance(case, subject, Ledger({})).evidence
    ('a message from obi, whose role observer does not grant request_work',)
    """
    reasons = [
        why
        for entry in case.entries
        if entry.kind == MESSAGE_KIND
        for why in (_distrust(entry, subject),)
        if why is not None
    ]
    reasons += list(_refused_readings(case, subject, ledger))
    if reasons:
        return Provenance.tainted_by(*dict.fromkeys(reasons))
    return Provenance.clean(
        f"every message on {case.id} is from someone trusted with {REQUEST_WORK}, and "
        f"intake refused none of its conversations' messages"
    )


# ---- the judgement ----


@dataclass(frozen=True)
class Judgement:
    """What :func:`judge` decided about one message, and what it consulted to decide.

    ``verdict`` is the policy's. ``consulted`` is what the ledger keeps of the inputs
    (:func:`consulted_of`), and ``notes`` the lines the operator reads beside the verdict:
    the audience in words, and the rules that fired without holding the message back.
    """

    verdict: Verdict
    consulted: Mapping[str, Any]
    notes: tuple[str, ...] = ()


def judge(
    outbound: Any,
    *,
    subject: Subject,
    now: datetime,
    audience: Any = None,
    provenance: Optional[Provenance] = None,
    mode: Optional[str] = None,
    key: Union[bytes, Callable[[], bytes], None] = None,
    disclosure: DisclosureSource = acquaint_disclosure,
    detectors: Iterable[Detector] = DFLT_DETECTORS,
    resolver: Resolver = resolve_person,
) -> Judgement:
    """The policy's verdict on ``outbound``, sent to its destination on ``subject``, with what it consulted.

    ``audience`` is correspond's record for the destination (None: unknown, so public).
    ``provenance`` is the run's (None: unknown, so tainted). ``mode`` overrides the
    subject's ``policy.mode``. ``key`` is the fingerprint key (see
    :func:`liaise.detect.detect`). ``disclosure``, ``detectors`` and ``resolver`` are the
    seams of those names. The disclosure is asked for exactly this message's readers: its
    recipient and copies by name, and the audience's listed readers through the record.
    """
    snapshot = audience_snapshot(audience, outbound.ref)
    readers = [outbound.recipient, *outbound.cc, *outbound.bcc]
    answer = disclosure(
        readers,
        projects=(outbound.project,) if outbound.project else (),
        audience=snapshot,
        today=now.date().isoformat(),
    )
    if not isinstance(answer, Mapping):
        raise TypeError(
            f"the disclosure is acquaint.disclosure's JSON (a mapping), not {type(answer).__name__}"
        )
    answer = with_leak_terms(answer, subject.policy.leak_terms)
    identities = identities_for(outbound, snapshot, subject=subject, resolver=resolver)
    provenance = (
        provenance if provenance is not None else Provenance.unknown(UNKNOWN_PROVENANCE)
    )
    policy = OutboundPolicy(
        reply_mode=subject.reply_mode_for(outbound.recipient),
        tainted_runs=subject.policy.tainted_runs,
        mode=mode or subject.policy.mode,
    )
    verdict = evaluate(
        outbound,
        audience=snapshot,
        disclosure=answer,
        findings=findings_in(
            outbound, answer, subject=subject, key=key, detectors=detectors
        ),
        provenance=provenance,
        policy=policy,
        now=now,
        identities=identities,
    )
    notes = (
        f"audience: {audience_in_words(snapshot)}",
        *(
            f"policy: {reason.text}"
            for reason in verdict.reasons
            if reason.flow == SEND
        ),
    )
    consulted = consulted_of(answer, identities=identities, provenance=provenance)
    return Judgement(verdict=verdict, consulted=consulted, notes=notes)


def verdict_id(verdict: Verdict) -> str:
    """A name for what an approval of ``verdict`` releases: the rules that fired and what they found.

    Each reason's rule, flow and reader, the fingerprint and entity of the finding it names
    (never its value), and the two hashes an approval binds to. The time the verdict was
    made is left out, so the same message judged twice has the same name — and a verdict
    that flags something else does not, which is how an approval given for one finding
    cannot settle another (liaise ADR 0002).
    """
    shape = {
        "reasons": sorted(
            [
                reason.rule,
                reason.flow,
                reason.reader or "",
                "" if reason.finding is None else reason.finding.fingerprint,
                "" if reason.finding is None else (reason.finding.entity or ""),
            ]
            for reason in verdict.reasons
        ),
        "payload_hash": verdict.payload_hash,
        "audience_hash": verdict.audience_hash,
    }
    return sha256(canonical_json(shape).encode("utf-8")).hexdigest()


# ---- after a send ----


def disclosed_records(verdict: Optional[Verdict]) -> tuple[str, ...]:
    """The labelled records ``verdict``'s message identified, each once, in order: never a term."""
    if verdict is None:
        return ()
    return tuple(
        dict.fromkeys(
            finding.entity
            for finding in verdict.findings
            if finding.kind in IDENTIFYING_KINDS
            and finding.entity
            and finding.entity != LEAK_TERMS_ENTITY
        )
    )


def record_disclosure(
    outbound: Any,
    verdict: Optional[Verdict],
    consulted: Mapping[str, Any],
) -> Optional[str]:
    """Append an ``interaction`` entry to each recipient's acquaint record naming what ``outbound`` identified.

    The recipients are the message's recipient and every copy that resolved to a person.
    Nothing is written when the message identified no labelled record, or acquaint does not
    import. Returns why a record could not be written, or None: the message has gone out,
    so a failure here is for the operator to read, never a reason to send again.
    """
    records = disclosed_records(verdict)
    if not records:
        return None
    try:
        import acquaint
    except ImportError:
        return None
    identities = consulted.get("identities") or {}
    copies = (identities.get(address) for address in (*outbound.cc, *outbound.bcc))
    people = dict.fromkeys(p for p in (outbound.recipient, *copies) if p)
    failures = []
    for person in people:
        try:
            acquaint.remember(
                person,
                f"liaise sent a {outbound.purpose} on {outbound.ref}",
                source=DISCLOSURE_SOURCE,
                kind="interaction",
                disclosed=list(records),
            )
        except Exception as error:  # the message went out: report, never raise
            failures.append(f"{person}: {type(error).__name__}: {error}")
    return "; ".join(failures) or None
