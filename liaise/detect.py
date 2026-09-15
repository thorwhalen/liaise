"""Detectors for outbound messages: what a message holds, reported without the value.

:func:`detect` runs :data:`DFLT_DETECTORS` over a message and returns :class:`Finding`
records. A finding says what was found (``kind`` and ``rule``), where (``start`` and
``end``, character offsets into the message as written), what it concerns (``entity``,
``label``, ``sealed_from``), how severe it is, and a keyed ``fingerprint`` that lets the
ledger correlate a repeat. It never holds the matched text. Detectors only find; what a
finding means for a send is the policy's decision (liaise discussion 32, §5.4).

The six kinds (discussion §5.2):

- ``secret``: the 0.1 leak scan's token shapes and private-key header, unchanged, and a
  curated set of distinctive-prefix rules (:data:`SECRET_RULES`). Tokens wrapped across
  lines are found too. There is no generic-entropy rule: its precision is too low to
  divert on (research §5.2).
- ``canary``: a ``canary_terms`` entry anywhere after normalisation, even inside a word.
- ``vocabulary``: a term of ``disclosure["vocabulary"]`` whose entity is not a person, as
  a whole word after normalisation.
- ``third_party``: the same for a person's term (entity ``person:<id>``), unless that person
  is a reader, one of ``disclosure["people"]``. A reader is never a third party.
- ``exfiltration``: inline, reference-style, HTML and autolinked URLs and images whose
  host is not in ``allowlist`` (a host or any of its subdomains); base64 runs of
  :data:`MIN_BASE64_RUN` characters and hex runs of :data:`MIN_HEX_RUN`; invisible
  characters, except where emoji, flags, bidirectional text or joining scripts need
  them; private, loopback, shared and link-local addresses; local paths and ``.env``
  files (the 0.1 path patterns).
- ``personal``: a ``personal_terms`` entry as a whole word after normalisation, and any
  email address (the 0.1 pattern).

**Normalisation** (:func:`normalise`, research §5.5) folds each character by
compatibility decomposition (NFKD, so full-width and other compatibility forms fold as
NFKC folds them) and case folding, drops combining marks (so an added accent does not
hide a term), maps a small set of Cyrillic, Greek and Latin letters that look like ASCII
letters to them, and removes invisible format characters and separators (whitespace,
hyphens and dashes, underscores and dots). An offset map sends every normalised character
back to the character of the message it came from, so a finding's positions cover the
text as written. Whole-word matching is judged against the message: a match must not
continue a word on either side, looking past invisible characters and marks.

**Fingerprints** are HMAC-SHA256 over the normalised value (the value itself when
normalisation leaves nothing, as for invisible characters), keyed by
:func:`fingerprint_key`: 32 random bytes in ``<state_dir>/fingerprint.key``, created on
first use with owner-only permissions. The key is read only when there is a finding.

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
the notice is reproduced beside :data:`SECRET_RULES`). Capturing and trailing-terminator
groups are dropped, since the scan adds its own word boundary, unescaped dots are
escaped and unbounded repetitions are bounded. No rule comes from a share-alike source.
The confusable letters are a hand-picked subset of those Unicode's confusables data
(UTS #39, Unicode License v3) maps to ASCII letters.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import re
import secrets
import unicodedata
from array import array
from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from hashlib import sha256
from itertools import chain as chain_iterables
from itertools import compress, repeat
from pathlib import Path
from typing import Optional, Union
from urllib.parse import unquote

#: The kinds a finding can have, in the order :data:`DFLT_DETECTORS` looks for them.
KINDS = ("secret", "canary", "vocabulary", "exfiltration", "personal", "third_party")
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

#: The shortest base64 run that is a finding: 75 bytes of data.
MIN_BASE64_RUN = 100
#: The shortest hex run that is a finding: longer than a SHA-512 digest, so digests and
#: commit hashes quoted in a message are not findings.
MIN_HEX_RUN = 129

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
    or check that matched; ``fingerprint`` is the keyed HMAC of the normalised value.
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

    def to_dict(self) -> dict:
        """The finding as a JSON-ready dict."""
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
        }


class FingerprintKeyError(Exception):
    """The fingerprint key file exists but cannot be used."""


# ---- normalisation ----

#: Invisible format characters (every character of general category ``Cf``) and the other
#: default-ignorable characters that render as nothing: the combining grapheme joiner,
#: Hangul fillers, Khmer inherent vowels, Mongolian variation selectors and the variation
#: selectors. ``test_detect`` checks that every ``Cf`` character is here.
_INVISIBLE_RANGES = (
    ("­", "­"),
    ("͏", "͏"),
    ("؀", "؅"),
    ("؜", "؜"),
    ("۝", "۝"),
    ("܏", "܏"),
    ("࢐", "࢑"),
    ("࣢", "࣢"),
    ("ᅟ", "ᅠ"),
    ("឴", "឵"),
    ("᠋", "᠏"),
    ("​", "‏"),
    ("‪", "‮"),
    ("⁠", "⁯"),
    ("ㅤ", "ㅤ"),
    ("︀", "️"),
    ("﻿", "﻿"),
    ("ﾠ", "ﾠ"),
    ("￹", "￻"),
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
#: Combining marks, dropped by normalisation. Spacing marks (``Mc``) carry a syllable's
#: sound in many scripts and are kept.
_MARK_CATEGORIES = frozenset({"Mn", "Me"})
#: Separators besides whitespace, dash punctuation and connector punctuation: the dots.
_DOTS = frozenset(".·․‧・．･")
_SEPARATOR_CATEGORIES = frozenset({"Pd", "Pc"})
#: Letters that render like an ASCII letter, as they are after case folding.
_CONFUSABLES = {
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "һ": "h", "н": "h", "і": "i", "ј": "j", "к": "k",
    "ӏ": "l", "м": "m", "о": "o", "р": "p", "ԛ": "q", "ѕ": "s", "т": "t", "ԝ": "w",
    "х": "x", "у": "y", "ү": "y", "ԁ": "d", "с": "c",
    # Greek
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ο": "o", "ρ": "p", "τ": "t",
    "χ": "x", "ζ": "z",
    # Latin and Armenian
    "ı": "i", "ȷ": "j", "ɑ": "a", "ɡ": "g", "ɩ": "i", "օ": "o", "ս": "u",
}  # fmt: skip


@lru_cache(maxsize=None)
def _is_invisible(char: str) -> bool:
    return _INVISIBLE_CHAR.match(char) is not None


@lru_cache(maxsize=None)
def _is_transparent(char: str) -> bool:
    """Whether whole-word matching looks past ``char``: an invisible character or a mark."""
    return _is_invisible(char) or unicodedata.category(char) in _MARK_CATEGORIES


def _vanishes(char: str) -> bool:
    return (
        _is_transparent(char)
        or char.isspace()
        or char in _DOTS
        or unicodedata.category(char) in _SEPARATOR_CATEGORIES
    )


@lru_cache(maxsize=None)
def _fold(char: str) -> str:
    """What one character contributes to the normalised text: nothing, or its folded form."""
    decomposed = unicodedata.normalize(
        "NFKD", unicodedata.normalize("NFKD", char).casefold()
    )
    return "".join(_CONFUSABLES.get(c, c) for c in decomposed if not _vanishes(c))


def _alnum_beside(source: str, index: int, step: int) -> bool:
    """Whether the first character from ``index`` in direction ``step``, past invisible
    characters and marks, is a letter or digit."""
    while 0 <= index < len(source) and _is_transparent(source[index]):
        index += step
    return 0 <= index < len(source) and source[index].isalnum()


@dataclass(frozen=True)
class Normalised:
    """A message folded for matching (``text``), and where each character came from.

    ``origins[i]`` is the index in ``source`` of the character ``text[i]`` came from.
    """

    source: str
    text: str
    origins: array

    def spans(self, term: str, *, whole_word: bool = True) -> Iterator[tuple[int, int]]:
        """Where the normalised ``term`` occurs, as ``(start, end)`` in ``source``.

        With ``whole_word``, a match must not continue a word of the message on either
        side. Occurrences of the same term do not overlap.
        """
        if not term:
            return
        index = self.text.find(term)
        while index >= 0:
            end = index + len(term)
            if not whole_word or (self._starts_word(index) and self._ends_word(end)):
                yield self._source_span(index, end)
                index = self.text.find(term, end)
            else:
                index = self.text.find(term, index + 1)

    def _starts_word(self, index: int) -> bool:
        if not self.text[index].isalnum():
            return True
        if index and self.origins[index - 1] == self.origins[index]:
            return not self.text[index - 1].isalnum()  # inside one folded character
        return not _alnum_beside(self.source, self.origins[index] - 1, -1)

    def _ends_word(self, end: int) -> bool:
        if not self.text[end - 1].isalnum():
            return True
        if end < len(self.text) and self.origins[end] == self.origins[end - 1]:
            return not self.text[end].isalnum()
        return not _alnum_beside(self.source, self.origins[end - 1] + 1, 1)

    def _source_span(self, index: int, end: int) -> tuple[int, int]:
        stop = self.origins[end - 1] + 1
        while stop < len(self.source) and (
            unicodedata.category(self.source[stop]) in _MARK_CATEGORIES
        ):
            stop += 1
        return self.origins[index], stop


def normalise(text: str) -> Normalised:
    """Fold ``text`` for matching, keeping where each folded character came from.

    >>> folded = normalise("Ｈｅ\\u200b-Ron!")
    >>> folded.text
    'heron!'
    >>> list(folded.spans("heron"))
    [(0, 7)]
    """
    folded, origins = (_fold_ascii if text.isascii() else _fold_characters)(text)
    return Normalised(source=text, text=folded, origins=origins)


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


# ---- fingerprint key ----


def _configured_state_dir() -> Path:
    """The state directory of the liaise config, or :data:`DFLT_STATE_DIR` without one."""
    from liaise.config import ConfigError, load_global_config

    try:
        return Path(load_global_config().state_dir).expanduser()
    except ConfigError:
        return DFLT_STATE_DIR.expanduser()


def fingerprint_key(
    state_dir: Union[str, os.PathLike, None] = None,
    *,
    key_file: str = DFLT_KEY_FILE,
    key_bytes: int = DFLT_KEY_BYTES,
) -> bytes:
    """The fingerprint key in ``<state_dir>/<key_file>``, created on first use.

    ``state_dir`` defaults to the liaise config's, else :data:`DFLT_STATE_DIR`. A new key
    is ``key_bytes`` random bytes in a file only its owner can read (the directory is
    created owner-only too). An existing file shorter than ``key_bytes`` raises
    :class:`FingerprintKeyError`: replacing it would silently change every fingerprint.
    """
    directory = (
        Path(state_dir).expanduser()
        if state_dir is not None
        else _configured_state_dir()
    )
    path = directory / key_file
    directory.mkdir(parents=True, exist_ok=True, mode=STATE_DIR_MODE)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, KEY_FILE_MODE)
    except FileExistsError:
        key = path.read_bytes()
        if len(key) < key_bytes:
            raise FingerprintKeyError(
                f"the fingerprint key in {path} holds {len(key)} bytes, fewer than "
                f"{key_bytes}. Remove the file to create a new key; fingerprints recorded "
                "with the old key will no longer match new ones."
            ) from None
        return key
    key = secrets.token_bytes(key_bytes)
    with os.fdopen(descriptor, "wb") as file:
        file.write(key)
        file.flush()
        os.fsync(file.fileno())
    return key


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
    def normalised(self) -> Normalised:
        """The message, folded for matching terms."""
        return normalise(self.text)

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
        return any(
            host == allowed or host.endswith("." + allowed)
            for allowed in self._allowed_hosts
        )

    @cached_property
    def _fingerprints(self) -> dict[str, str]:
        return {}

    def fingerprint(self, start: int, end: int) -> str:
        """The keyed fingerprint of ``text[start:end]``, normalised."""
        value = self.text[start:end]
        folded = "".join(map(_fold, value)) or value
        if folded not in self._fingerprints:
            message = folded.encode("utf-8", "surrogatepass")
            digest = hmac.new(self._key, message, sha256).hexdigest()
            self._fingerprints[folded] = digest
        return self._fingerprints[folded]

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
    ) -> Finding:
        """A :class:`Finding` for ``text[start:end]``, fingerprinted."""
        return Finding(
            kind=kind,
            start=start,
            end=end,
            entity=entity,
            label=label,
            sealed_from=sealed_from,
            rule=rule,
            severity=severity,
            fingerprint=self.fingerprint(start, end),
        )


#: ``(scan) -> findings``: one detector, or one check within a detector.
Detector = Callable[[Scan], Iterable[Finding]]


def _holds_a_literal(text: str, literals: tuple[str, ...]) -> bool:
    """Whether ``text`` holds one of ``literals``; true when there are none to require."""
    return not literals or any(literal in text for literal in literals)


def chain(*detectors: Detector, name: str = "chained") -> Detector:
    """One detector that yields the findings of ``detectors``, in order."""

    def chained(scan: Scan) -> Iterator[Finding]:
        for detector in detectors:
            yield from detector(scan)

    chained.__name__ = name
    return chained


# ---- secret ----


@dataclass(frozen=True)
class SecretRule:
    """A secret's shape: ``rule`` names it in findings, ``pattern`` is its expression.

    ``word_start``: a match must start a word. ``wrappable``: the rule is also looked for
    in the message with its line breaks removed, so a token wrapped across lines is found;
    its word boundary is then checked against the message itself, since removing a line
    break can glue a word to a token. A wrappable pattern must not fail after an unbounded
    repetition, or that scan, which has no word boundary to anchor it, turns quadratic.

    ``literals``: strings one of which every match contains. A text holding none of them
    is not scanned for the rule, which matters because a pattern that starts at a word
    boundary cannot use the regular-expression engine's fast search for a literal prefix.
    Empty: always scanned.
    """

    rule: str
    pattern: str
    literals: tuple[str, ...] = ()
    word_start: bool = True
    wrappable: bool = True

    def may_match(self, text: str) -> bool:
        """Whether ``text`` holds one of :attr:`literals`, or the rule has none."""
        return _holds_a_literal(text, self.literals)

    @cached_property
    def in_text(self) -> re.Pattern:
        """The pattern as it is looked for in the message."""
        return re.compile(rf"\b{self.pattern}" if self.word_start else self.pattern)

    @cached_property
    def in_unwrapped(self) -> re.Pattern:
        """The pattern as it is looked for in the message without its line breaks."""
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
#: follows each).
#: The literals every match of each of :data:`TOKEN_SHAPES` contains, in order.
TOKEN_LITERALS = (
    ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"),
    ("github_pat_",),
    ("sk-",),
    ("AKIA",),
    ("hf_",),
    ("xoxb-", "xoxa-", "xoxp-", "xoxr-", "xoxs-"),
)

#: The secret rules :func:`secret_detector` uses by default: the 0.1 token shapes and
#: private-key header, then distinctive-prefix rules adapted from gitleaks (the gitleaks id
#: follows each).
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
    SecretRule("npm-token", r"npm_[A-Za-z0-9]{36}", ("npm_",)),
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
        r"xapp-\d-[A-Za-z0-9]{1,64}-\d{1,16}-[A-Za-z0-9]{1,128}",
        ("xapp-",),
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
        r"glsa_[A-Za-z0-9]{32}_[A-Fa-f0-9]{8}",
        ("glsa_",),
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
    # jwt
    SecretRule(
        "jwt",
        r"ey[A-Za-z0-9]{17,4096}\.ey[A-Za-z0-9/\\_-]{17,4096}"
        r"\.(?:[A-Za-z0-9/\\_-]{10,4096}={0,2})?",
        (".ey",),
        wrappable=False,
    ),
)

_LINE_BREAK = re.compile(r"[\r\n]")
_WORD_CHAR = re.compile(r"\w")


@dataclass(frozen=True)
class _Unwrapped:
    """A message without its line breaks, and where each removal happened in it."""

    text: str
    removed_at: tuple[int, ...]

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        def source(index: int) -> int:
            return index + bisect_right(self.removed_at, index)

        return source(start), source(end - 1) + 1


def _without_line_breaks(text: str) -> _Unwrapped:
    breaks = (match.start() for match in _LINE_BREAK.finditer(text))
    removed_at = tuple(position - count for count, position in enumerate(breaks))
    return _Unwrapped(_LINE_BREAK.sub("", text), removed_at)


def secret_detector(rules: Iterable[SecretRule] = SECRET_RULES) -> Detector:
    """A detector of ``rules``: a ``secret`` finding for each match, severity 5.

    A token found both on one line and with the line breaks removed is one finding, as
    long as the longer of the two matches.
    """
    rules = tuple(rules)

    def detect_secrets(scan: Scan) -> Iterator[Finding]:
        text = scan.text
        ends: dict[tuple[str, int], int] = {}

        def keep(rule: str, start: int, end: int) -> None:
            ends[rule, start] = max(end, ends.get((rule, start), end))

        for rule in rules:
            if not rule.may_match(text):
                continue
            for match in rule.in_text.finditer(text):
                keep(rule.rule, match.start(), match.end())
        wrappable = [rule for rule in rules if rule.wrappable]
        if wrappable and _LINE_BREAK.search(text):
            unwrapped = _without_line_breaks(text)
            for rule in wrappable:
                if not rule.may_match(unwrapped.text):
                    continue
                for match in rule.in_unwrapped.finditer(unwrapped.text):
                    start, end = unwrapped.source_span(match.start(), match.end())
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
        for start, end in scan.normalised.spans(normalise(term).text, whole_word=False):
            yield scan.finding(
                "canary", start, end, rule="canary-term", severity=SEVERITY_CANARY
            )


def _vocabulary_entries(scan: Scan) -> Iterator[tuple[str, Mapping]]:
    """Each vocabulary entry with a term, and its term normalised."""
    for index, entry in enumerate(scan.disclosure.get("vocabulary") or ()):
        if not isinstance(entry, Mapping):
            raise TypeError(
                f"disclosure vocabulary entry {index} is a {type(entry).__name__}, "
                "not a mapping with a 'term'"
            )
        term = entry.get("term")
        folded = normalise(term).text if isinstance(term, str) else ""
        if folded:
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
        for start, end in scan.normalised.spans(folded):
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


# ---- exfiltration ----

_HOST_END = r"[^\s/\\?#<>\"'`()\[\]{}|^]"
#: A URL with a scheme and an authority. The authority ends where a browser ends it: a
#: backslash counts as a slash (WHATWG URL), so ``https://a.example\@b.example`` is a.
_SCHEME_URL = re.compile(
    rf"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{{0,31}}://(?P<authority>{_HOST_END}*)"
)
#: A scheme-relative URL (``//host/path``) where it is a destination: an inline link or
#: image, a reference definition, or an HTML attribute.
_RELATIVE_DESTINATION = re.compile(
    r"(?:\]\([ \t]{0,8}<?"
    r"|^[ \t]{0,3}\[[^\[\]\n]{1,999}\]:[ \t]{0,8}<?"
    r"|\b(?:src|srcset|href|poster|action|background)[ \t]{0,8}=[ \t]{0,8}[\"']?)"
    rf"(?P<url>//(?P<authority>{_HOST_END}+))",
    re.IGNORECASE | re.MULTILINE,
)
#: A ``www.`` host without a scheme, which GitHub and many renderers autolink.
_BARE_WWW = re.compile(
    r"(?<![\w.@/:-])(?P<authority>www\.[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,126})"
)
_URL_TAIL = re.compile(r"[^\s<>\"'`]*")
_URL_TRAILING_PUNCTUATION = ".,:;!?*_~'\")]}"
#: Where an image's URL starts: after ``![alt](``, or an ``<img>``'s ``src``.
_INLINE_IMAGE_DESTINATION = re.compile(r"!\[[^\[\]\n]{0,999}\]\([ \t]{0,8}<?")
_HTML_IMAGE_SOURCE = re.compile(
    r"<img\b[^<>]{0,999}?\bsrc(?:set)?[ \t]{0,8}=[ \t]{0,8}[\"']?", re.IGNORECASE
)
#: A reference-style image (``![alt][label]``, ``![label][]``, ``![label]``), and a
#: reference definition (``[label]: url``).
_IMAGE_REFERENCE = re.compile(
    r"!\[(?P<text>[^\[\]\n]{0,999})\](?:\[(?P<label>[^\[\]\n]{0,999})\])?"
)
_REFERENCE_DEFINITION = re.compile(
    r"^[ \t]{0,3}\[(?P<label>[^\[\]\n]{1,999})\]:[ \t]{0,8}<?", re.MULTILINE
)


def _reference_label(label: str) -> str:
    return " ".join(label.split()).casefold()


def _host(authority: str) -> str:
    """The host an authority names: after its last ``@``, without port or brackets."""
    host = authority.rpartition("@")[2]
    if host.startswith("["):
        host = host[1:].partition("]")[0]
    else:
        host = host.partition(":")[0]
    return unquote(host).strip().rstrip(".").lower()


def _image_url_starts(text: str) -> set[int]:
    """Where each URL that an image loads starts: inline, HTML or by reference."""
    starts = {match.end() for match in _INLINE_IMAGE_DESTINATION.finditer(text)}
    starts.update(match.end() for match in _HTML_IMAGE_SOURCE.finditer(text))
    labels = {
        _reference_label(match.group("label") or match.group("text"))
        for match in _IMAGE_REFERENCE.finditer(text)
    }
    starts.update(
        match.end()
        for match in _REFERENCE_DEFINITION.finditer(text)
        if _reference_label(match.group("label")) in labels
    )
    return starts


def scan_links(scan: Scan) -> Iterator[Finding]:
    """An ``exfiltration`` finding for each URL whose host is not allowlisted.

    Its rule is ``image-host`` when an image loads it (inline, reference-style or HTML),
    else ``link-host``. The finding spans the URL; a URL's path stops where the next URL
    starts, so a run of URLs is scanned once.
    """
    text = scan.text
    candidates = []  # (start, authority end, authority); each kind only if it can occur
    if "://" in text:
        candidates += [(m.start(), m.end("authority"), m.group("authority")) for m in _SCHEME_URL.finditer(text)]  # fmt: skip
    if "//" in text:
        candidates += [(m.start("url"), m.end("authority"), m.group("authority")) for m in _RELATIVE_DESTINATION.finditer(text)]  # fmt: skip
    if "www." in text:
        candidates += [(m.start(), m.end("authority"), m.group("authority")) for m in _BARE_WWW.finditer(text)]  # fmt: skip
    if not candidates:
        return
    candidates.sort()
    image_starts = _image_url_starts(text)
    for index, (start, authority_end, authority) in enumerate(candidates):
        host = _host(authority)
        if not host or scan.allows(host):
            continue
        limit = candidates[index + 1][0] if index + 1 < len(candidates) else len(text)
        end = _URL_TAIL.match(text, authority_end, max(limit, authority_end)).end()
        while end > authority_end and text[end - 1] in _URL_TRAILING_PUNCTUATION:
            end -= 1
        rule = "image-host" if start in image_starts else "link-host"
        yield scan.finding(
            "exfiltration", start, end, rule=rule, severity=SEVERITY_EXFILTRATION
        )


_BASE64_RUN = re.compile(
    rf"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{{{MIN_BASE64_RUN},}}={{0,2}}"
)
_HEX_RUN = re.compile(rf"(?<![0-9A-Fa-f])[0-9A-Fa-f]{{{MIN_HEX_RUN},}}(?![0-9A-Fa-f])")
_DIGIT = re.compile(r"[0-9]")
_UPPER = re.compile(r"[A-Z]")
_LOWER = re.compile(r"[a-z]")
_HEX_LETTER = re.compile(r"[A-Fa-f]")


def scan_base64_runs(scan: Scan) -> Iterator[Finding]:
    """An ``exfiltration`` finding for each base64 or base64url run of at least
    :data:`MIN_BASE64_RUN` characters that mixes digits, capitals and small letters, which
    encoded data does and a long word or path rarely does."""
    for match in _BASE64_RUN.finditer(scan.text):
        run = match.group()
        if _DIGIT.search(run) and _UPPER.search(run) and _LOWER.search(run):
            yield scan.finding(
                "exfiltration",
                match.start(),
                match.end(),
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


_BOM = "﻿"
_PRESENTATION_SELECTORS = frozenset({"︎", "️"})
_EMOJI_JOINING = frozenset({"‍", "️"})
#: The longest run of joiners and selectors inside an emoji sequence (VS16, then ZWJ).
_MAX_EMOJI_JOINING_RUN = 2
_BLACK_FLAG = "\U0001f3f4"
_CANCEL_TAG = "\U000e007f"
_FIRST_TAG, _LAST_TAG = "\U000e0020", "\U000e007e"
#: The most tag characters a subdivision flag holds before its cancel tag.
_MAX_FLAG_TAGS = 6
_JOINERS = frozenset({"‌", "‍"})
_BIDI_MARKS = frozenset({"‎", "‏"})
_PICTOGRAPHIC_CATEGORIES = frozenset({"So", "Sk"})
_RIGHT_TO_LEFT = frozenset({"R", "AL"})


def _needed_invisible(text: str, start: int, end: int) -> bool:
    """Whether the invisible run ``text[start:end]`` is one that text needs to render: a
    byte-order mark opening the text, a single emoji presentation selector, the joiners
    of an emoji sequence, a subdivision flag's tags, a joiner between letters of a
    joining script, or a direction mark beside right-to-left text."""
    run = text[start:end]
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""

    def pictographic(char: str) -> bool:
        return bool(char) and unicodedata.category(char) in _PICTOGRAPHIC_CATEGORIES

    def right_to_left(char: str) -> bool:
        return bool(char) and unicodedata.bidirectional(char) in _RIGHT_TO_LEFT

    def non_ascii_letter(char: str) -> bool:
        return bool(char) and char.isalpha() and not char.isascii()

    if run == _BOM and start == 0:
        return True
    if run in _PRESENTATION_SELECTORS:
        return True
    if (
        len(run) <= _MAX_EMOJI_JOINING_RUN
        and set(run) <= _EMOJI_JOINING
        and pictographic(before)
        and ("‍" not in run or pictographic(after))
    ):
        return True
    if (
        before == _BLACK_FLAG
        and 1 < len(run) <= _MAX_FLAG_TAGS + 1
        and run[-1] == _CANCEL_TAG
        and all(_FIRST_TAG <= char <= _LAST_TAG for char in run[:-1])
    ):
        return True
    if run in _JOINERS and non_ascii_letter(before) and non_ascii_letter(after):
        return True
    return run in _BIDI_MARKS and (right_to_left(before) or right_to_left(after))


def scan_invisible_characters(scan: Scan) -> Iterator[Finding]:
    """An ``exfiltration`` finding for each run of invisible characters the text does not
    need to render (see :func:`_needed_invisible`): zero-width spaces, direction
    overrides, tag characters, variation selectors used to carry data."""
    for match in _INVISIBLE_RUN.finditer(scan.text):
        if not _needed_invisible(scan.text, match.start(), match.end()):
            yield scan.finding(
                "exfiltration",
                match.start(),
                match.end(),
                rule="invisible-character",
                severity=SEVERITY_EXFILTRATION,
            )


_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")
_IPV6 = re.compile(r"(?<![\w:.])[0-9A-Fa-f:]{2,39}(?![\w:])")
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


def _internal(candidate: str) -> bool:
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return any(address in network for network in INTERNAL_NETWORKS)


def scan_private_addresses(scan: Scan) -> Iterator[Finding]:
    """An ``exfiltration`` finding for each IPv4 or IPv6 address in
    :data:`INTERNAL_NETWORKS`."""
    matches = [*_IPV4.finditer(scan.text)]
    matches += [m for m in _IPV6.finditer(scan.text) if m.group().count(":") >= 2]
    for match in matches:
        if _internal(match.group()):
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
        for start, end in scan.normalised.spans(normalise(term).text):
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
    returns it); by default :func:`fingerprint_key`, read only when there is a finding.

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
