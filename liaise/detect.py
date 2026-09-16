"""Detectors for outbound messages: what a message holds, reported without the value.

:func:`detect` runs :data:`DFLT_DETECTORS` over a message and returns :class:`Finding`
records. A finding says what was found (``kind`` and ``rule``), where (``start`` and
``end``, character offsets into the message as written), what it concerns (``entity``,
``label``, ``sealed_from``), how severe it is, and a keyed ``fingerprint`` that lets the
ledger correlate a repeat. It never holds the matched text. Detectors only find; what a
finding means for a send is the policy's decision (liaise discussion 32, §5.4).

The six kinds (discussion §5.2):

- ``secret``: the 0.1 leak scan's token shapes and private-key header, unchanged, and a
  curated set of distinctive-prefix rules (:data:`SECRET_RULES`). A token split by line
  breaks, invisible characters, emphasis marks or HTML markup is found too. There is no
  generic-entropy rule: its precision is too low to divert on (research §5.2).
- ``canary``: a ``canary_terms`` entry anywhere after normalisation, even inside a word or
  percent-encoded.
- ``vocabulary``: a term of ``disclosure["vocabulary"]`` whose entity is not a person, as
  a whole word after normalisation.
- ``third_party``: the same for a person's term (entity ``person:<id>``), unless that person
  is a reader, one of ``disclosure["people"]``. A reader is never a third party.
- ``exfiltration``: link and image destinations whose host is not in ``allowlist`` (a host
  or any of its subdomains), read as a browser reads them (Markdown inline and reference
  links and images, HTML attributes, autolinks and bare URLs); base64 runs, wrapped or not,
  and hex runs; invisible characters, except where emoji, the three subdivision flags,
  joining scripts or right-to-left text need them; private, loopback, shared and
  link-local addresses; local paths and ``.env`` files (the 0.1 path patterns).
- ``personal``: a ``personal_terms`` entry as a whole word after normalisation, and any
  email address (the 0.1 pattern).

**Two readings.** Terms and secrets are looked for in the message as written and, when it
holds markup, as a Markdown or HTML reader sees it (:func:`render`): tags, comments and
backslash escapes removed (a block tag reading as a space), character references and
percent-escapes decoded. A finding in either reading counts, so a plain-text reader and a
rendering one are both covered; the price is that markup a renderer hides can still read
as a word break to the plain-text reading.

**Normalisation** (:func:`normalise`, research §5.5) folds each character by
compatibility decomposition (NFKD, so full-width and other compatibility forms fold as
NFKC folds them), maps confusable letters to the ASCII letter they imitate (Unicode's
confusables data, plus the small capitals and Cyrillic and Greek shapes it does not map),
case-folds, drops combining marks, and removes invisible characters, separators
(whitespace, dashes and minus signs, underscores, dots) and the Markdown marks ``*``,
``~``, backtick and backslash. An offset map sends every normalised character back to the
characters of the message it came from, so a finding covers the text as written.

**Whole words.** A term matches when it neither continues a word on either side (judged in
the reading, past invisible characters and marks) nor spans a word break the term does
not have: ``He-ron``, a hyphenated line wrap and ``H e r o n`` are "Heron"; ``on a`` and
``He ron`` are not. Canary terms match anywhere.

**Fingerprints** are HMAC-SHA256, keyed by :func:`fingerprint_key` (32 random bytes in
``<state_dir>/fingerprint.key``, created on first use, owner-only), over the value as a
reader sees it: normalised for the kinds that match terms and addresses (``canary``,
``vocabulary``, ``third_party``, ``personal``), so every disguise of a term correlates;
exact for ``secret`` and ``exfiltration``, which are case-sensitive, with only line
breaks, invisible characters, emphasis marks and markup removed. The value itself is used
when that leaves nothing. The key is read only when there is a finding.

**Severity** (discussion §5.2): ``secret`` and ``canary`` 5; ``exfiltration`` 4; a term
sealed from one of the readers 4; a term labelled above the readers' least clearance
(``disclosure["least_clearance"]``) 3; anything else by audience, 2 when the least
clearance is ``clear`` (or unknown) and 1 otherwise.

**The seam.** ``detectors=`` takes any callables ``(Scan) -> Iterable[Finding]``; a
:class:`Scan` holds the inputs and builds findings. Its pointers are Presidio as an
optional extra and a semantic pass that may only add findings (discussion §8). This
module imports neither acquaint nor correspond: ``disclosure`` is the JSON of
``acquaint.disclosure`` (discussion §4.5).

**Sources.** The secret rules after the six 0.1 shapes and the private-key header adapt
the regular expressions of the rules with the same or similar ids in gitleaks' default
configuration (``config/gitleaks.toml``, MIT licence, copyright (c) 2019 Zachary Rice;
the notice is reproduced beside :data:`SECRET_RULES`): capturing and trailing-terminator
groups are dropped, since the scan adds its own word boundary, unescaped dots are
escaped and unbounded repetitions are bounded. ``data/confusables.json`` is a selection
of Unicode's confusables data (UTS #39, Unicode License v3; the notice travels in the
file), regenerated by ``misc/scripts/make_confusables.py``. No rule or table comes from a
share-alike source.
"""

from __future__ import annotations

import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import tempfile
import time
import unicodedata
from array import array
from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from hashlib import sha256
from importlib import resources
from itertools import chain as chain_iterables
from itertools import compress, repeat
from pathlib import Path
from typing import Optional, Union
from urllib.parse import unquote

#: The kinds a finding can have, in the order :data:`DFLT_DETECTORS` looks for them.
KINDS = ("secret", "canary", "vocabulary", "exfiltration", "personal", "third_party")
#: The kinds whose fingerprint is taken over the normalised value.
FOLDED_KINDS = frozenset({"canary", "vocabulary", "third_party", "personal"})
#: Traffic-light labels, least restrictive first (discussion decision 3).
LABELS = ("clear", "green", "amber", "red")
#: The label of a vocabulary entry that has none: amber for a project or an organisation,
#: green for a person (discussion §4.2).
DFLT_ENTITY_LABEL = "amber"
DFLT_PERSON_LABEL = "green"
#: How a vocabulary entry's entity names a person.
PERSON_PREFIX = "person:"

SEVERITY_SECRET = 5
SEVERITY_CANARY = 5
SEVERITY_EXFILTRATION = 4
SEVERITY_SEALED = 4
SEVERITY_ABOVE_CLEARANCE = 3
#: The severity of any other finding, when the least-cleared reader has clearance
#: ``clear`` (a public or unknown audience), and when not.
SEVERITY_WIDE_AUDIENCE = 2
SEVERITY_NARROW_AUDIENCE = 1

#: Where the fingerprint key lives when no liaise config names a state directory.
DFLT_STATE_DIR = Path("~/.local/share/liaise")
DFLT_KEY_FILE = "fingerprint.key"
DFLT_KEY_BYTES = 32
KEY_FILE_MODE = 0o600
STATE_DIR_MODE = 0o700
#: How often, and how far apart, a key file is read again while it is busy (on Windows a
#: reader can meet a sharing violation while another process moves its new key into
#: place) or shorter than a key (another process is still writing it).
KEY_READ_ATTEMPTS = 20
KEY_READ_RETRY_S = 0.05

#: The shortest base64 run that is a finding: 75 bytes of data.
MIN_BASE64_RUN = 100
#: The narrowest width of a wrapped base64 block (PEM wraps at 64, MIME at 76), and the
#: shortest unpadded last line that reads as the end of the data.
MIN_WRAPPED_BASE64_LINE = 40
MIN_BASE64_TAIL = 8
#: The shortest hex run that is a finding: longer than a SHA-512 digest, so digests and
#: commit hashes quoted in a message are not findings.
MIN_HEX_RUN = 129
#: How much of a link destination is read for its host.
MAX_DESTINATION = 2048

#: The package data file of confusable characters.
CONFUSABLES_RESOURCE = "confusables.json"

# ---- the 0.1 leak-scan patterns (liaise.gate reads them from here) ----

#: The token shapes of the 0.1 leak scan, each without the word boundary it starts at: the
#: GitHub (``ghp_`` and its siblings, ``github_pat_``), ``sk-`` API key, AWS access key,
#: Hugging Face (``hf_``) and Slack (``xoxb-`` and its siblings) shapes.
TOKEN_SHAPES = (
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"github_pat_\w{20,}",
    r"sk-[\w-]{20,}",
    r"AKIA[0-9A-Z]{16}\b",
    r"hf_[A-Za-z0-9]{30,}",
    r"xox[baprs]-[A-Za-z0-9-]{10,}",
)
#: The rule id of each of :data:`TOKEN_SHAPES`, in order.
TOKEN_RULES = (
    "github-token",
    "github-fine-grained-token",
    "sk-api-key",
    "aws-access-key",
    "hugging-face-token",
    "slack-token",
)
#: The literals every match of each of :data:`TOKEN_SHAPES` contains, in order.
TOKEN_LITERALS = (
    ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"),
    ("github_pat_",),
    ("sk-",),
    ("AKIA",),
    ("hf_",),
    ("xoxb-", "xoxa-", "xoxp-", "xoxr-", "xoxs-"),
)
#: The most characters an email address's local part holds (RFC 5321), a DNS label
#: holds, and labels a domain name holds.
_EMAIL_LOCAL_MAX = 64
_DNS_LABEL_MAX = 63
_DNS_LABELS_MAX = 127
#: How many words may name a private key's type (``ENCRYPTED``, ``OPENSSH``), and how long
#: each may be.
_KEY_TYPE_WORDS_MAX = 4
_KEY_TYPE_WORD_MAX = 16
_EMAIL_LOCAL_CHAR = r"[\w.%+-]"
_DNS_LABEL = rf"[A-Za-z0-9-]{{1,{_DNS_LABEL_MAX}}}"
#: An email address. Its local part is anchored where it starts and bounded, so a long run
#: of word characters is tried once, not once per character, which made the scan quadratic
#: in its length. A local part longer than the bound is still found, from its last
#: character. Each domain label and the label count are bounded too.
EMAIL_PATTERN = (
    rf"(?:(?<!{_EMAIL_LOCAL_CHAR}){_EMAIL_LOCAL_CHAR}{{1,{_EMAIL_LOCAL_MAX}}}"
    rf"|{_EMAIL_LOCAL_CHAR})"
    rf"@{_DNS_LABEL}(?:\.{_DNS_LABEL}){{0,{_DNS_LABELS_MAX}}}"
    rf"\.[A-Za-z]{{2,{_DNS_LABEL_MAX}}}"
)
#: A private key's first line: PEM's ``-----BEGIN ... PRIVATE KEY-----``, its type's words
#: bounded, or PGP's ``-----BEGIN PGP PRIVATE KEY BLOCK-----``.
PRIVATE_KEY_PATTERN = (
    r"-----BEGIN "
    rf"(?:(?:[A-Z0-9]{{1,{_KEY_TYPE_WORD_MAX}}} ){{0,{_KEY_TYPE_WORDS_MAX}}}PRIVATE KEY"
    r"|PGP PRIVATE KEY BLOCK)-----"
)
#: Absolute local paths: home directories on macOS, Linux and Windows (its backslashes
#: single, or doubled as JSON writes them), a Windows home through a WSL mount, and
#: macOS's temporary directories.
LOCAL_PATH_PATTERNS = (
    re.compile(r"(?<![\w.~-])/(?:Users|home|root)/"),
    re.compile(r"\b[A-Za-z]:(?:\\{1,2}|/)Users(?:\\{1,2}|/)", re.IGNORECASE),
    re.compile(r"(?<![\w.~-])/mnt/[A-Za-z]/Users/", re.IGNORECASE),
    re.compile(r"(?<![\w.~-])/(?:private/var|var/folders)/"),
)
#: A path ending in ``.env``.
ENV_FILE_PATTERN = re.compile(r"(?<=[\\/])\.env(?![\w-]|\.\w)")


# ---- findings ----


@dataclass(frozen=True, kw_only=True)
class Finding:
    """Something a detector found in a message: never the matched text.

    ``start`` and ``end`` are offsets into the message as written. ``entity``, ``label``
    and ``sealed_from`` are set for terms from the disclosure. ``rule`` names the pattern
    or check that matched; ``fingerprint`` is the keyed HMAC of the value. ``part`` names
    the part of the message the offsets are into when it is not the text (``title``,
    ``attachment name``); :func:`detect` scans one part and leaves it None.
    """

    kind: str
    start: int
    end: int
    entity: Optional[str] = None
    label: Optional[str] = None
    sealed_from: tuple[str, ...] = ()
    rule: str
    severity: int
    fingerprint: str
    part: Optional[str] = None

    def to_dict(self) -> dict:
        """The finding as a JSON-ready dict; ``part`` only when it names one."""
        return {
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "entity": self.entity,
            "label": self.label,
            "sealed_from": list(self.sealed_from),
            "rule": self.rule,
            "severity": self.severity,
            "fingerprint": self.fingerprint,
            **({"part": self.part} if self.part else {}),
        }


class FingerprintKeyError(Exception):
    """The fingerprint key file exists but cannot be used."""


# ---- characters ----

#: Invisible format characters (every character of general category ``Cf``) and the other
#: default-ignorable characters that render as nothing: the combining grapheme joiner,
#: Hangul fillers, Khmer inherent vowels, Mongolian variation selectors and the variation
#: selectors. ``test_detect`` checks that every ``Cf`` character is here.
_INVISIBLE_RANGES = (
    ("\u00ad", "\u00ad"),
    ("\u034f", "\u034f"),
    ("\u0600", "\u0605"),
    ("\u061c", "\u061c"),
    ("\u06dd", "\u06dd"),
    ("\u070f", "\u070f"),
    ("\u0890", "\u0891"),
    ("\u08e2", "\u08e2"),
    ("\u115f", "\u1160"),
    ("\u17b4", "\u17b5"),
    ("\u180b", "\u180f"),
    ("\u200b", "\u200f"),
    ("\u202a", "\u202e"),
    ("\u2060", "\u206f"),
    ("\u3164", "\u3164"),
    ("\ufe00", "\ufe0f"),
    ("\ufeff", "\ufeff"),
    ("\uffa0", "\uffa0"),
    ("\ufff9", "\ufffb"),
    ("\U000110bd", "\U000110bd"),
    ("\U000110cd", "\U000110cd"),
    ("\U00013430", "\U0001343f"),
    ("\U0001bca0", "\U0001bca3"),
    ("\U0001d173", "\U0001d17a"),
    ("\U000e0001", "\U000e0001"),
    ("\U000e0020", "\U000e007f"),
    ("\U000e0100", "\U000e01ef"),
)
_INVISIBLE_CLASS = "".join(
    first if first == last else f"{first}-{last}" for first, last in _INVISIBLE_RANGES
)
_INVISIBLE_CHAR = re.compile(f"[{_INVISIBLE_CLASS}]")
_INVISIBLE_RUN = re.compile(f"[{_INVISIBLE_CLASS}]+")
#: What :func:`visible` spells out: every invisible character, and every control
#: character but the tab and the line breaks (a terminal obeys an escape sequence, so a
#: message holding one could redraw what the operator reads).
_SHOWN_AS_CODE = re.compile(
    f"[{_INVISIBLE_CLASS}\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f-\\x9f]"
)


def visible(text: str) -> str:
    """``text`` with each invisible or control character written as ``<U+XXXX>``.

    What the operator reads before releasing a message: a zero-width space, a direction
    override or a terminal escape shows as what it is, where it is.

    >>> visible("He" + chr(0x200B) + "ron")
    'He<U+200B>ron'
    >>> visible("two\\nlines\\tand a tab")
    'two\\nlines\\tand a tab'
    """
    return _SHOWN_AS_CODE.sub(lambda match: f"<U+{ord(match.group()):04X}>", text)


#: Combining marks, dropped by normalisation. Spacing marks (``Mc``) carry a syllable's
#: sound in many scripts and are kept.
_MARK_CATEGORIES = frozenset({"Mn", "Me"})
#: Separators besides whitespace, dash punctuation and connector punctuation: dots,
#: minus signs and hyphen-like symbols, and the Markdown marks that disappear when rendered.
_SEPARATOR_CATEGORIES = frozenset({"Pd", "Pc"})
_OTHER_SEPARATORS = frozenset(
    ".\u00b7\u2024\u2027\u30fb\uff0e\uff65"  # dots
    "\u2212\u2043\u02d7\u2796"  # minus signs and hyphen-like symbols
    "*~`\\"  # Markdown emphasis, strikethrough, code and escape marks
)
#: Letters Unicode's confusables data does not map to ASCII that still read as an ASCII
#: letter: small capitals, and Cyrillic and Greek small letters shaped like one.
_LOOKALIKES = {
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ɢ": "g", "ʜ": "h",
    "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p",
    "ʀ": "r", "ꜱ": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
    "в": "b", "к": "k", "м": "m", "н": "h", "т": "t", "η": "n", "ε": "e",
}  # fmt: skip


#: How many characters' folds and classes are cached: most assigned characters, while
#: bounding the memory a message of distinct code points can take.
_CHARACTER_CACHE_SIZE = 1 << 18


@lru_cache(maxsize=None)
def _prototypes() -> dict[str, str]:
    """``{character: the ASCII letter or digit it imitates}``, from the package data."""
    data = resources.files("liaise.data").joinpath(CONFUSABLES_RESOURCE)
    record = json.loads(data.read_text(encoding="utf-8"))
    table = {
        source: prototype
        for prototype, sources in record["prototypes"].items()
        for source in sources
    }
    for char, prototype in _LOOKALIKES.items():
        table.setdefault(char, prototype)
    return table


@lru_cache(maxsize=_CHARACTER_CACHE_SIZE)
def _is_invisible(char: str) -> bool:
    return _INVISIBLE_CHAR.match(char) is not None


@lru_cache(maxsize=_CHARACTER_CACHE_SIZE)
def _is_transparent(char: str) -> bool:
    """Whether whole-word matching looks past ``char``: an invisible character or a mark."""
    return _is_invisible(char) or unicodedata.category(char) in _MARK_CATEGORIES


def _vanishes(char: str) -> bool:
    return (
        _is_transparent(char)
        or char.isspace()
        or char in _OTHER_SEPARATORS
        or unicodedata.category(char) in _SEPARATOR_CATEGORIES
    )


@lru_cache(maxsize=_CHARACTER_CACHE_SIZE)
def _fold(char: str) -> str:
    """What one character contributes to the normalised text: nothing, or its folded form.

    Confusables are mapped before case folding, since a capital and its small letter can
    imitate different letters (Greek capital eta is H, small eta is n), and again after.
    """
    prototypes = _prototypes()
    decomposed = unicodedata.normalize("NFKD", char)
    mapped = "".join(prototypes.get(c, c) for c in decomposed)
    folded = unicodedata.normalize("NFKD", mapped.casefold())
    return "".join(prototypes.get(c, c) for c in folded if not _vanishes(c))


# ---- views ----


@dataclass(frozen=True)
class View:
    """``text`` derived from ``source``, and where each of its characters came from.

    ``text[i]`` came from ``source[start_of(i):end_of(i)]``. ``origins`` is None when
    ``text`` is ``source``; ``ends`` is None when each character came from one character.
    """

    source: str
    text: str
    origins: Optional[array] = None
    ends: Optional[array] = None

    def start_of(self, index: int) -> int:
        """Where the source of ``text[index]`` starts."""
        return index if self.origins is None else self.origins[index]

    def end_of(self, index: int) -> int:
        """Where the source of ``text[index]`` ends."""
        return self.ends[index] if self.ends is not None else self.start_of(index) + 1

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        """The span of ``source`` that ``text[start:end]`` came from."""
        return self.start_of(start), self.end_of(end - 1)

    def derive(self, text: str, origins: array, ends: Optional[array] = None) -> "View":
        """A view of ``text``, whose characters came from this view's text at ``origins``
        (to ``ends``, or one character each), mapped back to this view's source."""
        if self.origins is None and self.ends is None:
            return View(self.source, text, origins, ends)
        lasts = origins if ends is None else array("q", map((-1).__add__, ends))
        if self.origins is None:
            starts = origins
        else:
            starts = array("q", map(self.origins.__getitem__, origins))
        if self.ends is not None:
            stops = array("q", map(self.ends.__getitem__, lasts))
        elif self.origins is not None:
            stops = array("q", map((1).__add__, map(self.origins.__getitem__, lasts)))
        else:
            stops = array("q", map((1).__add__, lasts))
        return View(self.source, text, starts, stops)


def _view_of(text: str) -> View:
    return View(text, text)


def _without(view: View, pattern: re.Pattern) -> View:
    """``view`` with every match of ``pattern`` removed from its text."""
    text, pieces, origins, position = view.text, [], array("q"), 0
    for match in pattern.finditer(text):
        pieces.append(text[position : match.start()])
        origins.extend(range(position, match.start()))
        position = match.end()
    pieces.append(text[position:])
    origins.extend(range(position, len(text)))
    return view.derive("".join(pieces), origins)


#: Where rendering Markdown or HTML may change the text: a comment, a tag, a processing
#: instruction or declaration, a character reference, a run of percent-escapes, or a
#: backslash escape.
_MARKUP_START = re.compile(
    r"<(?:!--|[!?/A-Za-z])"
    r"|&(?:#[0-9]{1,32}|#[xX][0-9A-Fa-f]{1,32}|[A-Za-z][A-Za-z0-9]{1,31});"
    r"|(?:%[0-9A-Fa-f]{2}){1,64}"
    r"|\\(?=[!-/:-@\[-`{-~])"
)
_MARKUP_CHARS = "<&%\\"
_TAG_NAME = re.compile(r"</?([A-Za-z][A-Za-z0-9-]*)")
#: Elements a renderer shows as a break between words: a tag of one reads as a space.
_BREAKING_TAGS = frozenset(
    "address article aside blockquote br dd details div dl dt figcaption figure footer "
    "h1 h2 h3 h4 h5 h6 header hr img li main nav ol p pre section summary table tbody "
    "td tfoot th thead tr ul".split()
)


def _decoded(token: str) -> str:
    if token.startswith("&"):
        return html.unescape(token)
    try:
        return unquote(token, errors="strict")
    except UnicodeDecodeError:
        return token


def render(text: str) -> Optional[View]:
    """``text`` as a Markdown or HTML reader sees it, or None when rendering changes nothing.

    Comments (to ``-->``, or to the end of the text when unclosed), tags, processing
    instructions, declarations and backslash escapes are removed, a tag of a block
    element or a line break reading as a space; character references and percent-escapes
    are decoded. Each piece of markup is read once, so rendering is linear in the text.

    >>> render("He<b></b>r&#111;n%21").text
    'Heron!'
    >>> render("on<br>a").text
    'on a'
    """
    if not any(char in text for char in _MARKUP_CHARS):
        return None
    pieces, origins, ends = [], array("q"), array("q")
    position, unclosed_tag_at = 0, len(text)

    def replace(start: int, stop: int, replacement: str) -> None:
        pieces.append(text[position:start])
        origins.extend(range(position, start))
        ends.extend(range(position + 1, start + 1))
        pieces.append(replacement)
        origins.extend(repeat(start, len(replacement)))
        ends.extend(repeat(stop, len(replacement)))

    for match in _MARKUP_START.finditer(text):
        start, token = match.start(), match.group()
        if start < position:
            continue  # inside markup already removed
        if token == "<!--":
            close = text.find("-->", match.end())
            stop, replacement = (len(text) if close < 0 else close + 3), ""
        elif token.startswith("<"):
            close = -1 if start >= unclosed_tag_at else text.find(">", match.end())
            if close < 0:  # no ">" from here on: no later "<" closes either
                unclosed_tag_at = min(unclosed_tag_at, start)
                continue
            name = _TAG_NAME.match(text, start)
            breaking = name is not None and name.group(1).lower() in _BREAKING_TAGS
            stop, replacement = close + 1, " " if breaking else ""
        elif token.startswith("\\"):
            stop, replacement = match.end(), ""
        else:
            stop, replacement = match.end(), _decoded(token)
            if replacement == token:
                continue
        replace(start, stop, replacement)
        position = stop
    if not position:
        return None
    pieces.append(text[position:])
    origins.extend(range(position, len(text)))
    ends.extend(range(position + 1, len(text) + 1))
    return View(text, "".join(pieces), origins, ends)


# ---- normalisation ----


def _alnum_beside(source: str, index: int, step: int) -> bool:
    """Whether the first character from ``index`` in direction ``step``, past invisible
    characters and marks, is a letter or digit."""
    while 0 <= index < len(source) and _is_transparent(source[index]):
        index += step
    return 0 <= index < len(source) and source[index].isalnum()


#: A line broken after a hyphen, which joins a word rather than separating two.
_HYPHENATED_WRAP = re.compile(r"[-‐­][ \t]*\r?\n[ \t]*")


def _spaced(gap: str) -> bool:
    """Whether a gap between two characters of a match reads as a space: whitespace other
    than a hyphenated line wrap."""
    return any(map(str.isspace, _HYPHENATED_WRAP.sub("", gap)))


@dataclass(frozen=True)
class FoldedTerm:
    """A term as normalisation folds it, and where its own words break.

    ``breaks`` holds each index ``k`` of ``text`` such that the term had a separator
    between ``text[k - 1]`` and ``text[k]``. ``lead`` and ``trail`` are the characters the
    term starts and ends with that folding drops (the ``~`` of ``~/notes``); a match
    covers them too when the message has them.
    """

    text: str
    breaks: frozenset[int]
    lead: str = ""
    trail: str = ""


@dataclass(frozen=True)
class Normalised(View):
    """A reading of a message folded for matching terms.

    ``reading`` is the text that was folded (the message, or its rendering) and
    ``reading_origins[i]`` the index in it of the character ``text[i]`` came from. Word
    boundaries and spacing are judged in the reading, as its reader sees them.
    """

    reading: str = ""
    reading_origins: Optional[array] = None

    def spans(
        self, term: FoldedTerm, *, whole_word: bool = True
    ) -> Iterator[tuple[int, int]]:
        """Where ``term`` occurs, as ``(start, end)`` in ``source``.

        With ``whole_word``, a match must not continue a word of the message on either side,
        and must not span a word break the term does not have, unless every letter of it is
        spaced apart. Occurrences of the same term do not overlap.
        """
        if not term.text:
            return
        index = self.text.find(term.text)
        while index >= 0:
            end = index + len(term.text)
            if not whole_word or self._is_word(term, index, end):
                yield self._covering_span(term, index, end)
                index = self.text.find(term.text, end)
            else:
                index = self.text.find(term.text, index + 1)

    def _is_word(self, term: FoldedTerm, index: int, end: int) -> bool:
        return (
            self._starts_word(index)
            and self._ends_word(end)
            and self._joined_as(term, index, end)
        )

    def _starts_word(self, index: int) -> bool:
        if not self.text[index].isalnum():
            return True
        at = self.reading_origins[index]
        if index and self.reading_origins[index - 1] == at:
            return not self.text[index - 1].isalnum()  # inside one folded character
        return not _alnum_beside(self.reading, at - 1, -1)

    def _ends_word(self, end: int) -> bool:
        if not self.text[end - 1].isalnum():
            return True
        at = self.reading_origins[end - 1]
        if end < len(self.text) and self.reading_origins[end] == at:
            return not self.text[end].isalnum()
        return not _alnum_beside(self.reading, at + 1, 1)

    def _joined_as(self, term: FoldedTerm, index: int, end: int) -> bool:
        """Whether the characters of a match are joined as the term's are: space between
        them only where the term breaks, or between every letter."""
        spaced = joined = False
        for offset in range(1, end - index):
            if offset in term.breaks:
                continue
            left = self.reading_origins[index + offset - 1]
            right = self.reading_origins[index + offset]
            if left == right:
                continue  # one character folded to several
            if _spaced(self.reading[left + 1 : right]):
                spaced = True
            else:
                joined = True
            if spaced and joined:
                return False
        return True

    def _covering_span(self, term: FoldedTerm, index: int, end: int) -> tuple[int, int]:
        """The span of a match, with its trailing marks and the term's dropped lead and
        trail where the message has them."""
        source = self.source
        start, stop = self.source_span(index, end)
        while (
            stop < len(source)
            and unicodedata.category(source[stop]) in _MARK_CATEGORIES
        ):
            stop += 1
        lead, trail = term.lead.casefold(), term.trail.casefold()
        if lead and source[max(0, start - len(lead)) : start].casefold() == lead:
            start -= len(lead)
        if trail and source[stop : stop + len(trail)].casefold() == trail:
            stop += len(trail)
        return start, stop


#: Folding an ASCII character lowers its case or drops it: which of the 128 are kept, and
#: a translation table that drops the others.
_ASCII_KEPT = bytes(0 if _vanishes(chr(code)) else 1 for code in range(128))
_ASCII_VANISHING = dict.fromkeys(code for code in range(128) if _vanishes(chr(code)))


def _fold_characters(text: str) -> tuple[str, array]:
    """``text`` folded one (cached) character at a time, and each folded character's
    origin."""
    pieces = list(map(_fold, text))
    lengths = map(len, pieces)
    origins = chain_iterables.from_iterable(map(repeat, range(len(text)), lengths))
    return "".join(pieces), array("q", origins)


def _fold_ascii(text: str) -> tuple[str, array]:
    """What :func:`_fold_characters` returns for ASCII ``text``, by string operations that
    run in C."""
    kept = map(_ASCII_KEPT.__getitem__, text.encode("ascii"))
    origins = array("q", compress(range(len(text)), kept))
    return text.lower().translate(_ASCII_VANISHING), origins


def _folded(view: View) -> Normalised:
    fold = _fold_ascii if view.text.isascii() else _fold_characters
    folded, origins = fold(view.text)
    derived = view.derive(folded, origins)
    return Normalised(
        derived.source, derived.text, derived.origins, derived.ends, view.text, origins
    )


def normalise(text: str) -> Normalised:
    """Fold ``text`` for matching, keeping where each folded character came from.

    >>> folded = normalise("Ｈｅ\\u200b-Ron!")
    >>> folded.text
    'heron!'
    >>> list(folded.spans(fold_term("Heron")))
    [(0, 7)]
    """
    return _folded(_view_of(text))


def fold_term(term: str) -> FoldedTerm:
    """``term`` folded as :func:`normalise` folds a message, with its word breaks."""
    folded = normalise(term)
    if not folded.text:
        return FoldedTerm("", frozenset())
    breaks = frozenset(
        offset
        for offset in range(1, len(folded.text))
        if any(
            not _is_transparent(char)
            for char in term[folded.end_of(offset - 1) : folded.start_of(offset)]
        )
    )
    lead = term[: folded.start_of(0)]
    trail = term[folded.end_of(len(folded.text) - 1) :]
    return FoldedTerm(folded.text, breaks, lead, trail)


def _folded_value(value: str) -> str:
    rendered = render(value)
    return "".join(map(_fold, rendered.text if rendered else value))


#: What does not change a secret as a reader sees it: line breaks, invisible characters,
#: and the Markdown emphasis, strikethrough and code marks.
_STRIPPABLE = re.compile(f"[\\r\\n*~`{_INVISIBLE_CLASS}]+")


def _stripped_value(value: str) -> str:
    rendered = render(value)
    return _STRIPPABLE.sub("", rendered.text if rendered else value)


# ---- fingerprint key ----


def _configured_state_dir() -> Path:
    """The state directory of the liaise config, or :data:`DFLT_STATE_DIR` without one."""
    from liaise.config import ConfigError, load_global_config

    try:
        return Path(load_global_config().state_dir).expanduser()
    except ConfigError:
        return DFLT_STATE_DIR.expanduser()


def _create_key_file(path: Path, key_bytes: int) -> Optional[bytes]:
    """Write a new key to ``path`` atomically: None when another process wrote it first.

    The key is written in full to an owner-only temporary file, which is then linked (or,
    on Windows, renamed) into place; neither replaces an existing file. On Windows,
    placing it while another process places its own can be refused as busy rather than
    as existing; that also means another process wrote it first.
    """
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=".fingerprint-", suffix=".tmp"
    )
    try:
        key = secrets.token_bytes(key_bytes)
        with os.fdopen(descriptor, "wb") as file:
            file.write(key)
            file.flush()
            os.fsync(file.fileno())
        place = os.link if os.name == "posix" else os.rename
        try:
            place(temporary, path)
        except FileExistsError:
            return None
        except OSError:
            if path.exists():
                return None  # another process placed its key meanwhile
            return _write_key_exclusively(path, key)  # hard links refused (EPERM, ...)
        return key
    finally:
        with suppress(FileNotFoundError, PermissionError):
            os.unlink(temporary)


def _write_key_exclusively(path: Path, key: bytes) -> Optional[bytes]:
    """Write ``key`` to ``path`` unless a file is there (None then). Not atomic: a reader
    can meet the file before it is full, which :func:`_read_key` waits out."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, KEY_FILE_MODE)
    except FileExistsError:
        return None
    with os.fdopen(descriptor, "wb") as file:
        file.write(key)
        file.flush()
        os.fsync(file.fileno())
    return key


def _read_key(path: Path, key_bytes: int) -> bytes:
    """The key in ``path``, read again up to :data:`KEY_READ_ATTEMPTS` times while the file
    is busy (a Windows sharing violation reports as a permission error) or shorter than
    ``key_bytes`` (another process may still be writing it)."""
    for attempt in range(1, KEY_READ_ATTEMPTS + 1):
        last = attempt == KEY_READ_ATTEMPTS
        try:
            key = path.read_bytes()
        except PermissionError as error:
            if last:
                raise FingerprintKeyError(
                    f"the fingerprint key in {path} cannot be read ({error}); liaise "
                    "creates it readable by its owner, so check the file's owner and "
                    "mode, or what else holds it open"
                ) from error
        else:
            if len(key) >= key_bytes:
                return key
            if last:
                raise FingerprintKeyError(
                    f"the fingerprint key in {path} holds {len(key)} bytes, fewer than "
                    f"{key_bytes}: it was not written by liaise. Replacing it changes "
                    "every fingerprint, so repeats recorded before will no longer "
                    "correlate."
                )
        time.sleep(KEY_READ_RETRY_S)
    raise AssertionError("unreachable: the last attempt returns or raises")


def fingerprint_key(
    state_dir: Union[str, os.PathLike, None] = None,
    *,
    key_file: str = DFLT_KEY_FILE,
    key_bytes: int = DFLT_KEY_BYTES,
    create: bool = True,
) -> bytes:
    """The fingerprint key in ``<state_dir>/<key_file>``, created on first use.

    ``state_dir`` defaults to the liaise config's, else :data:`DFLT_STATE_DIR`. A new key
    is ``key_bytes`` random bytes in a file only its owner can read, put in place
    atomically, so processes racing to create it all read the same key. A directory
    created here is owner-only. An existing file shorter than ``key_bytes`` raises
    :class:`FingerprintKeyError`.

    With ``create`` false (a dry run, which writes nothing), a missing key is not created:
    the answer is a key used once, so its fingerprints correlate with nothing recorded.
    """
    directory = (
        Path(state_dir).expanduser()
        if state_dir is not None
        else _configured_state_dir()
    )
    if not create and not (directory / key_file).exists():
        return secrets.token_bytes(key_bytes)
    directory.mkdir(parents=True, exist_ok=True, mode=STATE_DIR_MODE)
    path = directory / key_file
    if not path.exists():
        created = _create_key_file(path, key_bytes)
        if created is not None:
            return created
    return _read_key(path, key_bytes)


# ---- the scan ----


def _label_rank(label: Optional[str], *, unknown: int) -> int:
    return LABELS.index(label) if label in LABELS else unknown


def _ids(value) -> tuple[str, ...]:
    if not value:
        return ()
    return (value,) if isinstance(value, str) else tuple(value)


def _allowlist_entry(host: str) -> str:
    return host.strip().lower().removeprefix("*.").strip(".")


@dataclass(frozen=True)
class Scan:
    """What detectors look at: the message, the disclosure and the terms, and the key.

    ``key`` is the fingerprint key, or a callable that returns it; it is read the first
    time a finding is made.
    """

    text: str
    disclosure: Mapping = field(default_factory=dict)
    allowlist: tuple[str, ...] = ()
    canary_terms: tuple[str, ...] = ()
    personal_terms: tuple[str, ...] = ()
    key: Union[bytes, Callable[[], bytes]] = fingerprint_key

    @cached_property
    def rendered(self) -> Optional[View]:
        """The message as a Markdown or HTML reader sees it, when that differs."""
        return render(self.text)

    @cached_property
    def normalised(self) -> tuple[Normalised, ...]:
        """The readings of the message folded for matching terms: as written, and rendered."""
        readings = (_view_of(self.text), self.rendered)
        return tuple(_folded(reading) for reading in readings if reading is not None)

    @cached_property
    def readers(self) -> frozenset[str]:
        """The ids of the people the disclosure was computed for: the readers."""
        return frozenset(self.disclosure.get("people") or ())

    @cached_property
    def least_clearance(self) -> str:
        """The least-cleared reader's clearance; ``clear`` when the disclosure has none."""
        return self.disclosure.get("least_clearance") or LABELS[0]

    @cached_property
    def audience_severity(self) -> int:
        """The severity of a finding that only the audience makes serious."""
        if _label_rank(self.least_clearance, unknown=0) == 0:
            return SEVERITY_WIDE_AUDIENCE
        return SEVERITY_NARROW_AUDIENCE

    @cached_property
    def _allowed_hosts(self) -> tuple[str, ...]:
        return tuple(filter(None, map(_allowlist_entry, self.allowlist)))

    @cached_property
    def _key(self) -> bytes:
        key = self.key() if callable(self.key) else self.key
        if not isinstance(key, bytes) or not key:
            raise FingerprintKeyError("the fingerprint key must be non-empty bytes")
        return key

    def allows(self, host: str) -> bool:
        """Whether ``host`` is an allowlisted host or a subdomain of one."""
        return bool(host) and any(
            host == allowed or host.endswith("." + allowed)
            for allowed in self._allowed_hosts
        )

    def spans(
        self, term: FoldedTerm, *, whole_word: bool = True
    ) -> list[tuple[int, int]]:
        """Where ``term`` occurs in any reading of the message, in order."""
        found = {
            span
            for reading in self.normalised
            for span in reading.spans(term, whole_word=whole_word)
        }
        return sorted(found)

    @cached_property
    def _fingerprints(self) -> dict[tuple[bool, bool, str], str]:
        return {}

    def fingerprint(
        self, start: int, end: int, *, fold: bool, material: Optional[str] = None
    ) -> str:
        """The keyed fingerprint of ``text[start:end]``: normalised with ``fold``, else as
        a reader sees it; or of ``material`` exactly, when a check names what it found."""
        exact = material is not None
        value = material if exact else self.text[start:end]
        if (fold, exact, value) not in self._fingerprints:
            if exact:
                hashed = value
            else:
                hashed = (_folded_value if fold else _stripped_value)(value) or value
            message = hashed.encode("utf-8", "surrogatepass")
            digest = hmac.new(self._key, message, sha256).hexdigest()
            self._fingerprints[fold, exact, value] = digest
        return self._fingerprints[fold, exact, value]

    def finding(
        self,
        kind: str,
        start: int,
        end: int,
        *,
        rule: str,
        severity: int,
        entity: Optional[str] = None,
        label: Optional[str] = None,
        sealed_from: tuple[str, ...] = (),
        material: Optional[str] = None,
    ) -> Finding:
        """A :class:`Finding` for ``text[start:end]``, fingerprinted as its kind says, or
        over ``material`` when given."""
        fold = kind in FOLDED_KINDS
        return Finding(
            kind=kind,
            start=start,
            end=end,
            entity=entity,
            label=label,
            sealed_from=sealed_from,
            rule=rule,
            severity=severity,
            fingerprint=self.fingerprint(start, end, fold=fold, material=material),
        )


#: ``(scan) -> findings``: one detector, or one check within a detector.
Detector = Callable[[Scan], Iterable[Finding]]


def chain(*detectors: Detector, name: str = "chained") -> Detector:
    """One detector that yields the findings of ``detectors``, in order."""

    def chained(scan: Scan) -> Iterator[Finding]:
        for detector in detectors:
            yield from detector(scan)

    chained.__name__ = name
    return chained


def _holds_a_literal(
    text: str, literals: tuple[str, ...], *, ignore_case: bool = False
) -> bool:
    """Whether ``text`` holds one of ``literals``; true when there are none to require."""
    if not literals:
        return True
    haystack = text.casefold() if ignore_case else text
    return any(literal in haystack for literal in literals)


# ---- secret ----


@dataclass(frozen=True)
class SecretRule:
    """A secret's shape: ``rule`` names it in findings, ``pattern`` is its expression.

    ``literals``: strings one of which every match contains (in case-folded text, with
    ``ignore_case``). A text holding none is not scanned for the rule, which matters because
    a pattern that starts at a word boundary cannot use the regular-expression engine's
    fast search for a literal prefix. Empty: always scanned.

    ``word_start``: a match must start a word. ``wrappable``: the rule is also looked for in
    the message with line breaks, invisible characters and emphasis marks removed, so a
    token split by them is found; its word boundary is then checked against the message.
    A wrappable pattern must not fail after an unbounded repetition, or that scan, which
    has no word boundary to anchor it, turns quadratic.
    """

    rule: str
    pattern: str
    literals: tuple[str, ...] = ()
    word_start: bool = True
    wrappable: bool = True
    ignore_case: bool = False

    def may_match(self, text: str) -> bool:
        """Whether ``text`` holds one of :attr:`literals`, or the rule has none."""
        return _holds_a_literal(text, self.literals, ignore_case=self.ignore_case)

    @cached_property
    def in_text(self) -> re.Pattern:
        """The pattern as it is looked for in the message."""
        return re.compile(rf"\b{self.pattern}" if self.word_start else self.pattern)

    @cached_property
    def in_stripped(self) -> re.Pattern:
        """The pattern as it is looked for in the message with the strippable removed."""
        return re.compile(self.pattern)


# The rules after the 0.1 shapes adapt gitleaks' default configuration, under this notice:
#
#   MIT License
#
#   Copyright (c) 2019 Zachary Rice
#
#   Permission is hereby granted, free of charge, to any person obtaining a copy
#   of this software and associated documentation files (the "Software"), to deal
#   in the Software without restriction, including without limitation the rights
#   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#   copies of the Software, and to permit persons to whom the Software is
#   furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included in all
#   copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#   SOFTWARE.

#: The secret rules :func:`secret_detector` uses by default: the 0.1 token shapes and
#: private-key header, then distinctive-prefix rules adapted from gitleaks (the gitleaks id
#: precedes each).
SECRET_RULES: tuple[SecretRule, ...] = (
    *(
        SecretRule(rule, shape, literals)
        for rule, shape, literals in zip(TOKEN_RULES, TOKEN_SHAPES, TOKEN_LITERALS)
    ),
    SecretRule(
        "private-key",
        PRIVATE_KEY_PATTERN,
        ("-----BEGIN ",),
        word_start=False,
        wrappable=False,
    ),
    # gitlab-pat
    SecretRule("gitlab-token", r"glpat-[\w-]{20}", ("glpat-",)),
    # gcp-api-key
    SecretRule("google-api-key", r"AIza[\w-]{35}", ("AIza",)),
    # npm-access-token
    SecretRule("npm-token", r"(?i:npm_[a-z0-9]{36})", ("npm_",), ignore_case=True),
    # pypi-upload-token
    SecretRule(
        "pypi-token", r"pypi-AgEIcHlwaS5vcmc[\w-]{50,1000}", ("pypi-AgEIcHlwaS5vcmc",)
    ),
    # sendgrid-api-token
    SecretRule("sendgrid-api-key", r"SG\.[A-Za-z0-9=_.-]{66}", ("SG.",)),
    # age-secret-key
    SecretRule(
        "age-secret-key",
        r"AGE-SECRET-KEY-1[QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L]{58}",
        ("AGE-SECRET-KEY-1",),
    ),
    # telegram-bot-api-token, without its context words
    SecretRule("telegram-bot-token", r"[0-9]{5,16}:A[\w-]{34}(?![\w-])", (":A",)),
    # stripe-access-token
    SecretRule(
        "stripe-key",
        r"(?:sk|rk)_(?:test|live|prod)_[A-Za-z0-9]{10,99}",
        ("k_test_", "k_live_", "k_prod_"),
    ),
    # shopify-access-token
    SecretRule("shopify-token", r"shpat_[a-fA-F0-9]{32}", ("shpat_",)),
    # digitalocean-pat
    SecretRule("digitalocean-token", r"dop_v1_[a-f0-9]{64}", ("dop_v1_",)),
    # slack-webhook-url
    SecretRule(
        "slack-webhook",
        r"(?:https?://)?hooks\.slack\.com/(?:services|workflows|triggers)"
        r"/[A-Za-z0-9+/]{43,56}",
        ("hooks.slack.com/",),
    ),
    # slack-app-token
    SecretRule(
        "slack-app-token",
        r"(?i:xapp-\d-[A-Z0-9]{1,64}-\d{1,16}-[a-z0-9]{1,128})",
        ("xapp-",),
        ignore_case=True,
    ),
    # doppler-api-token
    SecretRule("doppler-token", r"dp\.pt\.[A-Za-z0-9]{43}", ("dp.pt.",)),
    # linear-api-key
    SecretRule("linear-api-key", r"lin_api_[A-Za-z0-9]{40}", ("lin_api_",)),
    # huggingface-organization-api-token
    SecretRule("hugging-face-org-token", r"api_org_[A-Za-z]{34}", ("api_org_",)),
    # aws-access-token, less the AKIA prefix the 0.1 rule has
    SecretRule(
        "aws-access-key-id",
        r"(?:A3T[A-Z0-9]|ASIA|ABIA|ACCA)[A-Z2-7]{16}\b",
        ("A3T", "ASIA", "ABIA", "ACCA"),
    ),
    # postman-api-token
    SecretRule("postman-api-key", r"PMAK-[A-Fa-f0-9]{24}-[A-Fa-f0-9]{34}", ("PMAK-",)),
    # grafana-service-account-token
    SecretRule(
        "grafana-service-account-token",
        r"(?i:glsa_[a-z0-9]{32}_[a-f0-9]{8})",
        ("glsa_",),
        ignore_case=True,
    ),
    # perplexity-api-key
    SecretRule("perplexity-api-key", r"pplx-[A-Za-z0-9]{48}", ("pplx-",)),
    # sentry-user-token
    SecretRule("sentry-user-token", r"sntryu_[a-f0-9]{64}", ("sntryu_",)),
    # pulumi-api-token
    SecretRule("pulumi-token", r"pul-[a-f0-9]{40}", ("pul-",)),
    # vault-service-token
    SecretRule("vault-token", r"hvs\.[\w-]{90,120}", ("hvs.",)),
    # 1password-service-account-token
    SecretRule(
        "1password-service-account-token",
        r"ops_eyJ[A-Za-z0-9+/]{250,}={0,3}",
        ("ops_eyJ",),
    ),
    # jwt; not wrappable: its bounded parts would still make the unanchored scan slow
    SecretRule(
        "jwt",
        r"ey[A-Za-z0-9]{17,4096}\.ey[A-Za-z0-9/\\_-]{17,4096}"
        r"\.(?:[A-Za-z0-9/\\_-]{10,4096}={0,2})?",
        (".ey",),
        wrappable=False,
    ),
)

_WORD_CHAR = re.compile(r"\w")


def secret_detector(rules: Iterable[SecretRule] = SECRET_RULES) -> Detector:
    """A detector of ``rules``: a ``secret`` finding for each match, severity 5.

    Each rule is looked for in the message and, when it holds markup, in its rendering;
    each wrappable rule also in both with line breaks, invisible characters and emphasis
    marks removed. A token found in several of these is one finding, as long as the
    longest match.
    """
    rules = tuple(rules)

    def detect_secrets(scan: Scan) -> Iterator[Finding]:
        text = scan.text
        ends: dict[tuple[str, int], int] = {}

        def keep(rule: str, start: int, end: int) -> None:
            ends[rule, start] = max(end, ends.get((rule, start), end))

        readings = [_view_of(text)] + ([scan.rendered] if scan.rendered else [])
        for reading in readings:
            for rule in rules:
                if rule.may_match(reading.text):
                    for match in rule.in_text.finditer(reading.text):
                        keep(rule.rule, *reading.source_span(*match.span()))
        wrappable = [rule for rule in rules if rule.wrappable]
        for reading in readings:
            if not wrappable or not _STRIPPABLE.search(reading.text):
                continue
            stripped = _without(reading, _STRIPPABLE)
            for rule in wrappable:
                if not rule.may_match(stripped.text):
                    continue
                for match in rule.in_stripped.finditer(stripped.text):
                    start, end = stripped.source_span(*match.span())
                    if rule.word_start and start and _WORD_CHAR.match(text[start - 1]):
                        continue
                    keep(rule.rule, start, end)
        for (rule, start), end in ends.items():
            yield scan.finding(
                "secret", start, end, rule=rule, severity=SEVERITY_SECRET
            )

    return detect_secrets


# ---- canary, vocabulary, third party ----


def detect_canaries(scan: Scan) -> Iterator[Finding]:
    """A ``canary`` finding, severity 5, wherever a canary term occurs, even inside a word:
    a canary is unique by construction, so a match anywhere is the alarm."""
    for term in scan.canary_terms:
        for start, end in scan.spans(fold_term(term), whole_word=False):
            yield scan.finding(
                "canary", start, end, rule="canary-term", severity=SEVERITY_CANARY
            )


def _vocabulary_entries(scan: Scan) -> Iterator[tuple[FoldedTerm, Mapping]]:
    """Each vocabulary entry with a term, and its term folded."""
    for index, entry in enumerate(scan.disclosure.get("vocabulary") or ()):
        if not isinstance(entry, Mapping):
            raise TypeError(
                f"disclosure vocabulary entry {index} is a {type(entry).__name__}, "
                "not a mapping with a 'term'"
            )
        term = entry.get("term")
        folded = fold_term(term) if isinstance(term, str) else None
        if folded and folded.text:
            yield folded, entry


def _term_severity(scan: Scan, label: str, sealed_from: tuple[str, ...]) -> int:
    if scan.readers.intersection(sealed_from):
        return SEVERITY_SEALED
    above = len(LABELS)  # an unknown label counts as the most restrictive
    if _label_rank(label, unknown=above) > _label_rank(scan.least_clearance, unknown=0):
        return SEVERITY_ABOVE_CLEARANCE
    return scan.audience_severity


def _term_findings(scan: Scan, *, people: bool) -> Iterator[Finding]:
    kind, rule = ("third_party", "person-name") if people else ("vocabulary", "term")
    for folded, entry in _vocabulary_entries(scan):
        entity = entry.get("entity")
        is_person = isinstance(entity, str) and entity.startswith(PERSON_PREFIX)
        if is_person != people:
            continue
        if is_person and entity.removeprefix(PERSON_PREFIX) in scan.readers:
            continue
        label = entry.get("label") or (
            DFLT_PERSON_LABEL if is_person else DFLT_ENTITY_LABEL
        )
        sealed_from = _ids(entry.get("sealed_from"))
        severity = _term_severity(scan, label, sealed_from)
        for start, end in scan.spans(folded):
            yield scan.finding(
                kind,
                start,
                end,
                rule=rule,
                severity=severity,
                entity=entity,
                label=label,
                sealed_from=sealed_from,
            )


def detect_vocabulary(scan: Scan) -> Iterator[Finding]:
    """A ``vocabulary`` finding for each whole-word occurrence of a disclosure term whose
    entity is not a person."""
    return _term_findings(scan, people=False)


def detect_third_parties(scan: Scan) -> Iterator[Finding]:
    """A ``third_party`` finding for each whole-word occurrence of a person's disclosure
    term, when that person is not a reader."""
    return _term_findings(scan, people=True)


# ---- exfiltration: links and images ----

#: Schemes whose URLs a browser reads with any number of slashes or backslashes before the
#: host (WHATWG URL's special schemes, less ``file``, which names no remote host).
_SPECIAL_SCHEMES = frozenset({"http", "https", "ws", "wss", "ftp"})
_HOSTLESS_SCHEMES = frozenset({"file"})
_C0_AND_SPACE = "".join(map(chr, range(0x21)))
_URL_IGNORED = re.compile(r"[\t\n\r]")
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]{0,31}):")
_AUTHORITY = re.compile(r"[^/\\?#]*")
_MARKDOWN_ESCAPE = re.compile(r"\\(?=[!-/:-@\[-`{-~])")


def _host(authority: str) -> str:
    """The host an authority names: after its last ``@``, without port or brackets."""
    host = authority.rpartition("@")[2]
    if host.startswith("["):
        host = host[1:].partition("]")[0]
    else:
        host = host.partition(":")[0]
    return unquote(host).strip().rstrip(".").lower()


def _url_host(url: str) -> Optional[str]:
    """The host a browser resolves ``url`` to; ``""`` when it has an authority whose host
    cannot be read; None when it names no host (a relative URL, or a scheme without one)."""
    scheme = _SCHEME.match(url)
    if scheme:
        name, rest = scheme.group(1).lower(), url[scheme.end() :]
        if name in _HOSTLESS_SCHEMES:
            return None
        if name in _SPECIAL_SCHEMES:
            rest = rest.lstrip("/\\")
        elif rest.startswith("//"):
            rest = rest[2:]
        else:
            return None
    elif len(url) > 1 and url[0] in "/\\" and url[1] in "/\\":
        rest = url.lstrip("/\\")
    else:
        return None
    if (
        not rest
    ):  # nothing after the slashes loads nothing, unless the read was cut short
        return "" if len(url) >= MAX_DESTINATION else None
    return _host(_AUTHORITY.match(rest).group())


@lru_cache(maxsize=4096)
def _destination_hosts(value: str) -> frozenset[str]:
    """The hosts a link or image destination may resolve to.

    The value is read as a browser reads it (character references decoded, tabs and line
    breaks removed, surrounding spaces stripped), both with and without Markdown's
    backslash escapes applied, since a renderer may or may not apply them.
    """
    url = _URL_IGNORED.sub("", html.unescape(value)).strip(_C0_AND_SPACE)
    readings = {url, _MARKDOWN_ESCAPE.sub("", url)}
    return frozenset(host for host in map(_url_host, readings) if host is not None)


def _srcset_hosts(value: str) -> frozenset[str]:
    """The hosts of every candidate of a ``srcset``: comma-separated URLs, each with an
    optional descriptor."""
    urls = (candidate.split()[0] for candidate in value.split(",") if candidate.split())
    return frozenset().union(*map(_destination_hosts, urls))


#: A Markdown inline destination starts after ``](``; an image's after ``![...](``.
_INLINE_DESTINATION = re.compile(r"\]\(")
_INLINE_IMAGE = re.compile(r"!\[(?:[^\[\]]|\[[^\[\]]{0,999}\]){0,999}\]\(")
#: A reference definition (``[label]: destination``), and a reference-style image
#: (``![alt][label]``, ``![label][]``, ``![label]``).
_REFERENCE_DEFINITION = re.compile(
    r"^[ \t]{0,3}\[(?P<label>[^\[\]\n]{1,999})\]:", re.MULTILINE
)
_IMAGE_REFERENCE = re.compile(
    r"!\[(?P<text>[^\[\]\n]{0,999})\](?:\[(?P<label>[^\[\]\n]{0,999})\])?"
)
#: A Markdown destination: after up to one line break, bracketed or bare.
_DESTINATION_VALUE = re.compile(
    rf"[ \t]{{0,8}}(?:\r?\n[ \t]{{0,8}})?"
    rf"(?:<(?P<bracketed>[^<>\n]{{0,{MAX_DESTINATION}}})"
    rf"|(?P<bare>[^\s()<>]{{1,{MAX_DESTINATION}}}))"
)
#: HTML attributes whose URL loads without a click, and those that link.
_LOADING_ATTRIBUTES = frozenset(
    {"src", "srcset", "poster", "background", "data", "lowsrc", "dynsrc"}
)
_HTML_ATTRIBUTE = re.compile(
    r"(?<![\w-])(?P<name>src|srcset|poster|background|data|lowsrc|dynsrc"
    r"|href|action|formaction)"
    r"[ \t\r\n]{0,8}=[ \t\r\n]{0,8}"
    rf"(?:\"(?P<double>[^\"]{{0,{MAX_DESTINATION}}})"
    rf"|'(?P<single>[^']{{0,{MAX_DESTINATION}}})"
    rf"|(?P<bare>[^\s\"'=<>`]{{1,{MAX_DESTINATION}}}))",
    re.IGNORECASE,
)
#: A Markdown autolink: ``<scheme:...>``.
_AUTOLINK = re.compile(
    rf"<(?P<value>[A-Za-z][A-Za-z0-9+.-]{{1,31}}:[^\s<>]{{0,{MAX_DESTINATION}}})>"
)
_HOST_END = r"[^\s/\\?#<>\"'`()\[\]{}|^]"
#: A URL in plain text, with a scheme and an authority.
_SCHEME_URL = re.compile(
    rf"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{{0,31}}://(?P<authority>{_HOST_END}*)"
)
#: A ``www.`` host without a scheme, which GitHub and many renderers autolink.
_BARE_WWW = re.compile(
    r"(?<![\w.@/:-])(?P<authority>www\.[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,126})"
)
_URL_TAIL = re.compile(r"[^\s<>\"'`]*")
_URL_TRAILING_PUNCTUATION = ".,:;!?*_~'\")]}"


def _reference_label(label: str) -> str:
    return " ".join(label.split()).casefold()


def _markdown_destinations(text: str) -> Iterator[tuple[int, int, frozenset, bool]]:
    """``(start, end, hosts, image)`` for each inline and reference destination."""

    def destination(position: int):
        match = _DESTINATION_VALUE.match(text, position)
        if match is None:
            return None
        group = "bracketed" if match.group("bracketed") is not None else "bare"
        return match.start(group), match.end(group), match.group(group)

    if "](" in text:
        images = {match.end() for match in _INLINE_IMAGE.finditer(text)}
        for opening in _INLINE_DESTINATION.finditer(text):
            found = destination(opening.end())
            if found:
                start, end, value = found
                yield start, end, _destination_hosts(value), opening.end() in images
    if "]:" in text:
        labels = {
            _reference_label(match.group("label") or match.group("text"))
            for match in _IMAGE_REFERENCE.finditer(text)
        }
        for definition in _REFERENCE_DEFINITION.finditer(text):
            found = destination(definition.end())
            if found:
                start, end, value = found
                image = _reference_label(definition.group("label")) in labels
                yield start, end, _destination_hosts(value), image


#: How far back an attribute looks for the ``<`` of the tag it would belong to.
MAX_TAG_LOOKBACK = 1024
_TAG_OPEN = re.compile(r"<[A-Za-z]")


def _named_host(host: str) -> bool:
    """Whether ``host`` reads as a host outside markup: it has a dot, or is ``localhost``."""
    return "." in host or host == "localhost"


def _inside_tag(text: str, position: int) -> bool:
    """Whether ``position`` is inside an HTML start tag: the last ``<`` before it, within
    :data:`MAX_TAG_LOOKBACK` characters, opens a tag that no ``>`` has closed yet."""
    opening = text.rfind("<", max(0, position - MAX_TAG_LOOKBACK), position)
    return (
        opening >= 0
        and text.rfind(">", opening, position) < 0
        and _TAG_OPEN.match(text, opening) is not None
    )


def _html_destinations(text: str) -> Iterator[tuple[int, int, frozenset, bool]]:
    """``(start, end, hosts, image)`` for each URL attribute, and each autolink. An
    attribute that is not plainly inside a tag (``data=//x`` in a code block) counts only
    hosts that read as hosts (:func:`_named_host`)."""
    if "=" in text:
        for match in _HTML_ATTRIBUTE.finditer(text):
            group = next(
                g for g in ("double", "single", "bare") if match.group(g) is not None
            )
            name, value = match.group("name").lower(), match.group(group)
            hosts = (
                _srcset_hosts(value) if name == "srcset" else _destination_hosts(value)
            )
            if not _inside_tag(text, match.start()):
                hosts = frozenset(filter(_named_host, hosts))
            image = name in _LOADING_ATTRIBUTES
            yield match.start(group), match.end(group), hosts, image
    if "<" in text:
        for match in _AUTOLINK.finditer(text):
            value = match.group("value")
            yield (
                match.start("value"),
                match.end("value"),
                _destination_hosts(value),
                False,
            )


def _text_urls(text: str) -> Iterator[tuple[int, int, frozenset, bool]]:
    """``(start, end, hosts, image)`` for each URL written in plain text. A URL's path
    stops where the next one starts, so a run of URLs is scanned once. A host with no dot,
    no port and nothing after it (``https://exam`` cut by a tab) is no link: GitHub's
    autolinker needs a dot, and a single-label host shows itself by a port or a path."""
    candidates = []
    if "://" in text:
        candidates += [(m.start(), m.end("authority"), m.group("authority"), False) for m in _SCHEME_URL.finditer(text)]  # fmt: skip
    if "www." in text:
        candidates += [(m.start(), m.end("authority"), m.group("authority"), True) for m in _BARE_WWW.finditer(text)]  # fmt: skip
    candidates.sort()
    for index, (start, authority_end, authority, bare) in enumerate(candidates):
        limit = candidates[index + 1][0] if index + 1 < len(candidates) else len(text)
        end = _URL_TAIL.match(text, authority_end, max(limit, authority_end)).end()
        while end > authority_end and text[end - 1] in _URL_TRAILING_PUNCTUATION:
            end -= 1
        if bare:
            hosts = frozenset({_host(authority)})
        else:
            hosts = frozenset(filter(None, _destination_hosts(text[start:end])))
            if end == authority_end and ":" not in authority:
                hosts = frozenset(filter(_named_host, hosts))
        if hosts:
            yield start, end, hosts, False


#: Two or more slashes or backslashes, not inside a word or a path, then an authority,
#: which may keep Markdown's escaped punctuation for a renderer to unescape.
_LOOSE_AUTHORITY = re.compile(
    r"(?<![\w/\\])[/\\]{2,}"
    r"(?P<authority>(?:[^\s/\\?#<>\"'`\[\]{}|^]|\\[!-/:-@\[-`{-~]){1,253})"
)
_HOST_TRAILING_PUNCTUATION = ")].,;:!?'\""


def _loose_urls(text: str) -> Iterator[tuple[int, int, frozenset, bool]]:
    """``(start, end, hosts, image)`` for each ``//host`` anywhere in the text whose host
    looks like one (it has a dot, or is ``localhost``), read with and without Markdown
    escapes. This backs the parsers: a construct they do not model (a reference definition
    inside a list or a quote, parentheses in a destination) still names its host here."""
    if "//" not in text and "\\\\" not in text:
        return
    for match in _LOOSE_AUTHORITY.finditer(text):
        authority = match.group("authority")
        readings = (authority.partition("\\")[0], _MARKDOWN_ESCAPE.sub("", authority))
        hosts = frozenset(
            host
            for host in (
                _host(_AUTHORITY.match(reading).group()).rstrip(
                    _HOST_TRAILING_PUNCTUATION
                )
                for reading in readings
            )
            if _named_host(host)
        )
        if hosts:
            yield match.start(), match.end("authority"), hosts, False


def scan_links(scan: Scan) -> Iterator[Finding]:
    """An ``exfiltration`` finding for each link or image whose host is not allowlisted.

    Destinations are read in Markdown (inline and reference), HTML attributes, autolinks
    and plain text, each as a browser resolves it; a destination is found when any
    reading of it names a host outside the allowlist, or an authority whose host cannot
    be read. Then every ``//host`` anywhere (:func:`_loose_urls`) not inside a URL
    already found. The rule is ``image-host`` when the URL loads without a click (a
    Markdown image, or an attribute such as ``src``), else ``link-host``. The finding
    spans the URL.
    """
    text = scan.text
    found: dict[int, tuple[int, bool]] = {}
    image_starts: set[int] = set()

    def consider(start: int, end: int, hosts: frozenset, image: bool) -> None:
        if any(not scan.allows(host) for host in hosts):
            previous_end, previous_image = found.get(start, (end, False))
            found[start] = (max(end, previous_end), image or previous_image)

    parsed = chain_iterables(
        _markdown_destinations(text), _html_destinations(text), _text_urls(text)
    )
    for start, end, hosts, image in parsed:
        if image:
            image_starts.add(start)
        consider(start, end, hosts, image)
    spans = sorted(found.items())
    starts = [start for start, _ in spans]
    for start, end, hosts, _ in _loose_urls(text):
        index = bisect_right(starts, start) - 1
        if index >= 0 and starts[index] < start < spans[index][1][0]:
            continue  # inside a URL already found
        consider(start, end, hosts, start in image_starts)
    for start, (end, image) in sorted(found.items()):
        yield scan.finding(
            "exfiltration",
            start,
            end,
            rule="image-host" if image else "link-host",
            severity=SEVERITY_EXFILTRATION,
        )


def link_urls(text: str) -> tuple[str, ...]:
    """Every link and image destination in ``text``, in full, in order, each once.

    Read as :func:`scan_links` reads them (Markdown, HTML attributes, autolinks, plain
    URLs, then any ``//host`` outside those), whatever their host: what the operator reads
    before releasing a message, since a link's title can say one place and its
    destination another.

    >>> link_urls("See [the docs](https://example.org/a) and https://example.com/b.")
    ('https://example.org/a', 'https://example.com/b')
    """
    found: dict[int, int] = {}
    parsed = chain_iterables(
        _markdown_destinations(text), _html_destinations(text), _text_urls(text)
    )
    for start, end, _, _ in parsed:
        found[start] = max(end, found.get(start, end))
    spans = sorted(found.items())
    starts = [start for start, _ in spans]
    for start, end, _, _ in _loose_urls(text):
        index = bisect_right(starts, start) - 1
        if index >= 0 and starts[index] <= start < spans[index][1]:
            continue  # inside a URL already found
        found[start] = max(end, found.get(start, end))
    return tuple(dict.fromkeys(text[start:end] for start, end in sorted(found.items())))


# ---- exfiltration: encoded runs ----

_BASE64_RUN = re.compile(
    rf"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{{{MIN_BASE64_RUN},}}={{0,2}}"
)
#: A line that may belong to a wrapped base64 block: after any indentation or ``>`` quote
#: marks, nothing but base64 characters.
_BASE64_LINE = re.compile(
    r"^[ \t>]*(?P<body>[A-Za-z0-9+/]+={0,2})[ \t]*\r?$", re.MULTILINE
)
_HEX_RUN = re.compile(rf"(?<![0-9A-Fa-f])[0-9A-Fa-f]{{{MIN_HEX_RUN},}}(?![0-9A-Fa-f])")
_DIGIT = re.compile(r"[0-9]")
_UPPER = re.compile(r"[A-Z]")
_LOWER = re.compile(r"[a-z]")
_HEX_LETTER = re.compile(r"[A-Fa-f]")


def _mixes_base64_classes(run: str) -> bool:
    return bool(_DIGIT.search(run) and _UPPER.search(run) and _LOWER.search(run))


def _base64_block(text: str, lines: list[tuple[int, int]]) -> Optional[tuple[int, int]]:
    """The span of a wrapped base64 block made of ``lines`` (body spans of one width but a
    shorter last), or None. A shorter last line counts only when it reads as the end of
    data (padded, or long and mixed), not as a word such as a signature after the block."""
    width = lines[0][1] - lines[0][0]
    last = text[lines[-1][0] : lines[-1][1]]
    if len(last) < width and not (
        last.endswith("=")
        or (len(last) >= MIN_BASE64_TAIL and _mixes_base64_classes(last))
    ):
        lines = lines[:-1]
    if len(lines) < 2 or width < MIN_WRAPPED_BASE64_LINE:
        return None
    run = "".join(text[start:end] for start, end in lines)
    if len(run) < MIN_BASE64_RUN or not _mixes_base64_classes(run):
        return None
    return lines[0][0], lines[-1][1]


def _wrapped_base64_blocks(text: str) -> list[tuple[int, int]]:
    """The spans of base64 wrapped over adjacent lines of one width, after any indentation
    or quote marks, in order."""
    blocks, lines, width, previous_end = [], [], 0, -2
    for match in _BASE64_LINE.finditer(text):
        start, end = match.span("body")
        continues = (
            lines
            and match.start() == previous_end + 1
            and lines[-1][1] - lines[-1][0] == width
            and end - start <= width
        )
        if continues:
            lines.append((start, end))
        else:
            if lines and (block := _base64_block(text, lines)):
                blocks.append(block)
            lines, width = [(start, end)], end - start
        previous_end = match.end()
    if lines and (block := _base64_block(text, lines)):
        blocks.append(block)
    return blocks


def scan_base64_runs(scan: Scan) -> Iterator[Finding]:
    """An ``exfiltration`` finding for each base64 or base64url run of at least
    :data:`MIN_BASE64_RUN` characters, on one line or wrapped over several (indented or
    quoted too), that mixes digits, capitals and small letters, which encoded data does and
    a long word or path rarely does."""
    blocks = _wrapped_base64_blocks(scan.text)  # disjoint, in order
    block_starts = [start for start, _ in blocks]
    runs = []
    for match in _BASE64_RUN.finditer(scan.text):
        index = bisect_right(block_starts, match.start()) - 1
        inside = index >= 0 and match.end() <= blocks[index][1]
        if not inside and _mixes_base64_classes(match.group()):
            runs.append(match.span())
    for start, end in sorted(blocks + runs):
        yield scan.finding(
            "exfiltration",
            start,
            end,
            rule="base64-run",
            severity=SEVERITY_EXFILTRATION,
        )


def scan_hex_runs(scan: Scan) -> Iterator[Finding]:
    """An ``exfiltration`` finding for each run of at least :data:`MIN_HEX_RUN` hex digits
    that mixes digits and letters."""
    for match in _HEX_RUN.finditer(scan.text):
        run = match.group()
        if _DIGIT.search(run) and _HEX_LETTER.search(run):
            yield scan.finding(
                "exfiltration",
                match.start(),
                match.end(),
                rule="hex-run",
                severity=SEVERITY_EXFILTRATION,
            )


# ---- exfiltration: invisible characters ----

_BOM = "\ufeff"
_PRESENTATION_SELECTORS = frozenset({"\ufe0e", "\ufe0f"})
#: The punctuation that has an emoji presentation (double exclamation, exclamation question,
#: wavy dash, part alternation mark); other punctuation takes no presentation selector.
_EMOJI_PUNCTUATION = frozenset("\u203c\u2049\u3030\u303d")
_EMOJI_JOINING = frozenset({"\u200d", "\ufe0f"})
#: The longest run of joiners and selectors inside an emoji sequence (VS16, then ZWJ).
_MAX_EMOJI_JOINING_RUN = 2
_KEYCAP_BASES = frozenset("0123456789#*")
_KEYCAP = "\u20e3"
_BLACK_FLAG = "\U0001f3f4"
_CANCEL_TAG = "\U000e007f"
_TAG_OFFSET = 0xE0000
#: The tag sequences of the only subdivision flags Unicode recommends for general
#: interchange (England, Scotland, Wales). Any other tag run renders as nothing.
_FLAG_TAGS = frozenset({"gbeng", "gbsct", "gbwls"})
_JOINERS = frozenset({"\u200c", "\u200d"})
#: Scripts whose spelling uses the zero-width joiner and non-joiner: Arabic, Syriac and
#: Thaana, and the Brahmic scripts from Devanagari to Sinhala.
_JOINING_SCRIPTS = (
    ("\u0600", "\u08ff"),
    ("\u0900", "\u0dff"),
    ("\ufb50", "\ufdff"),
    ("\ufe70", "\ufefc"),
)
_BIDI_MARKS = frozenset({"\u200e", "\u200f"})
_PICTOGRAPHIC_CATEGORIES = frozenset({"So", "Sk"})
_RIGHT_TO_LEFT = frozenset({"R", "AL"})


def _pictographic(char: str) -> bool:
    return (
        bool(char)
        and not char.isascii()
        and unicodedata.category(char) in _PICTOGRAPHIC_CATEGORIES
    )


def _needed_invisible(text: str, start: int, end: int) -> bool:
    """Whether the invisible run ``text[start:end]`` is one that text needs to render: a
    byte-order mark opening the text, an emoji presentation selector after a symbol or a
    keycap base, the joiners of an emoji sequence, one of the three subdivision flags'
    tags, a joiner inside a word of a joining script, or a direction mark beside
    right-to-left text."""
    run = text[start:end]
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""

    def symbol(char: str) -> bool:
        return bool(char) and (
            (not char.isascii() and unicodedata.category(char)[0] == "S")
            or char in _EMOJI_PUNCTUATION
        )

    def joining_script(char: str) -> bool:
        return bool(char) and any(low <= char <= high for low, high in _JOINING_SCRIPTS)

    def right_to_left(char: str) -> bool:
        return bool(char) and unicodedata.bidirectional(char) in _RIGHT_TO_LEFT

    if run == _BOM and start == 0:
        return True
    if run in _PRESENTATION_SELECTORS:
        return symbol(before) or (before in _KEYCAP_BASES and after == _KEYCAP)
    if (
        len(run) <= _MAX_EMOJI_JOINING_RUN
        and set(run) <= _EMOJI_JOINING
        and _pictographic(before)
        and ("\u200d" not in run or _pictographic(after))
    ):
        return True
    if before == _BLACK_FLAG and run.endswith(_CANCEL_TAG):
        codes = [ord(char) - _TAG_OFFSET for char in run[:-1]]
        tags = (
            "".join(map(chr, codes)) if all(0 < code < 0x80 for code in codes) else ""
        )
        return tags in _FLAG_TAGS
    if run in _JOINERS and joining_script(before) and joining_script(after):
        return True
    return run in _BIDI_MARKS and (right_to_left(before) or right_to_left(after))


_WHITESPACE = re.compile(r"\s")


def scan_invisible_characters(scan: Scan) -> Iterator[Finding]:
    """An ``exfiltration`` finding for each word holding invisible characters the text does
    not need to render (see :func:`_needed_invisible`): zero-width spaces, direction
    overrides, tag characters, variation selectors used to carry data.

    The runs within one word (no whitespace between them) are one finding, spanning them,
    fingerprinted over the invisible characters alone: a word stuffed with them is one
    thing to show the operator, and a message of them costs one finding, not thousands.
    """
    text = scan.text

    def region_finding(start: int, end: int, runs: list[str]) -> Finding:
        return scan.finding(
            "exfiltration",
            start,
            end,
            rule="invisible-character",
            severity=SEVERITY_EXFILTRATION,
            material="".join(runs),
        )

    region_start = region_end = None
    runs: list[str] = []
    for match in _INVISIBLE_RUN.finditer(text):
        start, end = match.span()
        if _needed_invisible(text, start, end):
            continue
        if runs and not _WHITESPACE.search(text, region_end, start):
            region_end = end
            runs.append(match.group())
            continue
        if runs:
            yield region_finding(region_start, region_end, runs)
        region_start, region_end, runs = start, end, [match.group()]
    if runs:
        yield region_finding(region_start, region_end, runs)


# ---- exfiltration: addresses and paths ----

#: Dotted IPv4 addresses, in any decimal digits (``\d`` includes full-width digits).
_IPV4 = re.compile(r"(?<![0-9A-Za-z.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9A-Za-z]|\.\d)")
_IPV6 = re.compile(r"(?<![\w:.])[0-9A-Fa-f:]{2,39}(?![\w:])")
#: Dots other than the full stop that a reader, or a URL parser, reads as one.
_DOTS_AS_FULL_STOP = str.maketrans({"\uff0e": ".", "\u3002": ".", "\uff61": "."})
_OCTAL_DIGITS = frozenset("01234567")
#: Private (RFC 1918 and unique-local), loopback, shared (RFC 6598) and link-local
#: networks: addresses that say something about the inside of a network.
INTERNAL_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def _internal(address: ipaddress._BaseAddress) -> bool:
    return any(address in network for network in INTERNAL_NETWORKS)


def _internal_ipv4(candidate: str) -> bool:
    """Whether a dotted IPv4 address is internal read as decimal, or, when an octet has a
    leading zero, as a URL parser reads it (octal)."""
    parts = candidate.split(".")
    readings = [[int(part) for part in parts]]
    if any(len(part) > 1 and part[0] == "0" for part in parts) and all(
        set(part) <= _OCTAL_DIGITS for part in parts
    ):
        readings.append(
            [int(part, 8) if part[0] == "0" else int(part) for part in parts]
        )
    return any(
        all(octet <= 255 for octet in octets)
        and _internal(ipaddress.IPv4Address(bytes(octets)))
        for octets in readings
    )


def _internal_ipv6(candidate: str) -> bool:
    if candidate.count(":") < 2:
        return False
    try:
        return _internal(ipaddress.IPv6Address(candidate))
    except ValueError:
        return False


def scan_private_addresses(scan: Scan) -> Iterator[Finding]:
    """An ``exfiltration`` finding for each IPv4 or IPv6 address in
    :data:`INTERNAL_NETWORKS`."""
    text = scan.text.translate(
        _DOTS_AS_FULL_STOP
    )  # one character for one: same offsets
    matches = [m for m in _IPV4.finditer(text) if _internal_ipv4(m.group())]
    matches += [m for m in _IPV6.finditer(text) if _internal_ipv6(m.group())]
    for match in matches:
        yield scan.finding(
            "exfiltration",
            match.start(),
            match.end(),
            rule="private-address",
            severity=SEVERITY_EXFILTRATION,
        )


@dataclass(frozen=True)
class PathRule:
    """A local-path shape: ``rule`` names it in findings, ``pattern`` finds where it starts.

    ``literals``, as for :class:`SecretRule`: strings one of which every match contains.
    A case-insensitive pattern has none, and is always scanned.
    """

    rule: str
    pattern: re.Pattern
    literals: tuple[str, ...] = ()

    def may_match(self, text: str) -> bool:
        """Whether ``text`` holds one of :attr:`literals`, or the rule has none."""
        return _holds_a_literal(text, self.literals)


#: The literals every match of each of :data:`LOCAL_PATH_PATTERNS` contains, in order.
LOCAL_PATH_LITERALS = (
    ("/Users/", "/home/", "/root/"),
    (),
    (),
    ("/private/var/", "/var/folders/"),
)
#: The 0.1 path patterns as rules.
LOCAL_PATH_RULES: tuple[PathRule, ...] = (
    *(
        PathRule("local-path", pattern, literals)
        for pattern, literals in zip(LOCAL_PATH_PATTERNS, LOCAL_PATH_LITERALS)
    ),
    PathRule("env-file", ENV_FILE_PATTERN, (".env",)),
)
_PATH_TAIL = re.compile(r"[^\s\"'`<>()\[\]{}|]*")
_PATH_TRAILING_PUNCTUATION = ".,:;!?"


def local_path_scanner(rules: Iterable[PathRule] = LOCAL_PATH_RULES) -> Detector:
    """A check for ``rules``: an ``exfiltration`` finding spanning each path, from where
    its rule matched to the end of the path. A match inside a path already found by the
    same rule is part of that path."""
    rules = tuple(rules)

    def scan_local_paths(scan: Scan) -> Iterator[Finding]:
        text = scan.text
        for rule in rules:
            if not rule.may_match(text):
                continue
            covered = 0
            for match in rule.pattern.finditer(text):
                if match.start() < covered:
                    continue
                end = _PATH_TAIL.match(text, match.end()).end()
                while end > match.end() and text[end - 1] in _PATH_TRAILING_PUNCTUATION:
                    end -= 1
                covered = end
                yield scan.finding(
                    "exfiltration",
                    match.start(),
                    end,
                    rule=rule.rule,
                    severity=SEVERITY_EXFILTRATION,
                )

    return scan_local_paths


#: The checks :data:`detect_exfiltration` runs.
EXFILTRATION_SCANNERS: tuple[Detector, ...] = (
    scan_links,
    scan_base64_runs,
    scan_hex_runs,
    scan_invisible_characters,
    scan_private_addresses,
    local_path_scanner(),
)
detect_exfiltration = chain(*EXFILTRATION_SCANNERS, name="detect_exfiltration")


# ---- personal ----

_EMAIL = re.compile(EMAIL_PATTERN)


def scan_personal_terms(scan: Scan) -> Iterator[Finding]:
    """A ``personal`` finding for each whole-word occurrence of a personal term."""
    for term in scan.personal_terms:
        for start, end in scan.spans(fold_term(term)):
            yield scan.finding(
                "personal",
                start,
                end,
                rule="personal-term",
                severity=scan.audience_severity,
            )


def scan_email_addresses(scan: Scan) -> Iterator[Finding]:
    """A ``personal`` finding for each email address."""
    if "@" not in scan.text:
        return
    for match in _EMAIL.finditer(scan.text):
        yield scan.finding(
            "personal",
            match.start(),
            match.end(),
            rule="email-address",
            severity=scan.audience_severity,
        )


#: The checks :data:`detect_personal` runs.
PERSONAL_SCANNERS: tuple[Detector, ...] = (scan_personal_terms, scan_email_addresses)
detect_personal = chain(*PERSONAL_SCANNERS, name="detect_personal")

#: The detectors :func:`detect` runs by default, one per kind of :data:`KINDS`.
DFLT_DETECTORS: tuple[Detector, ...] = (
    secret_detector(),
    detect_canaries,
    detect_vocabulary,
    detect_exfiltration,
    detect_personal,
    detect_third_parties,
)


def _terms(name: str, value: Iterable[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError(
            f"{name} is a collection of strings, not one string: {name}=[...]"
        )
    return tuple(value)


def detect(
    text: str,
    *,
    disclosure: Mapping,
    allowlist: Iterable[str] = (),
    canary_terms: Iterable[str] = (),
    personal_terms: Iterable[str] = (),
    key: Union[bytes, Callable[[], bytes], None] = None,
    detectors: Iterable[Detector] = DFLT_DETECTORS,
) -> tuple[Finding, ...]:
    """What ``detectors`` find in ``text``, ordered by position, each finding once.

    ``disclosure`` is the JSON of ``acquaint.disclosure`` (its ``people``,
    ``least_clearance`` and ``vocabulary`` are read; ``{}`` when there is none).
    ``allowlist`` holds the hosts a link may point at, subdomains included.
    ``canary_terms`` and ``personal_terms`` are the subject's canaries and the operator's
    own addresses, handles and paths. ``key`` is the fingerprint key (or a callable that
    returns it); by default :func:`fingerprint_key` with the configured state directory,
    read only when there is a finding. A caller with a config in hand should pass it.

    >>> key = b"k" * 32
    >>> [f.kind for f in detect("Use " + "ghp_" + "a" * 36, disclosure={}, key=key)]
    ['secret']
    >>> vocabulary = [{"term": "Heron", "entity": "project:heron", "label": "amber"}]
    >>> found = detect("Is he-RON late?", disclosure={"vocabulary": vocabulary}, key=key)
    >>> [(f.kind, f.start, f.end, f.severity) for f in found]
    [('vocabulary', 3, 9, 3)]
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be a str, not {type(text).__name__}")
    if not isinstance(disclosure, Mapping):
        raise TypeError(
            f"disclosure must be a mapping (acquaint.disclosure's JSON, or {{}}), "
            f"not {type(disclosure).__name__}"
        )
    if key is not None and not (isinstance(key, bytes) or callable(key)):
        raise TypeError(f"key must be bytes or a callable, not {type(key).__name__}")
    scan = Scan(
        text=text,
        disclosure=disclosure,
        allowlist=_terms("allowlist", allowlist),
        canary_terms=_terms("canary_terms", canary_terms),
        personal_terms=_terms("personal_terms", personal_terms),
        key=fingerprint_key if key is None else key,
    )
    found = {finding for detector in detectors for finding in detector(scan)}
    return tuple(
        sorted(found, key=lambda f: (f.start, f.end, f.kind, f.rule, f.entity or ""))
    )
