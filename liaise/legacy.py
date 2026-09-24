"""What liaise 0.1's gate would have decided: the counterfactual shadow mode measures against (liaise #39, #51).

Shadow mode does not loosen the gate. The policy of discussion 32 enforces on every
subject, ``mode = "shadow"`` included, and nothing sends that it holds back (the owner's
decision on liaise #51, recorded in ADR 0002). What shadow mode adds is a measurement:
beside each verdict the gate records what 0.1 would have decided about the same message,
so ``liaise gate report`` can count how often the two agree and which messages 0.1 would
have let out that the policy found severe (decision 11's *missed findings*).

0.1's gate ran, and stopped at the first divert: ``reply_mode`` (a message outside a case,
or in ``draft`` reply mode, waits for the operator, unless an approval is on the context),
``leak_scan`` (on a channel listed in ``policy.public_channels`` only: an absolute local
path, a ``.env`` path, an email address, a private key's first line, a token shape, or one
of ``policy.leak_terms`` as a whole word, in the text or the title), ``writing_card``,
``deslop`` and ``notify_recipient``. The last three are the gate's filters today, unchanged,
so :func:`legacy_decision` takes what they held back as given rather than running them
twice. Its answer is 0.1's own vocabulary: ``send``, or a draft for the operator (recorded
as the flow ``approve``, which is what a 0.1 divert was).

**Agreement is by flow class** (:func:`flow_class`): a message is either sent at once
(``send``) or held back (every other flow). 0.1 could say nothing finer, so a ``revise`` or
a ``refuse`` the policy chose agrees with a 0.1 divert. A ``delay`` counts as held even on a
subject whose outbox sends it unseen when its window passes, so on such a subject a 0.1
``send`` against a policy ``delay`` is a disagreement: agreement errs low, never high.

The counterfactual records the kinds 0.1 would have diverted on, never a value or a
position: it is kept in the ledger beside the verdict, and the report reads counts only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Optional

from liaise.detect import (
    EMAIL_PATTERN,
    ENV_FILE_PATTERN,
    LOCAL_PATH_PATTERNS,
    PRIVATE_KEY_PATTERN,
    TOKEN_SHAPES,
)
from liaise.policy import APPROVE, DRAFT_REPLY_MODE, SEND

#: The version of liaise whose gate :func:`legacy_decision` replays.
LEGACY_VERSION = "0.1"
#: The channels 0.1 scanned when a subject named none, kept here so the replay outlives
#: ``policy.public_channels``, which the policy no longer reads (ADR 0002).
LEGACY_PUBLIC_CHANNELS = ("github",)
#: The two flow classes agreement is measured by: out with no person looking, or held back.
SENT, HELD = "sent", "held"
#: What 0.1 diverted a message outside a case, or in draft reply mode, for.
OUTSIDE_A_CASE = "outside a case"
DRAFT_REPLY = "draft reply mode"
#: The prefix of a 0.1 leak-scan reason; the kinds follow it.
LEAK_SCAN = "leak scan"

#: What 0.1's leak scan diverted on, as (kind, pattern): the patterns :mod:`liaise.detect`
#: still owns, as 0.1 compiled them.
_LEAK_PATTERNS = (
    *(("local path", pattern) for pattern in LOCAL_PATH_PATTERNS),
    ("env file", ENV_FILE_PATTERN),
    ("email", re.compile(EMAIL_PATTERN)),
    ("private key", re.compile(PRIVATE_KEY_PATTERN)),
    *(("token", re.compile(rf"\b{shape}")) for shape in TOKEN_SHAPES),
)
#: The token shapes as 0.1 looked for them with the text's line breaks removed.
_UNWRAPPED_TOKEN_PATTERNS = tuple(re.compile(shape) for shape in TOKEN_SHAPES)
_LINE_BREAKS = frozenset("\r\n")
_WORD_CHAR = re.compile(r"\w")


@dataclass(frozen=True)
class Counterfactual:
    """What 0.1 would have decided: ``flow`` (``send`` or ``approve``) and why it held the message.

    ``reasons`` name what 0.1 diverted on — a filter, or the leak scan's kinds — never
    what the message said.
    """

    flow: str
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """JSON-ready, with the version it replays."""
        return {
            "version": LEGACY_VERSION,
            "flow": self.flow,
            "reasons": list(self.reasons),
        }


def flow_class(flow: Optional[str]) -> str:
    """:data:`SENT` for ``send``, :data:`HELD` for every other flow: what agreement compares.

    >>> flow_class("send"), flow_class("delay"), flow_class("approve")
    ('sent', 'held', 'held')
    """
    return SENT if flow == SEND else HELD


def leak_kinds(text: str, *, leak_terms: Iterable[str] = ()) -> tuple[str, ...]:
    """The kinds 0.1's leak scan found in ``text``, each once, in the order 0.1 listed them.

    >>> leak_kinds("mail pat" + "@example.com, see /" + "Users" + "/pat/notes")
    ('local path', 'email')
    >>> leak_kinds("ghp_" + "a" * 10 + "\\n" + "b" * 10)
    ('token',)
    >>> leak_kinds("the Orchid launch", leak_terms=["orchid"])
    ('leak term',)
    """
    kept = [index for index, char in enumerate(text) if char not in _LINE_BREAKS]
    unwrapped = "".join(text[index] for index in kept)
    kinds = [kind for kind, pattern in _LEAK_PATTERNS if pattern.search(text)]
    for pattern in _UNWRAPPED_TOKEN_PATTERNS:
        for match in pattern.finditer(unwrapped):
            start = kept[match.start()]
            if start == 0 or not _WORD_CHAR.fullmatch(text[start - 1]):
                kinds.append("token")
    if any(
        re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, flags=re.IGNORECASE)
        for term in leak_terms
        if term
    ):
        kinds.append("leak term")
    return tuple(dict.fromkeys(kinds))


def legacy_decision(
    outbound: Any,
    subject: Any,
    *,
    in_case: bool,
    approved: bool = False,
    shared_diverts: Iterable[str] = (),
) -> Counterfactual:
    """What 0.1's gate would have decided about ``outbound`` on ``subject``.

    ``in_case`` is whether the message belongs to a case; ``approved`` whether an approval
    of any kind was on the context (0.1 bound it to nothing, so any approval released a
    draft past its reply mode). ``shared_diverts`` names the filters the gate shares with
    0.1 (the writing card, deslop, the mention) that held this message back today.
    """
    reasons: list[str] = []
    if not approved:
        if not in_case:
            reasons.append(OUTSIDE_A_CASE)
        elif subject.reply_mode_for(outbound.recipient) == DRAFT_REPLY_MODE:
            reasons.append(DRAFT_REPLY)
    policy = subject.policy
    public = getattr(policy, "public_channels", LEGACY_PUBLIC_CHANNELS)
    if outbound.channel in public:
        parts = (outbound.text, getattr(outbound, "title", None) or "")
        kinds = dict.fromkeys(
            kind
            for part in parts
            for kind in leak_kinds(part, leak_terms=policy.leak_terms)
        )
        if kinds:
            reasons.append(f"{LEAK_SCAN}: {', '.join(kinds)}")
    reasons.extend(dict.fromkeys(shared_diverts))
    return Counterfactual(flow=APPROVE if reasons else SEND, reasons=tuple(reasons))
