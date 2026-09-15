"""Policy and verdict for outbound messages: from findings and an audience to a flow.

:func:`evaluate` is a pure function. It takes what the other packages and the detectors
established (liaise discussion 32, §1): the message, the ``Audience`` of its destination
(correspond's record, as JSON), the ``Disclosure`` for its readers (acquaint's record, as
JSON), the :class:`~liaise.detect.Finding` records the detectors made, the run's
:class:`Provenance` and the :class:`OutboundPolicy` in force, and returns a
:class:`Verdict`: a flow, the route the operator sees, every rule that fired with a
sentence they can read, the five axes the decision was made on, and the two hashes an
approval binds to. It imports nothing from correspond or acquaint, reads no file, no clock
and no network, and returns the same verdict for the same inputs.

**Flows and routes** (discussion §5.5). Flows order by restriction: ``send`` < ``delay`` <
``revise`` < ``approve`` < ``approve_twice`` < ``refuse``. Three routes: ``send`` (sent
now, or held in the outbox for ``delay``), ``draft`` (a flagged draft for the operator, or
back to the processor for ``revise``), ``block`` (a draft that cannot be released as
written). ``approve_twice`` keeps its place in the order and is never produced (decision
9).

**The rule table** (:data:`RULES`, discussion §5.4) is declared data: a tuple of
:class:`Rule` records, each a name, a predicate over the :class:`Facts` and a minimum flow.
Every rule is evaluated; the verdict's flow is the most restrictive that fired, and the
reasons are every hit, most restrictive first (decision 5). Two rows take their flow from a
condition the table states: *no write-down* and *co-ownership* are ``revise`` when the case
can resume and ``approve`` otherwise. Two decisions of this slice, recorded here because the
table and the scenario suite of research §9.3 disagree without them:

- *Exfiltration* refuses every exfiltration shape but a plain link: an image loads without a
  click, an encoded run or an invisible character carries data, a private address or a
  local path names the operator's machine; a link a reader has to follow is shown to the
  operator in full and is ``approve`` (S22 must deliver; S21 must not).
- *Taint* is ``approve`` for a tainted or unknown run reaching past the operator, and
  ``refuse`` when that run's message also names something the audience is not cleared
  for: an injection that got private content out is never released as written (S9).

The rows *no write-down* and *co-ownership* partition the labelled findings: a project's,
organisation's or group's term is judged against the least-cleared reader; a person's term
(``third_party``) reader by reader, so the reason names who is not cleared for whom. The
row *unknown audience* sets no flow: it explains that the ceiling is ``clear``.

**The least-cleared reader** (:func:`least_cleared_reader`, discussion §4.4): ``clear``
for a public or defaulted audience; for ``org`` and ``group`` with ``complete`` false, the
organisation's recorded clearance (the disclosure's ``audience.ceiling``) else ``clear``,
then the minimum over the resolved readers; for ``named``, the minimum over the resolved
readers, an unresolved identity or recipient at ``clear``; for ``operator``, no ceiling.
Readers are the disclosure's ``people`` and whatever ``identities`` resolves, each at
their ``clearance``; a resolved person the disclosure does not know is at ``clear``.

**Hashes.** :func:`payload_hash` is SHA-256 over the canonical JSON of the recipients,
``cc``, ``bcc``, the reference, the title, the exact text and the attachment names
(:func:`payload_of`). :func:`audience_hash` is SHA-256 over the canonical JSON of the
audience record without ``as_of`` and ``evidence``, the construction correspond's
``Audience.hash`` uses: canonical JSON is ``json.dumps(data, sort_keys=True,
separators=(",", ":"))`` with Python's default ASCII escaping, encoded UTF-8, and the
record is normalised as correspond stores it (readers deduplicated and sorted by their
canonical JSON, with their derived ``address``; classes, durability and widening sorted).
That equality is a cross-package contract, pinned by a fixture in the tests. Approvals
bind to both hashes (discussion §5.7).

**Inputs.** ``outbound`` is any object or mapping with ``ref``, ``channel``,
``recipient`` (a person id or an address) and ``text``, and optionally ``title``, ``cc``,
``bcc``, ``attachments`` and ``case_id`` (:class:`liaise.gate.Outbound` is one).
``audience`` is the JSON of a correspond ``Audience`` (or an object with ``to_dict``);
``None`` is unknown, which resolves to public. ``disclosure`` is the JSON of
``acquaint.disclosure``; ``{}`` when there is none. ``findings`` are
:class:`~liaise.detect.Finding` records or their dicts. ``identities`` maps an address
(a ``cc`` entry, a listed reader) to the person id it resolved to, or ``None`` when it
did not: identity resolution happens before policy (research §4.6), and this is where
its answer comes in. A ``cc`` or ``bcc`` entry that is neither a person of the disclosure
nor resolved by ``identities`` is a stranger.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime
from hashlib import sha256
from typing import Any, Optional, Union

from liaise.detect import LABELS, Finding

#: The flows, least restrictive first (discussion §5.5). ``approve_twice`` is reserved.
FLOWS = ("send", "delay", "revise", "approve", "approve_twice", "refuse")
SEND, DELAY, REVISE, APPROVE, APPROVE_TWICE, REFUSE = FLOWS
#: The route the operator sees for each flow.
ROUTES = {
    SEND: "send",
    DELAY: "send",
    REVISE: "draft",
    APPROVE: "draft",
    APPROVE_TWICE: "draft",
    REFUSE: "block",
}
ROUTE_SEND, ROUTE_DRAFT, ROUTE_BLOCK = "send", "draft", "block"
#: correspond's audience scopes, narrowest first.
SCOPES = ("operator", "named", "group", "org", "public")
OPERATOR, NAMED, GROUP, ORG, PUBLIC = SCOPES
#: The scopes an exfiltration shape or a personal detail must not reach.
WIDE_SCOPES = frozenset({GROUP, ORG, PUBLIC})
BROAD_SCOPES = frozenset({ORG, PUBLIC})
#: acquaint's tiers, most permissive first, and the relationship axis's extra value.
TIERS = ("open", "involved", "need-to-know", "reviewed")
STRANGER = "stranger"
RELATIONSHIPS = (*TIERS, STRANGER)
PERMISSIVE_TIERS = frozenset({"open", "involved"})
REVIEWED = "reviewed"
#: The finding kinds each row reads.
SECRET_KINDS = frozenset({"secret", "canary"})
EXFILTRATION = "exfiltration"
PERSONAL = "personal"
THIRD_PARTY = "third_party"
#: An exfiltration finding a reader must act on to trigger: shown in full, not refused.
LINK_RULE = "link-host"
#: Policy values: the reply mode that waits for the operator, the taint waiver, the modes.
DRAFT_REPLY_MODE = "draft"
TAINTED_RUNS_APPROVE, TAINTED_RUNS_SEND = "approve", "send"
MODES = ("enforce", "shadow")
ENFORCE, SHADOW = MODES
AVERSE = "averse"
#: The axes of every verdict (research §7.1), in the order ``to_dict`` writes them.
AXES = ("audience", "sensitivity", "relationship", "irreversible", "tainted")
#: The audience fields the hash leaves out (correspond's ``AUDIENCE_UNHASHED``).
AUDIENCE_UNHASHED = ("as_of", "evidence")
#: The audience record's fields and their defaults, as correspond's ``to_dict`` writes
#: them; ``scope`` has no default, since an audience without one is unknown.
AUDIENCE_DEFAULTS: Mapping[str, Any] = {
    "ref": "",
    "readers": (),
    "complete": False,
    "classes": (),
    "external": None,
    "retractable": False,
    "durability": (),
    "widening": (),
    "as_of": None,
    "evidence": (),
    "defaulted": False,
}
#: A reader's fields, as correspond's ``ChannelIdentity.to_dict`` writes them.
READER_DEFAULTS: Mapping[str, Any] = {
    "channel": "",
    "native_id": "",
    "handle": None,
    "display_name": None,
    "is_bot": False,
    "is_self": False,
    "authority": None,
}
#: What an unknown audience is, before anything else is known (correspond's
#: ``Audience.unknown``): public, every durability and widening flag, this evidence.
UNKNOWN_AUDIENCE_EVIDENCE = "unknown resolves to public"
DURABILITY = ("indexed", "archived_by_others", "copies_pushed", "edit_history_visible")
WIDENING = (
    "visibility_flip",
    "joiners_read_history",
    "forwarding",
    "forks",
    "list_expansion",
)
_SCOPE_WORDS = {
    OPERATOR: "only the operator",
    NAMED: "named readers",
    GROUP: "a bounded group",
    ORG: "organisation-wide",
    PUBLIC: "world-readable",
}
_CLASS_WORDS = {
    "watchers and participants receive the body by email": (
        "emailed to watchers and participants"
    ),
}


# ---- records ----


@dataclass(frozen=True)
class Provenance:
    """What the run that wrote the message read: tainted, clean, or unknown.

    ``tainted`` is ``None`` when nobody can say (the hook path), which counts as tainted
    (decision 10). ``evidence`` says why: the messages read and the grades and roles that
    the subject does not trust, in words.
    """

    tainted: Optional[bool] = None
    evidence: tuple[str, ...] = ()

    @classmethod
    def unknown(cls, *evidence: str) -> Provenance:
        """A run nobody can vouch for."""
        return cls(None, evidence)

    @classmethod
    def clean(cls, *evidence: str) -> Provenance:
        """A run that read only what the subject trusts."""
        return cls(False, evidence)

    @classmethod
    def tainted_by(cls, *evidence: str) -> Provenance:
        """A run that read something the subject does not trust for ``request_work``."""
        return cls(True, evidence)

    @classmethod
    def of(cls, value: Union[Provenance, Mapping, bool, None]) -> Provenance:
        """``value`` as a :class:`Provenance`: a record, its dict, a bool, or None."""
        if isinstance(value, Provenance):
            return value
        if value is None:
            return cls.unknown()
        if isinstance(value, bool):
            return cls(value)
        if isinstance(value, Mapping):
            tainted = value.get("tainted")
            if tainted is not None and not isinstance(tainted, bool):
                raise TypeError(f"provenance.tainted is true, false or null, not {tainted!r}")
            return cls(tainted, tuple(str(e) for e in value.get("evidence") or ()))
        raise TypeError(
            f"provenance is a Provenance, its dict, a bool or None, not {type(value).__name__}"
        )

    def to_dict(self) -> dict:
        """JSON-ready."""
        return {"tainted": self.tainted, "evidence": list(self.evidence)}


@dataclass(frozen=True, kw_only=True)
class OutboundPolicy:
    """The subject's policy as it applies to one message (discussion §5.4).

    ``reply_mode`` is the mode in force for the recipient (``draft`` waits for the
    operator). ``tainted_runs`` is ``approve`` (the taint rule applies) or ``send`` (the
    subject waives it). ``resumable`` is whether a ``revise`` can go back to the
    processor; ``None`` means "when the message belongs to a case". ``ai_tolerance`` is the
    recipient's, from their record, and ``disclosure_decision`` the decision recorded on
    the draft about saying the text is machine-written (``None``: none recorded).
    ``mode`` is ``enforce`` or ``shadow`` and is carried on the verdict.
    """

    reply_mode: str = "direct"
    tainted_runs: str = TAINTED_RUNS_APPROVE
    resumable: Optional[bool] = None
    ai_tolerance: Optional[str] = None
    disclosure_decision: Optional[str] = None
    mode: str = ENFORCE

    def __post_init__(self) -> None:
        if self.tainted_runs not in (TAINTED_RUNS_APPROVE, TAINTED_RUNS_SEND):
            raise ValueError(
                f"tainted_runs is {TAINTED_RUNS_APPROVE!r} or {TAINTED_RUNS_SEND!r}, "
                f"not {self.tainted_runs!r}"
            )
        if self.mode not in MODES:
            raise ValueError(f"mode is one of {MODES}, not {self.mode!r}")

    @classmethod
    def of(cls, value: Union[OutboundPolicy, Mapping, None]) -> OutboundPolicy:
        """``value`` as an :class:`OutboundPolicy`: a record, its dict, or None (defaults)."""
        if isinstance(value, OutboundPolicy):
            return value
        if value is None:
            return cls()
        if isinstance(value, Mapping):
            known = {f.name for f in fields(cls)}
            unknown = sorted(set(value) - known)
            if unknown:
                raise ValueError(f"unknown policy keys: {', '.join(unknown)}")
            return cls(**value)
        raise TypeError(
            f"policy is an OutboundPolicy, its dict or None, not {type(value).__name__}"
        )

    def to_dict(self) -> dict:
        """JSON-ready."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class Reader:
    """A reader the ceiling is judged on: their clearance (``None``: no ceiling), in words.

    ``person`` is the person id when the reader is one; a class of readers ("anyone") has
    none.
    """

    clearance: Optional[str]
    who: str
    person: Optional[str] = None

    def to_dict(self) -> dict:
        """JSON-ready."""
        return {"clearance": self.clearance, "who": self.who, "person": self.person}


@dataclass(frozen=True, kw_only=True)
class Reason:
    """One rule that fired: the finding and the reader it concerns, and a sentence.

    ``flow`` is the minimum flow this hit asks for. ``text`` is what the operator reads;
    it never holds the matched text.
    """

    rule: str
    flow: str
    text: str
    finding: Optional[Finding] = None
    reader: Optional[str] = None

    def to_dict(self) -> dict:
        """JSON-ready."""
        return {
            "rule": self.rule,
            "flow": self.flow,
            "text": self.text,
            "finding": None if self.finding is None else self.finding.to_dict(),
            "reader": self.reader,
        }


@dataclass(frozen=True, kw_only=True)
class Verdict:
    """What the policy decided about one message, and why (discussion §5.1).

    ``flow`` is one of :data:`FLOWS` and ``route`` its :data:`ROUTES` entry. ``reasons``
    are every rule that fired, most restrictive first. ``axes`` are the values the decision
    was made on: ``audience`` (the scope), ``sensitivity`` (the highest finding severity,
    0 when nothing was found), ``relationship`` (the most restrictive standing among the
    explicit recipients, a tier or ``stranger``), ``irreversible`` (the audience is not
    retractable) and ``tainted`` (true, false, or ``None`` for unknown). ``least_cleared``
    is the reader the content ceiling came from. ``payload_hash`` and ``audience_hash``
    are what an approval binds to; ``as_of`` is the ``now`` the verdict was made at, and
    ``mode`` the policy's.
    """

    flow: str
    route: str
    reasons: tuple[Reason, ...]
    axes: Mapping[str, Any]
    least_cleared: Reader
    findings: tuple[Finding, ...]
    payload_hash: str
    audience_hash: str
    as_of: str
    mode: str

    def to_dict(self) -> dict:
        """JSON-ready: what the ledger records for a gated message."""
        return {
            "flow": self.flow,
            "route": self.route,
            "reasons": [reason.to_dict() for reason in self.reasons],
            "axes": {axis: self.axes[axis] for axis in AXES},
            "least_cleared": self.least_cleared.to_dict(),
            "findings": [finding.to_dict() for finding in self.findings],
            "payload_hash": self.payload_hash,
            "audience_hash": self.audience_hash,
            "as_of": self.as_of,
            "mode": self.mode,
        }

    @property
    def rules(self) -> tuple[str, ...]:
        """The names of the rules that fired, most restrictive first, each once."""
        return tuple(dict.fromkeys(reason.rule for reason in self.reasons))


# ---- flows ----


def flow_rank(flow: str) -> int:
    """Where ``flow`` stands in :data:`FLOWS`; a ``ValueError`` for an unknown flow."""
    if flow not in FLOWS:
        raise ValueError(f"flow {flow!r} is not one of: {', '.join(FLOWS)}")
    return FLOWS.index(flow)


def most_restrictive(flows: Iterable[str]) -> str:
    """The highest of ``flows``, or ``send`` when there are none."""
    return max(flows, key=flow_rank, default=SEND)


def _label_rank(label: Optional[str]) -> int:
    """A label's place in :data:`~liaise.detect.LABELS`; an unknown label is the lowest."""
    return LABELS.index(label) if label in LABELS else 0


def above(label: Optional[str], clearance: Optional[str]) -> bool:
    """Whether ``label`` is more restrictive than ``clearance`` (``None``: no ceiling).

    >>> above("amber", "clear"), above("amber", "amber"), above("red", None)
    (True, False, False)
    """
    if clearance is None or label is None:
        return False
    return _label_rank(label) > _label_rank(clearance)


def _lowest_label(labels: Iterable[Optional[str]]) -> str:
    return min(labels, key=_label_rank, default=LABELS[0])


# ---- the message ----


def _field(source: Any, name: str, default: Any = None) -> Any:
    """``source.name`` or ``source[name]``, else ``default``."""
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _text_list(name: str, value: Any) -> list[str]:
    """``value`` as a list of stripped, non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        raise TypeError(f"{name} is a collection of strings, not the string {value!r}")
    return [str(v).strip() for v in value if str(v).strip()]


def _attachment_name(attachment: Any) -> str:
    if isinstance(attachment, str):
        return attachment
    if isinstance(attachment, Mapping):
        return str(attachment.get("name") or attachment.get("filename") or "")
    return str(getattr(attachment, "name", None) or getattr(attachment, "filename", ""))


def payload_of(outbound: Any) -> dict:
    """The message as the payload hash sees it: recipients, copies, ref, title, text, attachment names.

    >>> payload_of({"ref": "github:example/app#12", "recipient": "ada", "text": "hi"})
    {'recipients': ['ada'], 'cc': [], 'bcc': [], 'ref': 'github:example/app#12', 'title': None, 'text': 'hi', 'attachments': []}
    """
    text = _field(outbound, "text", "")
    if not isinstance(text, str):
        raise TypeError(f"the message text must be a str, not {type(text).__name__}")
    title = _field(outbound, "title")
    recipient = _field(outbound, "recipient")
    return {
        "recipients": [str(recipient)] if recipient else [],
        "cc": _text_list("cc", _field(outbound, "cc", ())),
        "bcc": _text_list("bcc", _field(outbound, "bcc", ())),
        "ref": str(_field(outbound, "ref", "") or ""),
        "title": None if title is None else str(title),
        "text": text,
        "attachments": [
            name
            for name in map(_attachment_name, _field(outbound, "attachments", ()) or ())
            if name
        ],
    }


def canonical_json(data: Any) -> str:
    """The canonical JSON both hashes are taken over: sorted keys, no spaces, ASCII."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _digest(data: Any) -> str:
    return sha256(canonical_json(data).encode("utf-8")).hexdigest()


def payload_hash(outbound: Any) -> str:
    """SHA-256, in hex, of the canonical JSON of :func:`payload_of`."""
    return _digest(payload_of(outbound))


# ---- the audience ----


def _reader_record(reader: Any) -> dict:
    """A listed reader as correspond's ``ChannelIdentity.to_dict`` writes it."""
    if isinstance(reader, str):
        channel, _, rest = reader.partition(":")
        reader = {"channel": channel, "handle": rest or None, "native_id": rest}
    elif not isinstance(reader, Mapping):
        reader = reader.to_dict() if hasattr(reader, "to_dict") else vars(reader)
    record = {name: reader.get(name, default) for name, default in READER_DEFAULTS.items()}
    record["address"] = reader.get("address") or (
        f"{record['channel']}:{record['handle'] or record['native_id']}"
    )
    return record


def _sorted_texts(name: str, values: Any) -> list[str]:
    if isinstance(values, str):
        raise TypeError(f"audience {name} is a collection of strings, not {values!r}")
    return sorted({str(v) for v in values or ()})


def audience_record(audience: Any) -> dict:
    """``audience`` as the dict correspond's ``Audience.to_dict`` writes, normalised alike.

    ``None``, or a record without a scope, is the unknown audience: public, defaulted,
    with every reason in its evidence. A defaulted audience is public whatever its
    ``scope`` says, as correspond's constructor insists. Unknown keys are dropped.
    """
    if audience is None:
        audience = {}
    elif not isinstance(audience, Mapping):
        if not hasattr(audience, "to_dict"):
            raise TypeError(
                f"audience is a correspond Audience record (a dict) or None, "
                f"not {type(audience).__name__}"
            )
        audience = audience.to_dict()
    scope = audience.get("scope")
    scope = getattr(scope, "value", scope)
    record = {name: audience.get(name, default) for name, default in AUDIENCE_DEFAULTS.items()}
    if scope not in SCOPES or record["defaulted"] is True:
        evidence = [str(e) for e in record["evidence"] or ()]
        if scope not in SCOPES:
            evidence.append(
                "no audience was given" if scope is None else f"audience scope {scope!r} is unknown"
            )
            record.update(readers=(), classes=(), external=None, durability=DURABILITY, widening=WIDENING)
        if UNKNOWN_AUDIENCE_EVIDENCE not in evidence:
            evidence.append(UNKNOWN_AUDIENCE_EVIDENCE)
        record.update(scope=PUBLIC, complete=False, defaulted=True, evidence=evidence)
        if record["retractable"]:
            record["retractable"] = False
    else:
        record["scope"] = scope
    readers = [_reader_record(reader) for reader in record["readers"] or ()]
    unique = {canonical_json(reader): reader for reader in readers}
    record["readers"] = [unique[key] for key in sorted(unique)]
    for name in ("classes", "durability", "widening"):
        record[name] = _sorted_texts(name, record[name])
    record["evidence"] = [str(e) for e in record["evidence"] or ()]
    as_of = record["as_of"]
    record["as_of"] = as_of.isoformat() if isinstance(as_of, datetime) else as_of
    for name in ("complete", "retractable", "defaulted"):
        record[name] = bool(record[name])
    if record["external"] is not None:
        record["external"] = bool(record["external"])
    return record


def audience_hash(audience: Any) -> str:
    """SHA-256, in hex, of the canonical JSON of :func:`audience_record` without ``as_of`` and ``evidence``.

    Equal to correspond's ``Audience.hash`` for the same record: the cross-package contract
    an approval binds to.
    """
    record = audience_record(audience)
    return _digest({k: v for k, v in record.items() if k not in AUDIENCE_UNHASHED})


def audience_in_words(audience: Any) -> str:
    """The audience in one line, as correspond's ``Audience.in_words`` says it."""
    record = audience_record(audience)
    if record["defaulted"]:
        parts = ["world-readable (assumed: the audience could not be determined)"]
    else:
        scope = record["scope"]
        parts = [_SCOPE_WORDS[scope]]
        readers = record["readers"]
        if readers and scope != PUBLIC:
            noun = "reader" if len(readers) == 1 else "readers"
            parts.append(
                f"exactly {len(readers)} {noun}"
                if record["complete"]
                else f"at least {len(readers)} known {noun}"
            )
        parts += [_CLASS_WORDS.get(c, c) for c in record["classes"]]
        if "indexed" in record["durability"] and scope != PUBLIC:
            parts.append("indexed by search")
        if "archived_by_others" in record["durability"]:
            parts.append("archived by others")
        if "edit_history_visible" in record["durability"]:
            parts.append("edits keep a visible history")
    parts.append("retractable" if record["retractable"] else "not retractable")
    return "; ".join(parts)


# ---- readers and the ceiling ----


def _findings(findings: Iterable[Any]) -> tuple[Finding, ...]:
    """``findings`` as :class:`~liaise.detect.Finding` records, in position order."""
    records = []
    for finding in findings or ():
        if isinstance(finding, Finding):
            records.append(finding)
        elif isinstance(finding, Mapping):
            records.append(
                Finding(
                    kind=finding["kind"],
                    start=int(finding["start"]),
                    end=int(finding["end"]),
                    entity=finding.get("entity"),
                    label=finding.get("label"),
                    sealed_from=tuple(finding.get("sealed_from") or ()),
                    rule=finding["rule"],
                    severity=int(finding["severity"]),
                    fingerprint=str(finding.get("fingerprint") or ""),
                )
            )
        else:
            raise TypeError(
                f"a finding is a liaise.detect.Finding or its dict, not {type(finding).__name__}"
            )
    return tuple(sorted(records, key=lambda f: (f.start, f.end, f.kind, f.rule)))


def _people(disclosure: Mapping) -> dict[str, Mapping]:
    people = disclosure.get("people") or {}
    if not isinstance(people, Mapping):
        raise TypeError("disclosure.people is a mapping of person id to their standing")
    return {str(person): (entry if isinstance(entry, Mapping) else {}) for person, entry in people.items()}


def _gap_texts(disclosure: Mapping) -> frozenset[str]:
    """Every reader the disclosure could not resolve, as it was named to it."""
    gaps = disclosure.get("gaps") or {}
    texts: set[str] = set()
    for values in (gaps.values() if isinstance(gaps, Mapping) else ()):
        for value in values or ():
            texts.add(str(value))
            texts.add(str(value).partition(" (")[0])
    return frozenset(texts)


def _disclosure_saw(audience: Mapping, disclosure: Mapping) -> bool:
    """Whether the disclosure was computed for this audience, so its readers are resolved."""
    about = disclosure.get("audience")
    return isinstance(about, Mapping) and bool(audience["ref"]) and about.get("ref") == audience["ref"]


def _clearance_of(entry: Mapping) -> str:
    clearance = entry.get("clearance")
    return clearance if clearance in LABELS else LABELS[0]


@dataclass(frozen=True)
class _Resolution:
    """Who the explicit recipients and the listed readers turned out to be."""

    #: person id -> the disclosure's entry (``{}`` for a person it does not know).
    readers: Mapping[str, Mapping]
    #: (as given, person id or None) for the recipient, then cc, then bcc.
    recipients: tuple[tuple[str, Optional[str]], ...]
    #: listed readers, or recipients, that resolved to nobody.
    unresolved: tuple[str, ...]


def _resolve(
    payload: Mapping,
    audience: Mapping,
    disclosure: Mapping,
    identities: Optional[Mapping[str, Optional[str]]],
) -> _Resolution:
    people = _people(disclosure)
    identities = dict(identities or {})
    gaps = _gap_texts(disclosure)
    saw = _disclosure_saw(audience, disclosure)
    readers: dict[str, Mapping] = dict(people)
    unresolved: list[str] = []

    def resolve(given: str) -> tuple[Optional[str], bool]:
        """``(person id, resolved)``: the id is None for a reader the disclosure knows
        only as one of its people (a listed reader it resolved itself)."""
        if given in people:
            return given, True
        if given in identities:
            person = identities[given]
            if person is not None:
                readers.setdefault(person, {})
            return person, person is not None
        return None, False

    recipients = []
    for given in (*payload["recipients"], *payload["cc"], *payload["bcc"]):
        person, _ = resolve(given)
        if person is None:
            unresolved.append(given)
        recipients.append((given, person))
    for reader in audience["readers"]:
        if reader["is_self"]:
            continue
        address = reader["address"]
        _, resolved = resolve(address)
        # A listed reader the disclosure saw and did not report as a gap is one of its
        # people, whichever one; without that, an identity nobody resolved is at clear.
        if not resolved and not (saw and address not in gaps):
            unresolved.append(address)
    return _Resolution(readers, tuple(recipients), tuple(dict.fromkeys(unresolved)))


def _ceiling(audience: Mapping, disclosure: Mapping) -> Optional[Reader]:
    """The reader class the scope adds, or ``None`` when the scope adds none."""
    scope = audience["scope"]
    ref = audience["ref"] or "the destination"
    if audience["defaulted"]:
        return Reader(LABELS[0], f"anyone (the audience of {ref} could not be determined)")
    if scope == PUBLIC:
        return Reader(LABELS[0], f"anyone ({audience_in_words(audience)})")
    if scope in (ORG, GROUP) and not audience["complete"]:
        about = disclosure.get("audience") if _disclosure_saw(audience, disclosure) else None
        organisation = (about or {}).get("organisation")
        ceiling = (about or {}).get("ceiling")
        noun = "organisation" if scope == ORG else "group"
        if ceiling in LABELS and organisation:
            return Reader(ceiling, f"the unlisted members of {organisation} (cleared to {ceiling})")
        return Reader(LABELS[0], f"the unlisted members of the {noun} behind {ref}")
    return None


def least_cleared_reader(
    audience: Any,
    disclosure: Mapping,
    *,
    identities: Optional[Mapping[str, Optional[str]]] = None,
    recipients: Iterable[str] = (),
) -> Reader:
    """The reader whose clearance is the content ceiling (discussion §4.4).

    ``recipients`` are the explicit recipients (person ids or addresses) to count among
    the readers, an unresolved one at ``clear``. The result's ``clearance`` is ``None``
    for the operator alone, and ``clear`` for anything public, defaulted or unresolved.

    >>> ada = {"people": {"ada": {"tier": "open", "clearance": "amber"}}}
    >>> email = {"ref": "email:ada", "scope": "named", "readers": ["email:ada"]}
    >>> least_cleared_reader(email, ada, identities={"email:ada": "ada"}, recipients=["ada"])
    Reader(clearance='amber', who='ada', person='ada')
    >>> least_cleared_reader(email, ada, recipients=["ada"]).who  # nobody tied the address to Ada
    'email:ada, who has no record'
    >>> least_cleared_reader({"ref": "github:example/app#1", "scope": "public"}, ada).clearance
    'clear'
    """
    record = audience_record(audience)
    payload = {"recipients": list(recipients), "cc": [], "bcc": []}
    resolution = _resolve(payload, record, disclosure, identities)
    return _least_cleared(record, disclosure, resolution)


def _least_cleared(audience: Mapping, disclosure: Mapping, resolution: _Resolution) -> Reader:
    if audience["scope"] == OPERATOR and not audience["defaulted"]:
        return Reader(None, "the operator")
    candidates: list[Reader] = []
    ceiling = _ceiling(audience, disclosure)
    if ceiling is not None:
        candidates.append(ceiling)
    for person, entry in resolution.readers.items():
        candidates.append(Reader(_clearance_of(entry), person, person))
    for given in resolution.unresolved:
        candidates.append(Reader(LABELS[0], f"{given}, who has no record"))
    if not candidates:
        candidates.append(Reader(LABELS[0], "an unlisted reader"))
    return min(candidates, key=lambda reader: _label_rank(reader.clearance))


# ---- the facts ----


@dataclass(frozen=True, kw_only=True)
class Facts:
    """Everything a rule may look at, computed once from the inputs of :func:`evaluate`."""

    payload: Mapping[str, Any]
    audience: Mapping[str, Any]
    disclosure: Mapping[str, Any]
    findings: tuple[Finding, ...]
    provenance: Provenance
    policy: OutboundPolicy
    now: datetime
    readers: Mapping[str, Mapping]
    recipients: tuple[tuple[str, Optional[str]], ...]
    unresolved: tuple[str, ...]
    least_cleared: Reader
    resumable: bool
    case_id: Optional[str]

    @property
    def scope(self) -> str:
        """The audience's scope."""
        return self.audience["scope"]

    @property
    def ref(self) -> str:
        """The destination, for the operator's sentence."""
        return self.audience["ref"] or self.payload["ref"] or "the destination"

    @property
    def tainted(self) -> Optional[bool]:
        """Whether the run is tainted; ``None`` when unknown."""
        return self.provenance.tainted

    def of_kind(self, *kinds: str) -> tuple[Finding, ...]:
        """The findings whose kind is one of ``kinds``."""
        return tuple(f for f in self.findings if f.kind in kinds)

    def sealed_readers(self, finding: Finding) -> tuple[str, ...]:
        """The resolved readers the finding's entity is sealed from."""
        if finding.entity is None:
            return ()
        sealed = set(finding.sealed_from)
        for seal in self.disclosure.get("seals") or ():
            if isinstance(seal, Mapping) and seal.get("entity") == finding.entity:
                sealed.add(str(seal.get("from")))
        return tuple(person for person in self.readers if person in sealed)

    @property
    def leaks(self) -> tuple[Finding, ...]:
        """The findings that name something a reader is not cleared for, or sealed from."""
        return tuple(
            f
            for f in self.findings
            if f.label is not None
            and (above(f.label, self.least_cleared.clearance) or self.sealed_readers(f))
        )

    def entry(self, person: Optional[str]) -> Mapping:
        """The disclosure's entry for ``person`` (``{}`` when it has none)."""
        return self.readers.get(person or "", {})

    def lapsed(self, entry: Mapping) -> bool:
        """Whether a permissive tier is past its review date, by the entry or by ``now``."""
        if entry.get("lapsed") is True:
            return True
        review_by = entry.get("review_by")
        tier = entry.get("recorded_tier") or entry.get("tier")
        if tier not in PERMISSIVE_TIERS or not review_by:
            return False
        try:
            due = date.fromisoformat(str(review_by)[:10])
        except ValueError:
            return True  # a review date nobody can read has passed
        return due < self.now.date()


def _where(finding: Finding) -> str:
    return f"at characters {finding.start}–{finding.end}"


def _describe(finding: Finding) -> str:
    """A finding in words, never its value: kind, rule and place."""
    what = {
        "secret": f"a secret ({finding.rule})",
        "canary": "a canary term",
        "vocabulary": f"'{finding.entity}' ({finding.label})",
        "third_party": f"'{finding.entity}' ({finding.label})",
        "exfiltration": f"an exfiltration shape ({finding.rule})",
        "personal": f"a personal detail ({finding.rule})",
    }.get(finding.kind, f"a {finding.kind} finding ({finding.rule})")
    return f"{what} {_where(finding)}"


# ---- the rules ----


@dataclass(frozen=True)
class Hit:
    """One thing a rule found: its sentence, the finding and reader concerned, and the flow
    the rule asks for (``None``: the rule's declared minimum)."""

    text: str
    finding: Optional[Finding] = None
    reader: Optional[str] = None
    flow: Optional[str] = None


Predicate = Callable[[Facts], Iterable[Hit]]


@dataclass(frozen=True)
class Rule:
    """One row of the table: a name, what it looks for, and its minimum flow."""

    name: str
    predicate: Predicate
    flow: str

    def __post_init__(self) -> None:
        flow_rank(self.flow)

    def hits(self, facts: Facts) -> Iterator[Reason]:
        """The reasons this rule contributes for ``facts``."""
        for hit in self.predicate(facts):
            yield Reason(
                rule=self.name,
                flow=hit.flow or self.flow,
                text=hit.text,
                finding=hit.finding,
                reader=hit.reader,
            )


def _resumable_flow(facts: Facts) -> str:
    return REVISE if facts.resumable else APPROVE


def secrets(facts: Facts) -> Iterator[Hit]:
    """Any ``secret`` or ``canary`` finding."""
    for finding in facts.of_kind(*SECRET_KINDS):
        what = "a canary term planted in private context" if finding.kind == "canary" else _describe(finding)
        yield Hit(f"{what}: a secret is never sent, whatever the audience", finding)


def seals(facts: Facts) -> Iterator[Hit]:
    """A resolved reader is in the ``sealed_from`` of a finding's entity."""
    for finding in facts.findings:
        for person in facts.sealed_readers(finding):
            yield Hit(
                f"'{finding.entity}' is sealed from {person}, who reads {facts.ref} "
                f"({_where(finding)})",
                finding,
                person,
            )


def exfiltration(facts: Facts) -> Iterator[Hit]:
    """An ``exfiltration`` finding, and a scope beyond ``named`` or external readers."""
    if facts.scope not in WIDE_SCOPES and facts.audience["external"] is not True:
        return
    why = "external readers" if facts.scope not in WIDE_SCOPES else f"the audience is {facts.scope}"
    for finding in facts.of_kind(EXFILTRATION):
        if finding.rule == LINK_RULE:
            yield Hit(
                f"a link to a host outside the allowlist {_where(finding)}, and {why}: "
                f"shown in full for the operator to release",
                finding,
                flow=APPROVE,
            )
        else:
            yield Hit(f"{_describe(finding)}, and {why}", finding)


def personal_public(facts: Facts) -> Iterator[Hit]:
    """A ``personal`` finding and scope ``org`` or ``public``."""
    if facts.scope not in BROAD_SCOPES:
        return
    for finding in facts.of_kind(PERSONAL):
        yield Hit(f"{_describe(finding)}, and the audience is {facts.scope}", finding)


def no_write_down(facts: Facts) -> Iterator[Hit]:
    """A labelled entity's term above the least-cleared reader's clearance."""
    least = facts.least_cleared
    for finding in facts.findings:
        if finding.kind == THIRD_PARTY or finding.label is None:
            continue
        if above(finding.label, least.clearance):
            yield Hit(
                f"'{finding.entity}' is {finding.label} ({_where(finding)}); the least-cleared "
                f"reader of {facts.ref} is {least.who}, cleared to {least.clearance}",
                finding,
                least.person,
                flow=_resumable_flow(facts),
            )


def co_ownership(facts: Facts) -> Iterator[Hit]:
    """A ``third_party`` finding whose subject's label is above a reader's clearance."""
    readers: list[Reader] = [
        Reader(_clearance_of(entry), person, person) for person, entry in facts.readers.items()
    ]
    least = facts.least_cleared
    if least.person is None and least.clearance is not None:
        readers.append(least)
    for finding in facts.of_kind(THIRD_PARTY):
        for reader in readers:
            if above(finding.label, reader.clearance):
                yield Hit(
                    f"'{finding.entity}' is {finding.label} ({_where(finding)}), and "
                    f"{reader.who} is cleared to {reader.clearance}: their news is theirs to tell",
                    finding,
                    reader.person,
                    flow=_resumable_flow(facts),
                )


def personal_private(facts: Facts) -> Iterator[Hit]:
    """A ``personal`` finding and scope ``named`` or ``group``."""
    if facts.scope not in (NAMED, GROUP):
        return
    for finding in facts.of_kind(PERSONAL):
        yield Hit(f"{_describe(finding)}, to {facts.scope} readers", finding)


def tier(facts: Facts) -> Iterator[Hit]:
    """A recipient's tier is ``reviewed``, a permissive tier has lapsed, or a review is due."""
    for _, person in facts.recipients:
        if person is None:
            continue
        entry = facts.entry(person)
        if entry.get("tier") == REVIEWED:
            yield Hit(f"{person} is reviewed: every message to them is released by the operator", reader=person)
        if facts.lapsed(entry):
            recorded = entry.get("recorded_tier") or entry.get("tier")
            yield Hit(
                f"{person}'s {recorded} tier is past its review date ({entry.get('review_by')}): "
                f"recertify it or let it lapse",
                reader=person,
            )
        review = entry.get("review")
        if review:
            items = "; ".join(str(item) for item in review)
            yield Hit(f"{person}'s record awaits the operator's review: {items}", reader=person)


def stranger(facts: Facts) -> Iterator[Hit]:
    """An explicit recipient with no acquaint record."""
    for given, person in facts.recipients:
        if person is None:
            yield Hit(f"{given} has no acquaint record: a stranger is written to by the operator", reader=given)


def disclosure_stance(facts: Facts) -> Iterator[Hit]:
    """The recipient is averse to machine-written text and the draft records no decision."""
    if facts.policy.ai_tolerance == AVERSE and facts.policy.disclosure_decision is None:
        recipient = "the recipient"
        if facts.recipients:
            recipient = facts.recipients[0][1] or facts.recipients[0][0]
        yield Hit(
            f"{recipient} is averse to machine-written text and the draft records no "
            f"decision about saying so",
            reader=recipient,
        )


def taint(facts: Facts) -> Iterator[Hit]:
    """The run is tainted or its provenance unknown, and the audience is wider than the operator."""
    if facts.policy.tainted_runs == TAINTED_RUNS_SEND or facts.scope == OPERATOR:
        return
    if facts.tainted is False:
        return
    if facts.tainted is None:
        why = "the run's provenance is unknown, which counts as tainted"
    else:
        evidence = "; ".join(facts.provenance.evidence) or "an inbound message the subject does not trust"
        why = f"the run read untrusted input ({evidence})"
    leaks = facts.leaks
    if leaks:
        named = ", ".join(dict.fromkeys(f"'{f.entity}'" for f in leaks))
        yield Hit(
            f"{why}, the audience of {facts.ref} is {facts.scope}, and the message names "
            f"{named}, which that audience is not cleared for: an injection that got private "
            f"content out is never released as written",
            leaks[0],
            flow=REFUSE,
        )
    else:
        yield Hit(f"{why}, and the audience of {facts.ref} is {facts.scope}: the operator releases it")


def reply_mode(facts: Facts) -> Iterator[Hit]:
    """``draft`` reply mode for this recipient or subject."""
    if facts.policy.reply_mode == DRAFT_REPLY_MODE:
        recipient = facts.recipients[0][0] if facts.recipients else "the recipient"
        yield Hit(f"draft reply mode for {recipient}: the operator releases every message", reader=recipient)


def irreversibility(facts: Facts) -> Iterator[Hit]:
    """Not retractable, and scope ``org`` or ``public``."""
    if not facts.audience["retractable"] and facts.scope in BROAD_SCOPES:
        yield Hit(
            f"a send to {facts.ref} cannot be withdrawn ({audience_in_words(facts.audience)}): "
            f"held for a cancellable window"
        )


def unknown_audience(facts: Facts) -> Iterator[Hit]:
    """``defaulted`` is true: nothing by itself; the ceiling is ``clear``."""
    if facts.audience["defaulted"]:
        evidence = "; ".join(facts.audience["evidence"]) or UNKNOWN_AUDIENCE_EVIDENCE
        yield Hit(f"the audience of {facts.ref} could not be determined ({evidence}): assumed public, ceiling clear")


#: The policy table (discussion §5.4), in its order. Not a seam: the rows and their flows
#: are the design; ``evaluate(rules=...)`` exists so a test can remove or weaken one row
#: and show that a scenario then gets a less restrictive verdict.
RULES: tuple[Rule, ...] = (
    Rule("secrets", secrets, REFUSE),
    Rule("seals", seals, REFUSE),
    Rule("exfiltration", exfiltration, REFUSE),
    Rule("personal, public", personal_public, REFUSE),
    Rule("no write-down", no_write_down, REVISE),
    Rule("co-ownership", co_ownership, REVISE),
    Rule("personal, private", personal_private, APPROVE),
    Rule("tier", tier, APPROVE),
    Rule("stranger", stranger, APPROVE),
    Rule("disclosure stance", disclosure_stance, APPROVE),
    Rule("taint", taint, APPROVE),
    Rule("reply mode", reply_mode, APPROVE),
    Rule("irreversibility", irreversibility, DELAY),
    Rule("unknown audience", unknown_audience, SEND),
)
RULE_NAMES = tuple(rule.name for rule in RULES)


# ---- the verdict ----


def _relationship(facts: Facts) -> str:
    """The most restrictive standing among the explicit recipients."""
    standings = []
    for given, person in facts.recipients:
        if person is None:
            standings.append(STRANGER)
            continue
        entry = facts.entry(person)
        tier_value = entry.get("tier")
        if tier_value not in TIERS:
            tier_value = "need-to-know"
        standings.append(tier_value)
    return max(standings, key=RELATIONSHIPS.index, default="need-to-know")


def facts_of(
    outbound: Any,
    *,
    audience: Any,
    disclosure: Optional[Mapping],
    findings: Iterable[Any],
    provenance: Union[Provenance, Mapping, bool, None],
    policy: Union[OutboundPolicy, Mapping, None] = None,
    now: datetime,
    identities: Optional[Mapping[str, Optional[str]]] = None,
) -> Facts:
    """The :class:`Facts` of :func:`evaluate`'s inputs, for a rule or a report to read."""
    if not isinstance(now, datetime):
        raise TypeError(f"now is a datetime, not {type(now).__name__}")
    disclosure = {} if disclosure is None else disclosure
    if not isinstance(disclosure, Mapping):
        raise TypeError(
            f"disclosure is acquaint.disclosure's JSON (a mapping) or {{}}, not {type(disclosure).__name__}"
        )
    payload = payload_of(outbound)
    record = audience_record(audience)
    policy = OutboundPolicy.of(policy)
    resolution = _resolve(payload, record, disclosure, identities)
    case_id = _field(outbound, "case_id")
    resumable = policy.resumable if policy.resumable is not None else case_id is not None
    return Facts(
        payload=payload,
        audience=record,
        disclosure=disclosure,
        findings=_findings(findings),
        provenance=Provenance.of(provenance),
        policy=policy,
        now=now,
        readers=resolution.readers,
        recipients=resolution.recipients,
        unresolved=resolution.unresolved,
        least_cleared=_least_cleared(record, disclosure, resolution),
        resumable=bool(resumable),
        case_id=None if case_id is None else str(case_id),
    )


def evaluate(
    outbound: Any,
    *,
    audience: Any,
    disclosure: Optional[Mapping],
    findings: Iterable[Any],
    provenance: Union[Provenance, Mapping, bool, None],
    policy: Union[OutboundPolicy, Mapping, None] = None,
    now: datetime,
    identities: Optional[Mapping[str, Optional[str]]] = None,
    rules: Sequence[Rule] = RULES,
) -> Verdict:
    """The :class:`Verdict` for ``outbound``, through every rule of ``rules``.

    Pure: no I/O, no clock (``now`` is given), and the same verdict for the same inputs.
    See the module docstring for what each input is.

    >>> from datetime import datetime, timezone
    >>> now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    >>> ada = {"people": {"ada": {"tier": "open", "clearance": "amber"}}}
    >>> email = {"ref": "email:ada", "scope": "named", "readers": ["email:ada"]}
    >>> message = {"ref": "email:ada", "channel": "email", "recipient": "ada", "text": "hi", "case_id": "s-1"}
    >>> verdict = evaluate(message, audience=email, disclosure=ada, findings=(), provenance=False,
    ...                    now=now, identities={"email:ada": "ada"})
    >>> verdict.flow, verdict.route, verdict.rules
    ('send', 'send', ())
    >>> evaluate(message, audience=None, disclosure=ada, findings=(), provenance=False, now=now).rules
    ('irreversibility', 'unknown audience')
    """
    facts = facts_of(
        outbound,
        audience=audience,
        disclosure=disclosure,
        findings=findings,
        provenance=provenance,
        policy=policy,
        now=now,
        identities=identities,
    )
    reasons = [reason for rule in rules for reason in rule.hits(facts)]
    reasons.sort(key=lambda reason: -flow_rank(reason.flow))  # stable: table order within a flow
    flow = most_restrictive(reason.flow for reason in reasons)
    axes = {
        "audience": facts.scope,
        "sensitivity": max((f.severity for f in facts.findings), default=0),
        "relationship": _relationship(facts),
        "irreversible": not facts.audience["retractable"],
        "tainted": facts.tainted,
    }
    return Verdict(
        flow=flow,
        route=ROUTES[flow],
        reasons=tuple(reasons),
        axes=axes,
        least_cleared=facts.least_cleared,
        findings=facts.findings,
        payload_hash=_digest(facts.payload),
        audience_hash=_digest({k: v for k, v in facts.audience.items() if k not in AUDIENCE_UNHASHED}),
        as_of=now.isoformat(),
        mode=facts.policy.mode,
    )


__all__ = [
    "AXES",
    "FLOWS",
    "ROUTES",
    "RULES",
    "RULE_NAMES",
    "SCOPES",
    "TIERS",
    "Facts",
    "Hit",
    "OutboundPolicy",
    "Provenance",
    "Reader",
    "Reason",
    "Rule",
    "Verdict",
    "above",
    "audience_hash",
    "audience_in_words",
    "audience_record",
    "canonical_json",
    "evaluate",
    "facts_of",
    "flow_rank",
    "least_cleared_reader",
    "most_restrictive",
    "payload_hash",
    "payload_of",
]
