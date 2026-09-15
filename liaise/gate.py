"""The outbound gate: the checks a message passes before liaise sends it.

A processor run reports outcomes, :mod:`liaise.outcomes` plans them into actions, and
each :class:`~liaise.outcomes.Send` among those is an :class:`Outbound` that the tick
hands to :func:`run_gate` before anything reaches a channel. The gate runs
:data:`DFLT_OUTBOUND_FILTERS`, in this order:

1. :func:`reply_mode`: nothing goes directly to a person in ``draft`` reply mode, unless
   the operator released it.
2. :func:`leak_scan`: on a public channel, nothing holding an absolute local path, a
   ``.env`` path, an email address, a private key, a token (wrapped across lines or not)
   or one of ``policy.leak_terms``. It never redacts.
3. :func:`writing_card`: a note with the recipient's acquaint writing card.
4. :func:`deslop`: nothing acquaint's style lint finds machine-sounding.
5. :func:`notify_recipient`: on GitHub, the message starts with ``@<login>``, since
   GitHub notifies only the people a comment mentions.

A filter is ``(outbound, ctx) -> Pass | Divert``. A :class:`Pass` hands the message,
possibly rewritten, to the next filter; only :func:`notify_recipient` rewrites. The
first :class:`Divert` ends the gate: the message is not sent, and goes to the operator
instead. Notes accumulate across the filters that ran. acquaint is optional
(``liaise[people]``): without it, or for a person it does not know, filters 3 and 4
add a note and let the message through.

The gate only decides. :func:`liaise.release.gate_and_send` sends
:attr:`GateDecision.send`, and its callers keep a diverted message as a draft (see
:func:`liaise.outcomes.make_draft`). The tick then notifies the operator. When the
operator releases a draft (``liaise case send-draft``), the same gate runs again on the
final text, with their :class:`~liaise.model.Approval` on :attr:`GateContext.approval`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Optional, Union

from liaise.detect import (
    EMAIL_PATTERN,
    ENV_FILE_PATTERN,
    LOCAL_PATH_PATTERNS,
    PRIVATE_KEY_PATTERN,
    TOKEN_SHAPES,
)
from liaise.model import Approval, Case
from liaise.subjects import Subject

#: The reply mode in which liaise sends nothing without the operator.
DRAFT_REPLY_MODE = "draft"
#: The channel whose messages must @mention their recipient to reach them.
MENTION_CHANNEL = "github"

#: What :func:`leak_scan` diverts on, as (kind, pattern): the 0.1 patterns, which
#: :mod:`liaise.detect` owns. Local paths are home directories on macOS, Linux and Windows,
#: a Windows home through a WSL mount, and macOS's temporary directories. An env file is a
#: path ending in ``.env``. No pattern has an unbounded quantifier ahead of a character it
#: requires, so the scan stays linear in the message's length.
_LEAK_PATTERNS = (
    *(("local path", pattern) for pattern in LOCAL_PATH_PATTERNS),
    ("env file", ENV_FILE_PATTERN),
    ("email", re.compile(EMAIL_PATTERN)),
    ("private key", re.compile(PRIVATE_KEY_PATTERN)),
    *(("token", re.compile(rf"\b{shape}")) for shape in TOKEN_SHAPES),
)
#: The token shapes as :func:`leak_scan` looks for them in the text with its line breaks
#: removed, so a token wrapped across lines is still found. Their word boundary is checked
#: against the message itself, since removing a line break can glue a word to a token.
_UNWRAPPED_TOKEN_PATTERNS = tuple(re.compile(shape) for shape in TOKEN_SHAPES)
#: The characters that break a line, and one that continues a word.
_LINE_BREAKS = frozenset("\r\n")
_WORD_CHAR = re.compile(r"\w")
#: A GitHub login: letters, digits and hyphens, at most 39 characters.
_GITHUB_LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")


@dataclass(frozen=True)
class Outbound:
    """A message liaise would send: ``text`` for ``recipient`` (a person id) at ``ref``.

    ``ref`` is the encoded conversation or address it goes to
    (``github:example/app#12``), and ``channel`` that ref's channel. ``purpose`` is the
    outcome kind it carries out (``ask``, ``reply``, ``propose``, ``deliver``).
    """

    case_id: str
    ref: str
    channel: str
    recipient: str
    purpose: str
    text: str


@dataclass(frozen=True)
class GateContext:
    """What the filters may consult: the subject and its policy, the case, the time.

    ``approval`` is the operator's release of this message (``liaise case send-draft``).
    It is None for every message the tick sends on its own.
    """

    subject: Subject
    case: Case
    now: datetime
    approval: Optional[Approval] = None


@dataclass(frozen=True)
class Pass:
    """A filter's verdict to go on, with ``outbound`` as the filter left it."""

    outbound: Outbound
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Divert:
    """A filter's verdict to send nothing and hand the message to the operator."""

    reason: str
    notes: tuple[str, ...] = ()


#: ``(outbound, ctx) -> Pass | Divert``: one check of the gate.
OutboundFilter = Callable[[Outbound, GateContext], Union[Pass, Divert]]


@dataclass(frozen=True)
class GateDecision:
    """What :func:`run_gate` decided: ``send`` a message, or why it was ``diverted``.

    Exactly one of ``send`` (the message as the filters left it) and ``diverted`` (the
    reason) is set. ``notes`` holds the notes of every filter that ran, in order.
    ``diverted_by`` names the filter that diverted, as an operator notification may say it:
    the reason can quote what a filter raised.
    """

    send: Optional[Outbound]
    diverted: Optional[str]
    notes: tuple[str, ...] = ()
    diverted_by: Optional[str] = None


def _acquaint_failure(error: Exception) -> str:
    if isinstance(error, ImportError):
        return f"acquaint could not be imported ({error}); install liaise[people]"
    return f"{type(error).__name__}: {error}"


# ---- the filters, in their default order ----


def reply_mode(outbound: Outbound, ctx: GateContext) -> Union[Pass, Divert]:
    """Divert when the recipient's reply mode is ``draft``, unless the operator released it.

    The mode is the person's ``policy.reply_modes`` override, else the subject's
    ``default_reply_mode`` (see :meth:`~liaise.subjects.Subject.reply_mode_for`). In
    ``draft`` mode, a message with the operator's :class:`~liaise.model.Approval` on
    ``ctx.approval`` passes, with a note saying who released it and when. The approval
    settles this filter alone; the filters after it judge the message as they would any
    other.
    """
    if ctx.subject.reply_mode_for(outbound.recipient) != DRAFT_REPLY_MODE:
        return Pass(outbound)
    approval = ctx.approval
    if not isinstance(approval, Approval):
        return Divert("draft reply mode")
    note = f"draft reply mode: released by {approval.by} at {approval.at.isoformat()}"
    return Pass(outbound, notes=(note,))


def leak_scan(outbound: Outbound, ctx: GateContext) -> Union[Pass, Divert]:
    """On a public channel, divert a message holding what must not be made public.

    That is an absolute local path (a home directory on macOS, Linux or Windows, written
    with single or JSON-doubled backslashes, a Windows home through a WSL mount, or a
    macOS temporary directory), a path ending in ``.env``, an email address, a private
    key's ``-----BEGIN ... PRIVATE KEY-----`` or ``-----BEGIN PGP PRIVATE KEY BLOCK-----``
    line, a token shape (``ghp_``,
    ``github_pat_``, ``sk-``, ``AKIA``, ``hf_``, ``xoxb-``), or one of
    ``policy.leak_terms`` as a whole word in any case. Tokens are also looked for with the
    text's line breaks removed, so a token wrapped across lines is found. The reason
    names each kind found and the notes say where, never what. It never redacts: a leak
    is for the operator to fix. A channel outside ``policy.public_channels`` passes
    unscanned.
    """

    def without_line_breaks(text: str) -> tuple[str, list[int]]:
        """``text`` without its line breaks, and where each character left was in ``text``."""
        kept = [index for index, char in enumerate(text) if char not in _LINE_BREAKS]
        return "".join(text[index] for index in kept), kept

    policy = ctx.subject.policy
    if outbound.channel not in policy.public_channels:
        return Pass(outbound)
    text = outbound.text
    unwrapped, positions = without_line_breaks(text)
    found = [
        (kind, match.start())
        for kind, pattern in _LEAK_PATTERNS
        for match in pattern.finditer(text)
    ]
    unwrapped_starts = (
        positions[match.start()]
        for pattern in _UNWRAPPED_TOKEN_PATTERNS
        for match in pattern.finditer(unwrapped)
    )
    found += [
        ("token", start)
        for start in unwrapped_starts
        if start == 0 or not _WORD_CHAR.fullmatch(text[start - 1])
    ]
    found += [
        ("leak term", match.start())
        for term in policy.leak_terms
        if term
        for match in re.finditer(
            rf"(?<!\w){re.escape(term)}(?!\w)", text, flags=re.IGNORECASE
        )
    ]
    hits = list(dict.fromkeys(found))  # a token on one line is found by both scans
    if not hits:
        return Pass(outbound)
    kinds = dict.fromkeys(kind for kind, _ in hits)
    return Divert(
        f"leak scan: {', '.join(kinds)}",
        notes=tuple(
            f"leak scan: {kind} at character {start}"
            for kind, start in sorted(hits, key=lambda hit: hit[1])
        ),
    )


def writing_card(outbound: Outbound, ctx: GateContext) -> Pass:
    """Note the recipient's acquaint writing card, for the ledger and the next run.

    Calls ``acquaint.brief(recipient, purpose=purpose)`` and notes its summary. It never
    diverts and never raises: without acquaint, or when acquaint fails (an unknown
    person raises ``AcquaintError``), the note says why the card is unavailable.
    """
    try:
        import acquaint

        card = acquaint.brief(outbound.recipient, purpose=outbound.purpose)
        summary = card.get("summary") or f"brief for {outbound.recipient}"
    except Exception as error:  # acquaint missing, or failing for this person
        note = f"writing card unavailable: {_acquaint_failure(error)}"
        return Pass(outbound, notes=(note,))
    return Pass(outbound, notes=(f"writing card: {summary}",))


def deslop(outbound: Outbound, ctx: GateContext) -> Union[Pass, Divert]:
    """Divert a message acquaint's style lint finds machine-sounding for its recipient.

    Calls ``acquaint.style_lint(text, recipient=recipient)``. When that is not ``ok``,
    the message is diverted, with each enforced finding as a note. Without acquaint, or
    when acquaint fails, the note says why and the message goes on. It never raises.
    """
    try:
        import acquaint

        lint = acquaint.style_lint(outbound.text, recipient=outbound.recipient)
        ok = bool(lint["ok"])
        findings = tuple(
            f"deslop: {finding['tier']} {finding['rule']}: {finding['message']}"
            + (f" (…{finding['excerpt']}…)" if finding.get("excerpt") else "")
            for finding in lint.get("findings", ())
            if finding.get("enforced")
        )
    except Exception as error:  # acquaint missing, or failing for this person
        note = f"deslop unavailable: {_acquaint_failure(error)}"
        return Pass(outbound, notes=(note,))
    if ok:
        return Pass(outbound)
    return Divert(f"deslop: {len(findings)} enforced finding(s)", notes=findings)


def notify_recipient(outbound: Outbound, ctx: GateContext) -> Union[Pass, Divert]:
    """On GitHub, make the message start with an ``@mention`` of its recipient.

    GitHub notifies only the people a comment mentions, and an issue an app files
    subscribes its partner to nothing. The login is the first valid one among the
    recipient's ``github:`` addresses, best first (see
    :meth:`~liaise.subjects.Subject.notify_addresses_for`). A missing mention is
    prefixed, the one rewrite the gate makes. A recipient with no GitHub address is
    diverted. Other channels pass unchanged.
    """
    if outbound.channel != MENTION_CHANNEL:
        return Pass(outbound)
    addresses = ctx.subject.notify_addresses_for(
        outbound.recipient, channels=MENTION_CHANNEL
    )
    logins = (address.partition(":")[2] for address in addresses)
    login = next((name for name in logins if _GITHUB_LOGIN_RE.fullmatch(name)), None)
    if login is None:
        return Divert(f"no handle to notify {outbound.recipient}")
    text = outbound.text.lstrip()
    if re.match(rf"@{re.escape(login)}(?![\w-])", text, flags=re.IGNORECASE):
        return Pass(outbound)
    mentioned = replace(outbound, text=f"@{login} {text}")
    return Pass(mentioned, notes=(f"added the mention @{login}",))


#: The gate's filters, in the order they run. The order is part of the design, not a
#: setting: a draft is diverted before anything else looks at it.
DFLT_OUTBOUND_FILTERS: tuple[OutboundFilter, ...] = (
    reply_mode,
    leak_scan,
    writing_card,
    deslop,
    notify_recipient,
)


def run_gate(
    outbound: Outbound,
    ctx: GateContext,
    *,
    outbound_filters: Iterable[OutboundFilter] = DFLT_OUTBOUND_FILTERS,
) -> GateDecision:
    """Run ``outbound`` through ``outbound_filters`` in order, stopping at the first divert.

    Each :class:`Pass` hands its message, possibly rewritten, to the next filter. The
    first :class:`Divert` ends the gate with nothing to send. Notes accumulate across
    the filters that ran. The gate fails closed: a filter that raises, or returns
    anything but a ``Pass`` or a ``Divert``, diverts the message with a reason naming it.
    """
    notes: list[str] = []
    for outbound_filter in outbound_filters:
        # By its type when it has no name: a repr can hold what a filter was bound to, such
        # as a local path, and the name reaches the operator's notification.
        name = getattr(outbound_filter, "__name__", type(outbound_filter).__name__)
        try:
            verdict = outbound_filter(outbound, ctx)
        except Exception as error:
            verdict = Divert(f"{name} failed: {type(error).__name__}: {error}")
        if isinstance(verdict, Pass):
            notes.extend(verdict.notes)
            outbound = verdict.outbound
            continue
        if not isinstance(verdict, Divert):
            kind = type(verdict).__name__
            verdict = Divert(f"{name} returned {kind}, not Pass or Divert")
        notes.extend(verdict.notes)
        return GateDecision(
            send=None, diverted=verdict.reason, notes=tuple(notes), diverted_by=name
        )
    return GateDecision(send=outbound, diverted=None, notes=tuple(notes))
