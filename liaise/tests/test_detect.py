"""Tests for liaise.detect: the six detectors, normalisation and fingerprints (slice L1).

Every token, address and path sample is built at test time by concatenation, so neither
the no-personal-data guard nor a secret-scanning push protection reads one in this file.

The mutation checks: each secret rule, path rule, exfiltration check, personal check and
detector is removed in turn, and its sample must then go unfound; each secret rule also
has a near miss one character short of its bound, which a loosened bound would find.
Spans, trailing punctuation, marks, word checks and length bounds each have a test that
pins them.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import stat
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

import pytest

from liaise import detect as detect_module
from liaise.detect import (
    DFLT_DETECTORS,
    DFLT_KEY_BYTES,
    DFLT_KEY_FILE,
    EXFILTRATION_SCANNERS,
    KINDS,
    LOCAL_PATH_RULES,
    MIN_BASE64_RUN,
    PERSONAL_SCANNERS,
    SECRET_RULES,
    SEVERITY_ABOVE_CLEARANCE,
    SEVERITY_CANARY,
    SEVERITY_EXFILTRATION,
    SEVERITY_NARROW_AUDIENCE,
    SEVERITY_SEALED,
    SEVERITY_SECRET,
    SEVERITY_WIDE_AUDIENCE,
    Finding,
    FingerprintKeyError,
    chain,
    detect,
    fingerprint_key,
    fold_term,
    local_path_scanner,
    normalise,
    render,
    secret_detector,
    EMAIL_PATTERN,
    ENV_FILE_PATTERN,
    LOCAL_PATH_PATTERNS,
    PRIVATE_KEY_PATTERN,
    TOKEN_SHAPES,
    key_source,
    link_urls,
    visible,
)
from liaise.tests.test_gate import (
    LEAK_TERMS,
    LEAKS,
    LONG_WORD,
    LONG_WORD_SCAN_BOUND_S,
)

KEY = b"k" * 32
OTHER_KEY = b"o" * 32
ALLOWLIST = ("example.org",)
ADDRESS = "ada" + "@" + "example.org"
CANARY = "zq-canary-7731"
#: The disclosure of the worked case (discussion §1): Ada (p-01) and Bram (p-02) read the
#: channel, the audience is public, Heron (p-17) is amber and sealed from Bram, Cy (p-03)
#: and Ona (p-04) are people who are not readers. The ids share no text with the terms,
#: so the check that no finding holds its matched text can be strict.
HERON = "project:p-17"
DISCLOSURE = {
    "people": {"p-01": {"tier": "open", "clearance": "amber"}, "p-02": {"tier": "reviewed", "clearance": "clear"}},
    "least_clearance": "clear",
    "vocabulary": [
        {"term": "Heron", "entity": HERON, "label": "amber", "sealed_from": ["p-02"]},
        {"term": "the bird project", "entity": HERON, "label": "amber", "sealed_from": ["p-02"]},
        {"term": "Cy", "entity": "person:p-03", "label": "green"},
        {"term": "Ona", "entity": "person:p-04", "label": "green"},
        {"term": "Ada", "entity": "person:p-01", "label": "green"},
    ],
}


def _detect(text, **options):
    options = {"disclosure": DISCLOSURE, "allowlist": ALLOWLIST, "key": KEY, **options}
    return detect(text, **options)


def _of_kind(kind, text, **options):
    return [finding for finding in _detect(text, **options) if finding.kind == kind]


def _rules(kind, text, **options):
    return [f.rule for f in _of_kind(kind, text, **options)]


# ---- the 0.1 leak-scan table ----

#: What each 0.1 kind is now: (kind, rule), the rule None where the 0.1 kind had several.
_LEAK_KINDS = {
    "local path": ("exfiltration", "local-path"),
    "env file": ("exfiltration", "env-file"),
    "email": ("personal", "email-address"),
    "private key": ("secret", "private-key"),
    "token": ("secret", None),
    "leak term": ("vocabulary", "term"),
}
LEAK_DISCLOSURE = {"vocabulary": [{"term": term, "entity": "project:board", "label": "amber"} for term in LEAK_TERMS]}


#: The 0.1 leak scan's patterns, as (kind, pattern): the oracle the detectors are held to.
_LEAK_PATTERNS_0_1 = (
    *(("local path", pattern) for pattern in LOCAL_PATH_PATTERNS),
    ("env file", ENV_FILE_PATTERN),
    ("email", re.compile(EMAIL_PATTERN)),
    ("private key", re.compile(PRIVATE_KEY_PATTERN)),
    *(("token", re.compile(rf"\b{shape}")) for shape in TOKEN_SHAPES),
)


def _leak_scan_hits(text):
    """What the 0.1 leak scan found in ``text``, as (kind, start).

    The leak scan is gone from the gate (liaise ADR 0002); its search is kept here, over the
    patterns liaise.detect still owns, so every leak it found is still found where it was.
    """
    kept = [index for index, char in enumerate(text) if char not in "\r\n"]
    unwrapped = "".join(text[index] for index in kept)
    found = [(kind, match.start()) for kind, pattern in _LEAK_PATTERNS_0_1 for match in pattern.finditer(text)]
    for shape in TOKEN_SHAPES:  # a token wrapped across lines, its word boundary judged on the text
        for match in re.finditer(shape, unwrapped):
            start = kept[match.start()]
            if start == 0 or not re.fullmatch(r"\w", text[start - 1]):
                found.append(("token", start))
    found += [
        ("leak term", match.start())
        for term in LEAK_TERMS
        for match in re.finditer(rf"(?<!\w){re.escape(term)}(?!\w)", text, flags=re.IGNORECASE)
    ]
    return list(dict.fromkeys(found))


@pytest.mark.parametrize("kind, text", list(LEAKS.values()), ids=list(LEAKS))
def test_every_0_1_leak_is_found_at_the_same_position(kind, text):
    hits = _leak_scan_hits(text)
    assert hits and {hit_kind for hit_kind, _ in hits} == {kind}
    findings = detect(text, disclosure=LEAK_DISCLOSURE, key=KEY)
    for hit_kind, start in hits:
        new_kind, rule = _LEAK_KINDS[hit_kind]
        assert any(
            f.kind == new_kind and f.start == start and rule in (None, f.rule) for f in findings
        ), (hit_kind, start, findings)


# ---- what the operator reads before releasing a message (L3) ----


def test_visible_spells_out_invisible_and_control_characters_and_keeps_line_breaks_and_tabs():
    text = "Fixed" + "​" + ".\tSee\n" + "‮" + "txt.exe" + "\x1b[2J" + "\x85"
    assert visible(text) == "Fixed<U+200B>.\tSee\n<U+202E>txt.exe<U+001B>[2J<U+0085>"
    assert visible("plain text") == "plain text"


def test_link_urls_lists_every_destination_in_full_once_in_order():
    text = (
        "Read [the changelog](https://example.org/notes) and ![chart](https://img.example.com/c.png). "
        "Also <a href='https://example.net/a'>here</a>, https://example.org/notes again, and //example.io/x."
    )
    assert link_urls(text) == (
        "https://example.org/notes",
        "https://img.example.com/c.png",
        "https://example.net/a",
        "//example.io",  # a bare authority is read to the host, as scan_links reads it
    )
    assert link_urls("No links here.") == ()


def test_a_key_that_may_not_be_created_is_used_once_and_writes_nothing(tmp_path):
    state = tmp_path / "state"
    once, again = fingerprint_key(state, create=False), fingerprint_key(state, create=False)
    assert len(once) == DFLT_KEY_BYTES and once != again and not state.exists()
    kept = fingerprint_key(state)
    assert fingerprint_key(state, create=False) == kept  # an existing key is read, never replaced


def test_a_key_source_answers_one_key_however_often_it_is_asked(tmp_path):
    """One command may judge a message twice, and the two judgements must fingerprint alike:
    a key that is not written down (a dry run) would otherwise be a new key each time."""
    state = tmp_path / "state"
    once = key_source(state, create=False)
    assert once() == once() and not state.exists()
    assert key_source(state, create=False)() != once()  # another command, another key

    kept = key_source(state)
    assert kept() == kept() == fingerprint_key(state)


def test_a_finding_names_its_part_only_when_it_has_one():
    finding = Finding(kind="secret", start=0, end=4, rule="github-token", severity=5, fingerprint="f")
    assert "part" not in finding.to_dict()
    assert Finding(**{**finding.__dict__, "part": "title"}).to_dict()["part"] == "title"


# ---- vocabulary and normalisation ----

HERON_FORMS = {
    "plain": "Heron",
    "full-width": "\uff28\uff45\uff52\uff4f\uff4e",
    "zero-width-space": "He\u200bron",
    "hyphenated": "He-ron",
    "mixed-case": "hErOn",
    "letter-spaced": "H e r o n",
    "soft-hyphen": "Her\u00adon",
    "cyrillic-e": "H\u0435ron",
    "greek-capitals": "\u0397ERO\u039d",
    "small-capitals": "\u029c\u1d07\u0280\u1d0f\u0274",
    "minus-sign": "He\u2212ron",
    "accented": "H\u00e9r\u00f6n",
    "decomposed-accent": "He\u0301ron",
    "emphasis": "He*ro*n",
    "html-tag": "He<b></b>ron",
    "html-comment": "He<!-- -->ron",
    "character-reference": "H&#101;ron",
    "zero-padded-reference": "H&#00000000101;ron",
}


@pytest.mark.parametrize("form", list(HERON_FORMS.values()), ids=list(HERON_FORMS))
def test_heron_is_found_in_every_form_at_its_place_in_the_text(form):
    text = "Update: " + form + " slips to October."
    (finding,) = _of_kind("vocabulary", text)
    assert text[finding.start : finding.end] == form
    assert (finding.entity, finding.label, finding.sealed_from, finding.rule) == (HERON, "amber", ("p-02",), "term")


def test_a_term_of_several_words_is_found_across_separators():
    text = "About The Bird-Project: nothing yet."
    (finding,) = _of_kind("vocabulary", text)
    assert text[finding.start : finding.end] == "The Bird-Project"


def test_a_match_covers_the_marks_after_it():
    text = "Heron\u0301 slips."
    (finding,) = _of_kind("vocabulary", text)
    assert text[finding.start : finding.end] == "Heron\u0301"


@pytest.mark.parametrize(
    "text",
    ["Herons nest here.", "The heronry is quiet.", "Herondale road.", "xHeron", "She Ron called.", "a\u200bheron", "He ron"],
)
def test_a_term_is_matched_as_a_whole_word_only(text):
    assert _of_kind("vocabulary", text) == []


@pytest.mark.parametrize(
    "text",
    ["We move on a new plan.", "Moving on, a new plan.", "Go on and on a bit.", "<td>on</td><td>a</td>", "on<br>a plan"],
)
def test_a_match_does_not_span_a_word_break_the_term_does_not_have(text):
    assert _of_kind("third_party", text) == []


@pytest.mark.parametrize(
    "form",
    [
        "He<!-- < -->ron",
        "He<!--" + "x" * 5000 + "-->ron",
        'He<b title="' + "x" * 1100 + '">ron',
        "He<?x?>ron",
        "He-\nron",
        "He-\r\n  ron",
    ],
)
def test_heron_across_long_or_odd_markup_or_a_hyphenated_wrap_is_found(form):
    text = "Update: " + form + " slips."
    (finding,) = _of_kind("vocabulary", text)
    assert text[finding.start : finding.end] == form


@pytest.mark.parametrize("text", ["Heron's date", "(Heron)", "Heron.", "\u00abHeron\u00bb", "heron_v2"])
def test_a_term_beside_punctuation_is_a_whole_word(text):
    assert len(_of_kind("vocabulary", text)) == 1


def test_severity_follows_the_seal_then_the_label_then_the_audience():
    text = "Heron slips."
    assert _of_kind("vocabulary", text)[0].severity == SEVERITY_SEALED
    readers = {**DISCLOSURE, "people": {"p-01": {}}}
    assert _of_kind("vocabulary", text, disclosure=readers)[0].severity == SEVERITY_ABOVE_CLEARANCE
    cleared = {**readers, "least_clearance": "amber"}
    assert _of_kind("vocabulary", text, disclosure=cleared)[0].severity == SEVERITY_NARROW_AUDIENCE
    wide = {**readers, "vocabulary": [{"term": "Heron", "entity": HERON, "label": "clear"}]}
    assert _of_kind("vocabulary", text, disclosure=wide)[0].severity == SEVERITY_WIDE_AUDIENCE


def test_an_unknown_label_counts_as_the_most_restrictive():
    disclosure = {"least_clearance": "red", "vocabulary": [{"term": "Heron", "entity": HERON, "label": "purple"}]}
    assert _of_kind("vocabulary", "Heron", disclosure=disclosure)[0].severity == SEVERITY_ABOVE_CLEARANCE


@pytest.mark.parametrize("text", ["Ｈé\u200b-Ron!", "ﬁle ß İ", "a\u0301\u0302b", "plain ascii-text_here.", "\u0397\u029c"])
def test_every_normalised_character_comes_from_its_origin(text):
    folded = normalise(text)
    assert len(folded.origins) == len(folded.text)
    for index, char in enumerate(folded.text):
        assert char in normalise(text[folded.start_of(index) : folded.end_of(index)]).text


def test_ascii_text_folds_exactly_as_any_text_does():
    text = "".join(map(chr, range(128))) * 2 + "Mixed-Case_text.with SEPARATORS"
    assert detect_module._fold_ascii(text) == detect_module._fold_characters(text)


def test_rendering_removes_markup_and_maps_back_to_the_source():
    text = "a<i>b</i>&amp;%41\\*"
    view = render(text)
    assert view.text == "ab&A*"
    assert [text[view.start_of(i) : view.end_of(i)] for i in range(len(view.text))] == ["a", "b", "&amp;", "%41", "*"]
    assert render("no markup here") is None


def test_every_format_character_counts_as_invisible():
    missing = [
        hex(code)
        for code in range(sys.maxunicode + 1)
        if unicodedata.category(chr(code)) == "Cf" and not detect_module._is_invisible(chr(code))
    ]
    assert missing == []


# ---- third party, personal, canary ----


def test_a_person_who_is_not_a_reader_is_a_third_party():
    text = "Cy and Ada agreed."
    (finding,) = _of_kind("third_party", text)
    assert text[finding.start : finding.end] == "Cy"
    assert (finding.entity, finding.rule) == ("person:p-03", "person-name")
    assert _of_kind("vocabulary", text) == []


def test_a_reader_is_never_a_third_party():
    assert _of_kind("third_party", "Thanks, Ada.") == []


@pytest.mark.parametrize("term", [ADDRESS, "@ada-l", "~" + "/notes/ada"])
def test_a_personal_term_is_found_as_personal(term):
    text = "Reach me at " + term + " today."
    (finding,) = [f for f in _of_kind("personal", text, personal_terms=[term]) if f.rule == "personal-term"]
    assert text[finding.start : finding.end] == term
    assert finding.severity == SEVERITY_WIDE_AUDIENCE


def test_any_email_address_is_personal():
    text = "Write to " + ADDRESS + "."
    (finding,) = _of_kind("personal", text)
    assert (finding.rule, text[finding.start : finding.end]) == ("email-address", ADDRESS)


@pytest.mark.parametrize(
    "form",
    [CANARY, "ZQ-Canary-7731", "zq\u200bcanary7731", "prefix" + "zqcanary7731" + "suffix", "zq%2Dcanary%2D7731"],
)
def test_a_canary_term_is_found_anywhere(form):
    text = "Here: https://example.org/search?q=" + form + " done."
    (finding,) = _of_kind("canary", text, canary_terms=[CANARY])
    assert (finding.rule, finding.severity) == ("canary-term", SEVERITY_CANARY)


# ---- exfiltration: links ----

COLLECTOR = "collector.example.net"


def test_a_reference_style_image_to_a_host_outside_the_allowlist():
    url = "https://" + COLLECTOR + "/p.png?d=1"
    text = "Status: ![status][badge]\n\n[badge]: " + url + "\n"
    (finding,) = _of_kind("exfiltration", text)
    assert (finding.rule, finding.severity) == ("image-host", SEVERITY_EXFILTRATION)
    assert text[finding.start : finding.end] == url


@pytest.mark.parametrize(
    "text, rule",
    [
        ("See [the log](https://" + COLLECTOR + "/x).", "link-host"),
        ("![chart](https://" + COLLECTOR + "/c.png)", "image-host"),
        ("<https://" + COLLECTOR + "/a>", "link-host"),
        ('<img alt="x" src="https://' + COLLECTOR + '/i.png">', "image-host"),
        ("![c](//" + COLLECTOR + "/c.png)", "image-host"),
        ("![s][ref]\n[REF]: //" + COLLECTOR + "/s.png", "image-host"),
        ("![s][b]\n[b]:\n//" + COLLECTOR + "/s.png", "image-host"),
        ("Go to www." + COLLECTOR + " now", "link-host"),
        ("[a](https://example.org@" + COLLECTOR + "/)", "link-host"),
        ("[a](https://" + COLLECTOR + "\\@example.org/)", "link-host"),
        ("[a](https://example.org\\@" + COLLECTOR + "/)", "link-host"),
        ("[a](https://example.org." + COLLECTOR + "/)", "link-host"),
        ("[a](https://example.org%2E" + COLLECTOR + "/)", "link-host"),
        ("![x](https&#58;//" + COLLECTOR + "/p.png)", "image-host"),
        ('<img src="https:///' + COLLECTOR + '/p.png">', "image-host"),
        ('<img src="https:\\\\' + COLLECTOR + '/p.png">', "image-host"),
        ('<img src="https://\n' + COLLECTOR + '/p.png">', "image-host"),
        ('<img src="https://example.org@\n' + COLLECTOR + '/p.png">', "image-host"),
        ('<img src="https:/\n/' + COLLECTOR + '/p.png">', "image-host"),
        ('<img srcset="https://example.org/a.png 1x, //' + COLLECTOR + '/b.png 2x">', "image-host"),
        ('<img alt=">" src="https:' + COLLECTOR + '/p.png">', "image-host"),
    ],
)
def test_links_and_images_to_hosts_outside_the_allowlist(text, rule):
    assert _rules("exfiltration", text) == [rule]


@pytest.mark.parametrize(
    "text",
    [
        "See [the docs](https://example.org/docs).",
        "![logo](https://cdn.example.org/logo.png)",
        "Visit www.example.org today, or HTTPS://EXAMPLE.ORG./x.",
        "A relative [link](docs/setup.md), an ![icon](img/i.png) and a double slash a//b.",
        "In code: `std::vector` at 12:30:45, and a [mail](mailto:x) link.",
        '<img src="./img/local.png"> and <a href="#top">top</a>',
        "```\ncurl -d data=//x -d src=build\n```",
    ],
)
def test_allowlisted_hosts_and_relative_links_are_not_findings(text):
    assert _of_kind("exfiltration", text) == []


@pytest.mark.parametrize(
    "text",
    [
        "![x](//example.org(x)@" + COLLECTOR + "/p.png)",
        "![x](//example.org\\(@" + COLLECTOR + "/p.png)",
        "> ![s][b]\n>\n> [b]: //" + COLLECTOR + "/p.png",
        "![s][b\\]]\n\n[b\\]]: //" + COLLECTOR + "/p.png",
        "![s][b c]\n\n[b\nc]: //" + COLLECTOR + "/p.png",
        "- a\n\n  - b\n\n    ![s][q]\n\n    [q]: //" + COLLECTOR + "/p.png",
    ],
)
def test_a_host_is_found_whatever_markdown_construct_holds_it(text):
    assert _of_kind("exfiltration", text)


@pytest.mark.parametrize(
    "text, rule",
    [
        ('<video poster="https:' + COLLECTOR + '/p.png">', "image-host"),
        ('<table background="https:' + COLLECTOR + '/b.png">', "image-host"),
        ('<object data="https:' + COLLECTOR + '/o">', "image-host"),
        ('<img lowsrc="https:' + COLLECTOR + '/l.png">', "image-host"),
        ('<img dynsrc="https:' + COLLECTOR + '/d.avi">', "image-host"),
        ('<a href="https:' + COLLECTOR + '/x">', "link-host"),
        ('<form action="https:' + COLLECTOR + '/x">', "link-host"),
        ('<button formaction="https:' + COLLECTOR + '/x">', "link-host"),
        ('<img src="ws:' + COLLECTOR + '/p.png">', "image-host"),
        ('<img src="wss:' + COLLECTOR + '/p.png">', "image-host"),
        ('<img src="ftp:' + COLLECTOR + '/p.png">', "image-host"),
    ],
)
def test_every_url_attribute_and_special_scheme_is_read(text, rule):
    """Special schemes with no slashes, which only the attribute parser reads."""
    assert _rules("exfiltration", text) == [rule]


def test_a_scheme_that_is_not_special_names_no_host_without_slashes():
    assert _of_kind("exfiltration", '<img src="gopher:' + COLLECTOR + '/p.png">') == []


def test_tabs_inside_a_url_are_ignored():
    assert _of_kind("exfiltration", '<img src="https://exam\tple.org/p.png">') == []
    text = '<img src="https://collec\ttor.example.net/p.png">'
    assert _rules("exfiltration", text) == ["image-host"]


@pytest.mark.parametrize("text", ["[link](https://) TODO", '`<a href="https://">`', "See https:// for it."])
def test_a_url_with_nothing_after_its_slashes_is_not_a_finding(text):
    assert _of_kind("exfiltration", text) == []


def test_a_url_finding_stops_before_trailing_punctuation():
    url = "https://" + COLLECTOR + "/x"
    text = "It went to " + url + "."
    (finding,) = _of_kind("exfiltration", text)
    assert text[finding.start : finding.end] == url


# ---- exfiltration: encoded runs, invisible characters, addresses, paths ----

BASE64_RUN = base64.b64encode(bytes(range(150))).decode()  # 200 characters
HEX_RUN = ("0123456789abcdef" * 9)[:129]


def test_a_long_base64_run_is_exfiltration():
    text = "data " + BASE64_RUN + " end"
    (finding,) = _of_kind("exfiltration", text)
    assert (finding.rule, text[finding.start : finding.end]) == ("base64-run", BASE64_RUN)


def test_a_base64_run_at_the_bound_is_exfiltration():
    assert _rules("exfiltration", "x " + BASE64_RUN[:MIN_BASE64_RUN] + " y") == ["base64-run"]


WRAPPED = base64.b64encode(bytes(range(256)) * 2).decode()


@pytest.mark.parametrize("width, prefix", [(40, ""), (56, ""), (64, ""), (76, ""), (76, "    "), (76, "> ")])
def test_wrapped_base64_is_exfiltration(width, prefix):
    lines = [WRAPPED[i : i + width] for i in range(0, len(WRAPPED), width)]
    block = "\n".join(prefix + line for line in lines)
    text = "Attached:\n" + block + "\nBest"
    (finding,) = _of_kind("exfiltration", text)
    assert (finding.rule, text[finding.start : finding.end]) == ("base64-run", block[len(prefix) :])


def test_base64_wrapped_narrower_than_the_bound_is_no_block():
    block = "\n".join(WRAPPED[i : i + 39] for i in range(0, len(WRAPPED), 39))
    assert _of_kind("exfiltration", block) == []


@pytest.mark.parametrize(
    "run",
    [BASE64_RUN[: MIN_BASE64_RUN - 1], "a" * 300, "0123456789abcdef" * 4, "0123456789abcdef" * 8, "Z" * 150 + "1"],
)
def test_short_or_unmixed_runs_are_not_exfiltration(run):
    """A base64 run one short of the bound, a long word, a SHA-256 and a SHA-512 digest,
    capitals and a digit without a small letter."""
    assert _of_kind("exfiltration", "x " + run + " y") == []


def test_commit_hashes_one_per_line_are_not_exfiltration():
    text = "\n".join(("0123456789abcdef" * 3)[:40] for _ in range(6))
    assert _of_kind("exfiltration", text) == []


def test_a_long_hex_run_is_exfiltration():
    assert _rules("exfiltration", "x " + HEX_RUN + " y") == ["hex-run"]


#: Tag characters spelling ``text``, as they could ride behind a black flag.
def _tags(text):
    return "".join(chr(0xE0000 + ord(char)) for char in text)


@pytest.mark.parametrize(
    "text",
    [
        "Fixed\u200b.",
        "a\u202eb",
        "tag" + _tags("AB"),
        "x\ufe01y",
        "a\u2060b",
        "\u200b\u200b",
        "word\u200dword",
        "mid\ufeffdle",
        "h\ufe0fel\ufe0flo",
        "caf\u00e9\u200d\u00e9t\u00e9",
        "`\u200d`",
        "\U0001f3f4" + _tags("ghp_aa") + "\U000e007f",
        "\U0001f3f4" + _tags("zzzzz") + "\U000e007f",
        "don’️t know",
        "“q”️",
        "a —️ b",
    ],
)
def test_an_invisible_character_is_exfiltration(text):
    assert _rules("exfiltration", text) == ["invisible-character"] * len(_rules("exfiltration", text))
    assert _rules("exfiltration", text)


@pytest.mark.parametrize(
    "text",
    [
        "\ufeffHello",
        "Love \u2764\ufe0f",
        "family \U0001f468\u200d\U0001f469\u200d\U0001f467",
        "\u2764\ufe0f\u200d\U0001f525",
        "flag \U0001f3f4" + _tags("gbsct") + "\U000e007f",
        "keycap 1\ufe0f\u20e3",
        "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",
        "\u0915\u094d\u200d\u0937",
        "\u05e9\u05dc\u05d5\u05dd\u200f!",
        "wow\u203c\ufe0f",
    ],
)
def test_invisible_characters_that_text_needs_are_not_findings(text):
    assert _of_kind("exfiltration", text) == []


def test_invisible_runs_within_a_word_are_one_finding_over_the_invisible_characters():
    text = "a\u200bb\u200bc d\u200be"
    findings = _of_kind("exfiltration", text)
    assert [(f.start, f.end) for f in findings] == [(1, 4), (7, 8)]
    assert findings[0].fingerprint == hmac.new(KEY, "\u200b\u200b".encode(), sha256).hexdigest()


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",
        "172.16.4.4",
        "192.168.1.20",
        "127.0.0.1",
        "169.254.1.1",
        "100.64.0.1",
        "::1",
        "fe80::1",
        "fd12:3456::1",
        "192.168.001.020",
        "010.0.0.1",
        "012.0.0.1",
        "\uff11\uff10\uff0e\uff10\uff0e\uff10\uff0e\uff15",
    ],
)
def test_a_private_address_is_exfiltration(address):
    text = "It listens on " + address + ", see."
    (finding,) = _of_kind("exfiltration", text)
    assert (finding.rule, text[finding.start : finding.end]) == ("private-address", address)


def test_an_address_after_an_underscore_is_found():
    assert _rules("exfiltration", "host_10.0.0.5 is up") == ["private-address"]


@pytest.mark.parametrize("text", ["8.8.8.8", "172.32.0.1", "192.0.2.1", "version 1.2.3.4.5", "v10.0.0.1", "2001:db8::1", "999.1.1.1"])
def test_public_addresses_and_version_numbers_are_not_private(text):
    assert "private-address" not in _rules("exfiltration", text)


def test_a_local_path_finding_spans_the_whole_path():
    path = "/ho" + "me/someone/app/src/main.py"
    text = "Look at " + path + ". Thanks"
    (finding,) = _of_kind("exfiltration", text)
    assert (finding.rule, text[finding.start : finding.end]) == ("local-path", path)


# ---- secrets ----

#: Each secret rule's sample at its bound, and a near miss one character short.
SECRET_SAMPLES = {
    "github-token": ("ghp_" + "a" * 20, "ghp_" + "a" * 19),
    "github-fine-grained-token": ("github_pat_" + "a" * 20, "github_pat_" + "a" * 19),
    "sk-api-key": ("sk-" + "a" * 20, "sk-" + "a" * 19),
    "aws-access-key": ("AKIA" + "A" * 16, "AKIA" + "A" * 15),
    "hugging-face-token": ("hf_" + "a" * 30, "hf_" + "a" * 29),
    "slack-token": ("xoxb-" + "1" * 10, "xoxb-" + "1" * 9),
    "private-key": ("-----BEGIN " + "RSA PRIVATE KEY" + "-----", "-----BEGIN " + "RSA PUBLIC KEY" + "-----"),
    "gitlab-token": ("glpat-" + "a" * 20, "glpat-" + "a" * 19),
    "google-api-key": ("AIza" + "a" * 35, "AIza" + "a" * 34),
    "npm-token": ("npm_" + "a" * 36, "npm_" + "a" * 35),
    "pypi-token": ("pypi-" + "AgEIcHlwaS5vcmc" + "a" * 50, "pypi-" + "AgEIcHlwaS5vcmc" + "a" * 49),
    "sendgrid-api-key": ("SG." + "a" * 66, "SG." + "a" * 65),
    "age-secret-key": ("AGE-SECRET-KEY-" + "1" + "Q" * 58, "AGE-SECRET-KEY-" + "1" + "Q" * 57),
    "telegram-bot-token": ("12345:" + "A" + "a" * 34, "12345:" + "A" + "a" * 33),
    "stripe-key": ("sk_" + "test_" + "a" * 10, "sk_" + "test_" + "a" * 9),
    "shopify-token": ("shpat_" + "a" * 32, "shpat_" + "a" * 31),
    "digitalocean-token": ("dop_v1_" + "a" * 64, "dop_v1_" + "a" * 63),
    "slack-webhook": ("https://hooks.slack.com/services/" + "A" * 43, "https://hooks.slack.com/services/" + "A" * 42),
    "slack-app-token": ("xapp-1-A-1-" + "a", "xapp-1-A-1-"),
    "doppler-token": ("dp.pt." + "a" * 43, "dp.pt." + "a" * 42),
    "linear-api-key": ("lin_api_" + "a" * 40, "lin_api_" + "a" * 39),
    "hugging-face-org-token": ("api_org_" + "a" * 34, "api_org_" + "a" * 33),
    "aws-access-key-id": ("ASIA" + "A" * 16, "ASIA" + "A" * 15),
    "postman-api-key": ("PMAK-" + "a" * 24 + "-" + "a" * 34, "PMAK-" + "a" * 24 + "-" + "a" * 33),
    "grafana-service-account-token": ("glsa_" + "a" * 32 + "_" + "a" * 8, "glsa_" + "a" * 32 + "_" + "a" * 7),
    "perplexity-api-key": ("pplx-" + "a" * 48, "pplx-" + "a" * 47),
    "sentry-user-token": ("sntryu_" + "a" * 64, "sntryu_" + "a" * 63),
    "pulumi-token": ("pul-" + "a" * 40, "pul-" + "a" * 39),
    "vault-token": ("hvs." + "a" * 90, "hvs." + "a" * 89),
    "1password-service-account-token": ("ops_" + "eyJ" + "a" * 250, "ops_" + "eyJ" + "a" * 249),
    "jwt": ("ey" + "a" * 17 + ".ey" + "a" * 17 + ".", "ey" + "a" * 16 + ".ey" + "a" * 17 + "."),
}


def _secret_rules_found(text, rules=SECRET_RULES):
    return {f.rule for f in detect(text, disclosure={}, key=KEY, detectors=(secret_detector(rules),))}


def test_every_secret_rule_has_a_sample():
    assert set(SECRET_SAMPLES) == {rule.rule for rule in SECRET_RULES}
    assert len(SECRET_RULES) == len(SECRET_SAMPLES)  # no two rules share an id


@pytest.mark.parametrize("rule", list(SECRET_SAMPLES))
def test_each_secret_rule_finds_its_sample_and_removing_it_loses_the_sample(rule):
    sample, near_miss = SECRET_SAMPLES[rule]
    text = "Use " + sample + " now."
    (finding,) = [f for f in _detect(text) if f.rule == rule]
    assert (finding.kind, finding.severity, text[finding.start : finding.end]) == ("secret", SEVERITY_SECRET, sample)
    assert rule not in _secret_rules_found(text, [r for r in SECRET_RULES if r.rule != rule])
    assert rule not in _secret_rules_found("Use " + near_miss + " now.")


@pytest.mark.parametrize(
    "rule, token",
    [
        ("github-token", "gho_" + "a" * 20),
        ("github-token", "ghu_" + "a" * 20),
        ("github-token", "ghs_" + "a" * 20),
        ("github-token", "ghr_" + "a" * 20),
        ("slack-token", "xoxa-" + "1" * 10),
        ("slack-token", "xoxp-" + "1" * 10),
        ("slack-token", "xoxr-" + "1" * 10),
        ("slack-token", "xoxs-" + "1" * 10),
        ("stripe-key", "rk_" + "live_" + "a" * 10),
        ("stripe-key", "sk_" + "prod_" + "a" * 10),
        ("aws-access-key-id", "A3TX" + "A" * 16),
        ("aws-access-key-id", "ABIA" + "A" * 16),
        ("aws-access-key-id", "ACCA" + "A" * 16),
        ("slack-webhook", "hooks.slack.com/workflows/" + "A" * 43),
        ("private-key", "-----BEGIN " + "PGP PRIVATE KEY BLOCK" + "-----"),
        ("npm-token", "NPM_" + "A" * 36),
        ("grafana-service-account-token", "GLSA_" + "A" * 32 + "_" + "A" * 8),
        ("slack-app-token", "XAPP-1-A-1-A"),
    ],
)
def test_every_prefix_variant_passes_the_literal_prefilter(rule, token):
    """A rule is scanned only when the text holds one of its literals; every variant its
    pattern allows must hold one."""
    assert rule in _secret_rules_found("Use " + token + " now.")


def test_a_token_of_a_new_rule_wrapped_across_lines_is_found_where_it_starts():
    text = "Key:\n" + "glpat-" + "a" * 10 + "\r\n" + "a" * 10 + " end"
    (finding,) = [f for f in _detect(text) if f.kind == "secret"]
    assert (finding.rule, finding.start, finding.end) == ("gitlab-token", 5, len(text) - 4)


@pytest.mark.parametrize(
    "splitter", ["\ufe0f", "\u200b", "\u00ad", "\u2060", "**", "<b></b>", "<!-- x -->", "`"]
)
def test_a_token_split_by_invisible_characters_or_markup_is_a_secret(splitter):
    text = "Use " + "ghp_" + "a" * 18 + splitter + "a" * 18 + " now."
    (finding,) = [f for f in _detect(text) if f.kind == "secret"]
    assert (finding.rule, finding.start, finding.end) == ("github-token", 4, len(text) - 5)


def test_an_encoded_private_key_header_is_a_secret():
    assert "private-key" in _secret_rules_found("-----BEGIN&#32;RSA PRIVATE KEY-----")


def test_a_token_glued_to_a_word_after_stripping_is_not_a_secret():
    """The word check of the stripped reading: a token continuing a word is no token."""
    assert _secret_rules_found("x" + "ghp_" + "a" * 18 + "\u200b" + "a" * 18) == set()
    assert _secret_rules_found("x " + "ghp_" + "a" * 18 + "\u200b" + "a" * 18) == {"github-token"}


# ---- the mutation checks for the other checks and detectors ----

EXFILTRATION_SAMPLES = {
    "scan_links": ("[a](https://" + COLLECTOR + "/x)", "link-host"),
    "scan_base64_runs": (BASE64_RUN, "base64-run"),
    "scan_hex_runs": (HEX_RUN, "hex-run"),
    "scan_invisible_characters": ("a\u200bb", "invisible-character"),
    "scan_private_addresses": ("on 10.0.0.5 now", "private-address"),
    "scan_local_paths": ("in " + "/ho" + "me/someone/app", "local-path"),
}


def test_every_exfiltration_check_has_a_sample():
    assert set(EXFILTRATION_SAMPLES) == {check.__name__ for check in EXFILTRATION_SCANNERS}


@pytest.mark.parametrize("name", list(EXFILTRATION_SAMPLES))
def test_removing_one_exfiltration_check_loses_its_sample(name):
    text, rule = EXFILTRATION_SAMPLES[name]

    def rules(checks):
        return {f.rule for f in _detect(text, detectors=(chain(*checks),))}

    assert rule in rules(EXFILTRATION_SCANNERS)
    assert rule not in rules([check for check in EXFILTRATION_SCANNERS if check.__name__ != name])


#: A sample for each of LOCAL_PATH_RULES, in its order.
PATH_SAMPLES = (
    "/Us" + "ers/someone/app",
    "C:" + "\\Users\\someone\\app",
    "/mnt/c" + "/Us" + "ers/someone/app",
    "/private" + "/var/folders/xy/T/app.log",
    "app/" + ".env",
)


@pytest.mark.parametrize("index", range(len(PATH_SAMPLES)))
def test_removing_one_path_rule_loses_its_sample(index):
    assert len(PATH_SAMPLES) == len(LOCAL_PATH_RULES)
    text = "See " + PATH_SAMPLES[index] + " here"
    start = text.index(PATH_SAMPLES[index]) + (4 if index == 4 else 0)

    def hits(rules):
        return {(f.rule, f.start) for f in _detect(text, detectors=(local_path_scanner(rules),))}

    rule = LOCAL_PATH_RULES[index]
    assert (rule.rule, start) in hits(LOCAL_PATH_RULES)
    assert (rule.rule, start) not in hits(LOCAL_PATH_RULES[:index] + LOCAL_PATH_RULES[index + 1 :])


PERSONAL_SAMPLES = {"scan_personal_terms": "personal-term", "scan_email_addresses": "email-address"}


@pytest.mark.parametrize("name", list(PERSONAL_SAMPLES))
def test_removing_one_personal_check_loses_its_sample(name):
    assert set(PERSONAL_SAMPLES) == {check.__name__ for check in PERSONAL_SCANNERS}
    text, rule = "Mail " + ADDRESS + " now", PERSONAL_SAMPLES[name]

    def rules(checks):
        return {f.rule for f in _detect(text, personal_terms=[ADDRESS], detectors=(chain(*checks),))}

    assert rule in rules(PERSONAL_SCANNERS)
    assert rule not in rules([check for check in PERSONAL_SCANNERS if check.__name__ != name])


#: A text holding one finding of each kind, in the order of KINDS.
KIND_SAMPLES = (
    "Use " + "ghp_" + "a" * 36,
    "Here: " + CANARY,
    "Heron slips.",
    "a\u200bb",
    "Mail " + ADDRESS,
    "Cy agreed.",
)


@pytest.mark.parametrize("index", range(len(KINDS)))
def test_removing_one_detector_loses_its_kind(index):
    kind, text = KINDS[index], KIND_SAMPLES[index]

    def kinds(detectors):
        return {f.kind for f in _detect(text, canary_terms=[CANARY], detectors=detectors)}

    assert kind in kinds(DFLT_DETECTORS)
    assert kind not in kinds(DFLT_DETECTORS[:index] + DFLT_DETECTORS[index + 1 :])


# ---- no value in a finding ----

#: Every sample above, with the options it is detected under.
_TABLE = (
    [(text, {"disclosure": LEAK_DISCLOSURE}) for _, text in LEAKS.values()]
    + [("Update: " + form + " slips.", {}) for form in HERON_FORMS.values()]
    + [("Use " + sample + " now.", {}) for sample, _ in SECRET_SAMPLES.values()]
    + [(text, {}) for text, _ in EXFILTRATION_SAMPLES.values()]
    + [("Reach me at " + ADDRESS + " today.", {"personal_terms": [ADDRESS]})]
    + [(text, {"canary_terms": [CANARY]}) for text in KIND_SAMPLES]
)


#: Every rule id the module has: a finding's rule must be one, so it can carry no text.
KNOWN_RULES = (
    {rule.rule for rule in SECRET_RULES}
    | {rule.rule for rule in LOCAL_PATH_RULES}
    | {"canary-term", "term", "person-name", "image-host", "link-host", "base64-run", "hex-run"}
    | {"invisible-character", "private-address", "personal-term", "email-address"}
)


@pytest.mark.parametrize("text, options", _TABLE)
def test_no_finding_holds_the_text_it_matched(text, options):
    """The matched text appears nowhere in a finding, in any letter case; its normalised
    form appears in no field but the rule id, which must be one of the module's rule ids
    (``env-file`` holds the ``env`` of ``.env``)."""
    findings = _detect(text, **options)
    assert findings
    for finding in findings:
        assert finding.rule in KNOWN_RULES, finding
        value = text[finding.start : finding.end]
        record = (repr(finding) + json.dumps(finding.to_dict(), ensure_ascii=False)).casefold()
        assert value.casefold() not in record, finding
        fields = {name: field for name, field in finding.to_dict().items() if name != "rule"}
        folded = normalise(value).text
        assert not folded or folded not in json.dumps(fields, ensure_ascii=False).casefold(), finding


# ---- fingerprints and the key ----


def test_a_fingerprint_is_stable_across_calls_and_changes_with_the_key():
    (first,) = _of_kind("vocabulary", "Heron slips.")
    (second,) = _of_kind("vocabulary", "Heron slips.")
    (other,) = _of_kind("vocabulary", "Heron slips.", key=OTHER_KEY)
    assert first.fingerprint == second.fingerprint != other.fingerprint
    assert first.fingerprint == hmac.new(KEY, b"heron", sha256).hexdigest()


def test_every_form_of_a_term_has_one_fingerprint():
    fingerprints = {_of_kind("vocabulary", "x " + form + " y")[0].fingerprint for form in HERON_FORMS.values()}
    assert len(fingerprints) == 1


def test_a_secret_fingerprint_keeps_letter_case_and_ignores_how_the_token_is_split():
    def fingerprint(token):
        (finding,) = [f for f in _detect("Use " + token + " now") if f.kind == "secret"]
        return finding.fingerprint

    token = "ghp_" + "AbCd" * 9
    assert fingerprint(token) != fingerprint(token.lower())
    assert fingerprint(token) == fingerprint(token[:20] + "\u200b" + token[20:]) == fingerprint(token[:20] + "\n" + token[20:])
    assert fingerprint(token) == hmac.new(KEY, token.encode(), sha256).hexdigest()


def test_a_key_may_be_a_callable():
    assert _of_kind("vocabulary", "Heron", key=lambda: KEY) == _of_kind("vocabulary", "Heron")


def test_the_fingerprint_key_is_created_once_and_owner_only(tmp_path):
    state_dir = tmp_path / "state"
    key = fingerprint_key(state_dir)
    assert len(key) == DFLT_KEY_BYTES
    assert fingerprint_key(state_dir) == key
    assert [path.name for path in state_dir.iterdir()] == [DFLT_KEY_FILE]
    if os.name == "posix":
        assert stat.S_IMODE((state_dir / DFLT_KEY_FILE).stat().st_mode) == 0o600


def test_processes_racing_to_create_the_key_all_read_the_same_one(tmp_path):
    for trial in range(20):
        state_dir = tmp_path / f"trial-{trial}"
        with ThreadPoolExecutor(max_workers=8) as pool:
            keys = set(pool.map(lambda _: fingerprint_key(state_dir), range(8)))
        assert len(keys) == 1


def test_a_busy_key_file_is_read_again(tmp_path, monkeypatch):
    """What a Windows reader meets while another process moves its key into place."""
    key = fingerprint_key(tmp_path)
    read_bytes, calls = Path.read_bytes, []

    def busy_twice(path):
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError("the file is in use by another process")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", busy_twice)
    monkeypatch.setattr(detect_module, "KEY_READ_RETRY_S", 0)
    assert fingerprint_key(tmp_path) == key
    assert len(calls) == 3


def test_a_key_file_that_stays_busy_raises(tmp_path, monkeypatch):
    fingerprint_key(tmp_path)

    def busy(path):
        raise PermissionError("the file is in use by another process")

    monkeypatch.setattr(Path, "read_bytes", busy)
    monkeypatch.setattr(detect_module, "KEY_READ_RETRY_S", 0)
    with pytest.raises(FingerprintKeyError, match="cannot be read"):
        fingerprint_key(tmp_path)


def test_the_key_is_created_where_hard_links_are_refused(tmp_path, monkeypatch):
    def refuse(source, target):
        raise PermissionError("hard links are not supported on this filesystem")

    monkeypatch.setattr(os, "link", refuse)
    monkeypatch.setattr(os, "rename", refuse)
    key = fingerprint_key(tmp_path)
    assert len(key) == DFLT_KEY_BYTES
    assert fingerprint_key(tmp_path) == key
    assert [path.name for path in tmp_path.iterdir()] == [DFLT_KEY_FILE]


def test_a_short_key_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(detect_module, "KEY_READ_RETRY_S", 0)
    (tmp_path / DFLT_KEY_FILE).write_bytes(b"short")
    with pytest.raises(FingerprintKeyError, match="fewer than 32"):
        fingerprint_key(tmp_path)


def test_the_default_key_is_read_only_when_there_is_a_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(detect_module, "_configured_state_dir", lambda: tmp_path)
    assert detect("Nothing to see.", disclosure={}) == ()
    assert not (tmp_path / DFLT_KEY_FILE).exists()
    token = "ghp_" + "a" * 36
    (finding,) = detect("Use " + token, disclosure={})
    assert finding.fingerprint == hmac.new(fingerprint_key(tmp_path), token.encode(), sha256).hexdigest()


# ---- the record, the inputs, the time bound ----


def test_to_dict_is_json_ready_and_carries_every_field():
    finding = Finding(kind="vocabulary", start=1, end=6, entity=HERON, label="amber", sealed_from=("p-02",), rule="term", severity=4, fingerprint="f")
    assert json.loads(json.dumps(finding.to_dict())) == {
        "kind": "vocabulary", "start": 1, "end": 6, "entity": HERON, "label": "amber",
        "sealed_from": ["p-02"], "rule": "term", "severity": 4, "fingerprint": "f",
    }  # fmt: skip


@pytest.mark.parametrize("option", ["allowlist", "canary_terms", "personal_terms"])
def test_one_string_where_a_collection_belongs_is_refused(option):
    with pytest.raises(TypeError, match="not one string"):
        detect("x", disclosure={}, key=KEY, **{option: "example.org"})


@pytest.mark.parametrize(
    "options, message",
    [
        (dict(disclosure=None), "disclosure must be a mapping"),
        (dict(disclosure={}, key="a string"), "key must be bytes"),
        (dict(disclosure={"vocabulary": ["Heron"]}), "vocabulary entry 0"),
    ],
)
def test_malformed_inputs_are_refused_with_a_reason(options, message):
    with pytest.raises(TypeError, match=message):
        detect("Heron", **options)


def test_findings_are_ordered_by_position():
    text = "Cy, Heron and " + "ghp_" + "a" * 36
    starts = [f.start for f in _detect(text)]
    assert starts == sorted(starts) and len(starts) == 3


@pytest.mark.parametrize(
    "text",
    [
        LONG_WORD,
        "\uff28\u00e9-x\u00a0" * 250_000,
        ("![a](" + "x" * 8 + ")") * 71_429,
        "https://" * 125_000,
        "/Us" "ers/" * 142_858,
        "a\u200bb" * 333_334,
        "He<b></b>ron &amp; " * 52_632,
        ('<img src="//' + COLLECTOR + '/p.png">') * 25_642,
    ],
    ids=["0.1-long-word", "non-ascii", "markdown-images", "urls", "paths", "invisible", "markup", "html-images"],
)
def test_a_million_character_message_is_scanned_within_the_bound(text):
    options = dict(canary_terms=[CANARY], personal_terms=[ADDRESS])
    started = time.perf_counter()
    _detect(text, **options)
    elapsed = time.perf_counter() - started
    assert elapsed < LONG_WORD_SCAN_BOUND_S, f"detect took {elapsed:.1f}s"


# ---- a term a link's path spells out (liaise #46) ----

_HERON = [{"term": "Heron", "entity": "project:heron", "label": "amber"}]
_PEOPLE = [{"term": "Bram Kest", "entity": "person:bram", "label": "amber"}]


def _link_terms(text, vocabulary=_HERON, people=None):
    from liaise.detect import detect_link_terms

    disclosure = {"vocabulary": vocabulary, "people": people or {}}
    return [(f.kind, f.rule, f.start, f.end) for f in detect(text, disclosure=disclosure, key=KEY, detectors=[detect_link_terms])]


@pytest.mark.parametrize(
    "url",
    [
        "https://files.example.net/HeronAcquisitionTerms.pdf",
        "https://files.example.net/d?p=heronTerms",
        "https://files.example.net/heron2026.pdf",
        "https://files.example.net/x#theHeronPlan",
        "https://files.example.net/%48eronTerms",
    ],
)
def test_a_term_glued_into_a_link_is_found_over_the_whole_destination(url):
    text = f"Notes: {url} ok"
    assert _link_terms(text) == [("vocabulary", "term-in-link", 7, 7 + len(url))]


def test_a_markdown_destination_and_a_multi_word_person_are_read_too():
    text = "See [notes](https://files.example.net/BramKestNotes.md)."
    assert _link_terms(text, vocabulary=_PEOPLE) == [("third_party", "term-in-link", 12, 54)]
    assert _link_terms(text, vocabulary=_PEOPLE, people={"bram": {}}) == []  # a reader is no third party


def test_what_the_whole_word_reading_finds_or_the_host_says_is_not_found_again():
    assert _link_terms("See https://files.example.net/heron-terms.pdf") == []
    assert _link_terms("See https://heronterms.example.net/a") == []  # the host is scan_links' concern
    assert _link_terms("See https://files.example.net/Heronry.pdf and HeronTerms") == []


def test_the_link_term_is_labelled_and_graded_like_the_term():
    text = "See https://files.example.net/HeronTerms.pdf"
    (plain,) = detect("Heron", disclosure={"vocabulary": _HERON}, key=KEY)
    (glued,) = [f for f in detect(text, disclosure={"vocabulary": _HERON}, key=KEY) if f.rule == "term-in-link"]
    assert (glued.label, glued.entity, glued.severity) == (plain.label, plain.entity, plain.severity)
