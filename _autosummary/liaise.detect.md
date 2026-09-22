# liaise.detect

Detectors for outbound messages: what a message holds, reported without the value.

[`detect()`](#liaise.detect.detect) runs [`DFLT_DETECTORS`](#liaise.detect.DFLT_DETECTORS) over a message and returns [`Finding`](#liaise.detect.Finding)
records. A finding says what was found (`kind` and `rule`), where (`start` and
`end`, character offsets into the message as written), what it concerns (`entity`,
`label`, `sealed_from`), how severe it is, and a keyed `fingerprint` that lets the
ledger correlate a repeat. It never holds the matched text. Detectors only find; what a
finding means for a send is the policy’s decision (liaise discussion 32, §5.4).

The six kinds (discussion §5.2):

- `secret`: the 0.1 leak scan’s token shapes and private-key header, unchanged, and a
  curated set of distinctive-prefix rules ([`SECRET_RULES`](#liaise.detect.SECRET_RULES)). A token split by line
  breaks, invisible characters, emphasis marks or HTML markup is found too. There is no
  generic-entropy rule: its precision is too low to divert on (research §5.2).
- `canary`: a `canary_terms` entry anywhere after normalisation, even inside a word or
  percent-encoded.
- `vocabulary`: a term of `disclosure["vocabulary"]` whose entity is not a person, as
  a whole word after normalisation.
- `third_party`: the same for a person’s term (entity `person:<id>`), unless that person
  is a reader, one of `disclosure["people"]`. A reader is never a third party.
- `exfiltration`: link and image destinations whose host is not in `allowlist` (a host
  or any of its subdomains), read as a browser reads them (Markdown inline and reference
  links and images, HTML attributes, autolinks and bare URLs); base64 runs, wrapped or not,
  and hex runs; invisible characters, except where emoji, the three subdivision flags,
  joining scripts or right-to-left text need them; private, loopback, shared and
  link-local addresses; local paths and `.env` files (the 0.1 path patterns).
- `personal`: a `personal_terms` entry as a whole word after normalisation, and any
  email address (the 0.1 pattern).

**Two readings.** Terms and secrets are looked for in the message as written and, when it
holds markup, as a Markdown or HTML reader sees it ([`render()`](#liaise.detect.render)): tags, comments and
backslash escapes removed (a block tag reading as a space), character references and
percent-escapes decoded. A finding in either reading counts, so a plain-text reader and a
rendering one are both covered; the price is that markup a renderer hides can still read
as a word break to the plain-text reading.

**Normalisation** ([`normalise()`](#liaise.detect.normalise), research §5.5) folds each character by
compatibility decomposition (NFKD, so full-width and other compatibility forms fold as
NFKC folds them), maps confusable letters to the ASCII letter they imitate (Unicode’s
confusables data, plus the small capitals and Cyrillic and Greek shapes it does not map),
case-folds, drops combining marks, and removes invisible characters, separators
(whitespace, dashes and minus signs, underscores, dots) and the Markdown marks `*`,
`~`, backtick and backslash. An offset map sends every normalised character back to the
characters of the message it came from, so a finding covers the text as written.

**Whole words.** A term matches when it neither continues a word on either side (judged in
the reading, past invisible characters and marks) nor spans a word break the term does
not have: `He-ron`, a hyphenated line wrap and `H e r o n` are “Heron”; `on a` and
`He ron` are not. Canary terms match anywhere.

**Fingerprints** are HMAC-SHA256, keyed by [`fingerprint_key()`](#liaise.detect.fingerprint_key) (32 random bytes in
`<state_dir>/fingerprint.key`, created on first use, owner-only), over the value as a
reader sees it: normalised for the kinds that match terms and addresses (`canary`,
`vocabulary`, `third_party`, `personal`), so every disguise of a term correlates;
exact for `secret` and `exfiltration`, which are case-sensitive, with only line
breaks, invisible characters, emphasis marks and markup removed. The value itself is used
when that leaves nothing. The key is read only when there is a finding.

**Severity** (discussion §5.2): `secret` and `canary` 5; `exfiltration` 4; a term
sealed from one of the readers 4; a term labelled above the readers’ least clearance
(`disclosure["least_clearance"]`) 3; anything else by audience, 2 when the least
clearance is `clear` (or unknown) and 1 otherwise.

**The seam.** `detectors=` takes any callables `(Scan) -> Iterable[Finding]`; a
[`Scan`](#liaise.detect.Scan) holds the inputs and builds findings. Its pointers are Presidio as an
optional extra and a semantic pass that may only add findings (discussion §8). This
module imports neither acquaint nor correspond: `disclosure` is the JSON of
`acquaint.disclosure` (discussion §4.5).

**Sources.** The secret rules after the six 0.1 shapes and the private-key header adapt
the regular expressions of the rules with the same or similar ids in gitleaks’ default
configuration (`config/gitleaks.toml`, MIT licence, copyright (c) 2019 Zachary Rice;
the notice is reproduced beside [`SECRET_RULES`](#liaise.detect.SECRET_RULES)): capturing and trailing-terminator
groups are dropped, since the scan adds its own word boundary, unescaped dots are
escaped and unbounded repetitions are bounded. `data/confusables.json` is a selection
of Unicode’s confusables data (UTS #39, Unicode License v3; the notice travels in the
file), regenerated by `misc/scripts/make_confusables.py`. No rule or table comes from a
share-alike source.

### Module Attributes

| [`KINDS`](#liaise.detect.KINDS)                   | The kinds a finding can have, in the order [`DFLT_DETECTORS`](#liaise.detect.DFLT_DETECTORS) looks for them.                                                                                                                 |
|--------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`FOLDED_KINDS`](#liaise.detect.FOLDED_KINDS)            | The kinds whose fingerprint is taken over the normalised value.                                                                                                                                                                            |
| [`LABELS`](#liaise.detect.LABELS)                  | Traffic-light labels, least restrictive first (discussion decision 3).                                                                                                                                                                     |
| [`DFLT_ENTITY_LABEL`](#liaise.detect.DFLT_ENTITY_LABEL)       | amber for a project or an organisation, green for a person (discussion §4.2).                                                                                                                                                              |
| [`PERSON_PREFIX`](#liaise.detect.PERSON_PREFIX)           | How a vocabulary entry's entity names a person.                                                                                                                                                                                            |
| [`SEVERITY_WIDE_AUDIENCE`](#liaise.detect.SEVERITY_WIDE_AUDIENCE)  | The severity of any other finding, when the least-cleared reader has clearance `clear` (a public or unknown audience), and when not.                                                                                                       |
| [`DFLT_STATE_DIR`](#liaise.detect.DFLT_STATE_DIR)          | Where the fingerprint key lives when no liaise config names a state directory.                                                                                                                                                             |
| [`KEY_READ_ATTEMPTS`](#liaise.detect.KEY_READ_ATTEMPTS)       | How often, and how far apart, a key file is read again while it is busy (on Windows a reader can meet a sharing violation while another process moves its new key into place) or shorter than a key (another process is still writing it). |
| [`MIN_BASE64_RUN`](#liaise.detect.MIN_BASE64_RUN)          | 75 bytes of data.                                                                                                                                                                                                                          |
| [`MIN_WRAPPED_BASE64_LINE`](#liaise.detect.MIN_WRAPPED_BASE64_LINE) | The narrowest width of a wrapped base64 block (PEM wraps at 64, MIME at 76), and the shortest unpadded last line that reads as the end of the data.                                                                                        |
| [`MIN_HEX_RUN`](#liaise.detect.MIN_HEX_RUN)             | longer than a SHA-512 digest, so digests and commit hashes quoted in a message are not findings.                                                                                                                                           |
| [`MAX_DESTINATION`](#liaise.detect.MAX_DESTINATION)         | How much of a link destination is read for its host.                                                                                                                                                                                       |
| [`CONFUSABLES_RESOURCE`](#liaise.detect.CONFUSABLES_RESOURCE)    | The package data file of confusable characters.                                                                                                                                                                                            |
| [`TOKEN_SHAPES`](#liaise.detect.TOKEN_SHAPES)            | the GitHub (`ghp_` and its siblings, `github_pat_`), `sk-` API key, AWS access key, Hugging Face (`hf_`) and Slack (`xoxb-` and its siblings) shapes.                                                                                      |
| [`TOKEN_RULES`](#liaise.detect.TOKEN_RULES)             | The rule id of each of [`TOKEN_SHAPES`](#liaise.detect.TOKEN_SHAPES), in order.                                                                                                                                            |
| [`TOKEN_LITERALS`](#liaise.detect.TOKEN_LITERALS)          | The literals every match of each of [`TOKEN_SHAPES`](#liaise.detect.TOKEN_SHAPES) contains, in order.                                                                                                                      |
| [`EMAIL_PATTERN`](#liaise.detect.EMAIL_PATTERN)           | An email address.                                                                                                                                                                                                                          |
| [`PRIVATE_KEY_PATTERN`](#liaise.detect.PRIVATE_KEY_PATTERN)     | PEM's <br/><br/>```<br/>``<br/>```<br/><br/>-----BEGIN .                                                                                                                                                                                   |
| [`LOCAL_PATH_PATTERNS`](#liaise.detect.LOCAL_PATH_PATTERNS)     | home directories on macOS, Linux and Windows (its backslashes single, or doubled as JSON writes them), a Windows home through a WSL mount, and macOS's temporary directories.                                                              |
| [`ENV_FILE_PATTERN`](#liaise.detect.ENV_FILE_PATTERN)        | A path ending in `.env`.                                                                                                                                                                                                                   |
| [`Detector`](#liaise.detect.Detector)                | one detector, or one check within a detector.                                                                                                                                                                                              |
| [`SECRET_RULES`](#liaise.detect.SECRET_RULES)            | the 0.1 token shapes and private-key header, then distinctive-prefix rules adapted from gitleaks (the gitleaks id precedes each).                                                                                                          |
| [`MAX_TAG_LOOKBACK`](#liaise.detect.MAX_TAG_LOOKBACK)        | How far back an attribute looks for the `<` of the tag it would belong to.                                                                                                                                                                 |
| [`INTERNAL_NETWORKS`](#liaise.detect.INTERNAL_NETWORKS)       | Private (RFC 1918 and unique-local), loopback, shared (RFC 6598) and link-local networks: addresses that say something about the inside of a network.                                                                                      |
| [`LOCAL_PATH_LITERALS`](#liaise.detect.LOCAL_PATH_LITERALS)     | The literals every match of each of [`LOCAL_PATH_PATTERNS`](#liaise.detect.LOCAL_PATH_PATTERNS) contains, in order.                                                                                                               |
| [`LOCAL_PATH_RULES`](#liaise.detect.LOCAL_PATH_RULES)        | The 0.1 path patterns as rules.                                                                                                                                                                                                            |
| [`EXFILTRATION_SCANNERS`](#liaise.detect.EXFILTRATION_SCANNERS)   | The checks `detect_exfiltration` runs.                                                                                                                                                                                                     |
| [`PERSONAL_SCANNERS`](#liaise.detect.PERSONAL_SCANNERS)       | The checks `detect_personal` runs.                                                                                                                                                                                                         |
| [`DFLT_DETECTORS`](#liaise.detect.DFLT_DETECTORS)          | The detectors [`detect()`](#liaise.detect.detect) runs by default, one per kind of [`KINDS`](#liaise.detect.KINDS).                                                                           |

### Functions

| [`chain`](#liaise.detect.chain)(\*detectors[, name])                        | One detector that yields the findings of `detectors`, in order.                                                                                                                                                                                                                                                              |
|----------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`detect`](#liaise.detect.detect)(text, \*, disclosure[, allowlist, ...])    | What `detectors` find in `text`, ordered by position, each finding once.                                                                                                                                                                                                                                                     |
| [`detect_canaries`](#liaise.detect.detect_canaries)(scan)                             | A `canary` finding, severity 5, wherever a canary term occurs, even inside a word: a canary is unique by construction, so a match anywhere is the alarm.                                                                                                                                                                     |
| `detect_exfiltration`(scan)                                                                        |                                                                                                                                                                                                                                                                                                                              |
| `detect_personal`(scan)                                                                            |                                                                                                                                                                                                                                                                                                                              |
| [`detect_third_parties`](#liaise.detect.detect_third_parties)(scan)                        | A `third_party` finding for each whole-word occurrence of a person's disclosure term, when that person is not a reader.                                                                                                                                                                                                      |
| [`detect_vocabulary`](#liaise.detect.detect_vocabulary)(scan)                           | A `vocabulary` finding for each whole-word occurrence of a disclosure term whose entity is not a person.                                                                                                                                                                                                                     |
| [`fingerprint_key`](#liaise.detect.fingerprint_key)([state_dir, key_file, ...])       | The fingerprint key in `<state_dir>/<key_file>`, created on first use.                                                                                                                                                                                                                                                       |
| [`fold_term`](#liaise.detect.fold_term)(term)                                   | `term` folded as [`normalise()`](#liaise.detect.normalise) folds a message, with its word breaks.                                                                                                                                                                                                         |
| [`key_source`](#liaise.detect.key_source)([state_dir, key_file, key_bytes, ...]) | A callable that answers one key, however often it is asked: [`fingerprint_key()`](#liaise.detect.fingerprint_key), once.                                                                                                                                                                                        |
| [`link_urls`](#liaise.detect.link_urls)(text)                                   | Every link and image destination in `text`, in full, in order, each once.                                                                                                                                                                                                                                                    |
| [`local_path_scanner`](#liaise.detect.local_path_scanner)([rules])                       | A check for `rules`: an `exfiltration` finding spanning each path, from where its rule matched to the end of the path.                                                                                                                                                                                                       |
| [`normalise`](#liaise.detect.normalise)(text)                                   | Fold `text` for matching, keeping where each folded character came from.                                                                                                                                                                                                                                                     |
| [`render`](#liaise.detect.render)(text)                                      | `text` as a Markdown or HTML reader sees it, or None when rendering changes nothing.                                                                                                                                                                                                                                         |
| [`scan_base64_runs`](#liaise.detect.scan_base64_runs)(scan)                            | An `exfiltration` finding for each base64 or base64url run of at least [`MIN_BASE64_RUN`](#liaise.detect.MIN_BASE64_RUN) characters, on one line or wrapped over several (indented or quoted too), that mixes digits, capitals and small letters, which encoded data does and a long word or path rarely does. |
| [`scan_email_addresses`](#liaise.detect.scan_email_addresses)(scan)                        | A `personal` finding for each email address.                                                                                                                                                                                                                                                                                 |
| [`scan_hex_runs`](#liaise.detect.scan_hex_runs)(scan)                               | An `exfiltration` finding for each run of at least [`MIN_HEX_RUN`](#liaise.detect.MIN_HEX_RUN) hex digits that mixes digits and letters.                                                                                                                                                                    |
| [`scan_invisible_characters`](#liaise.detect.scan_invisible_characters)(scan)                   | An `exfiltration` finding for each word holding invisible characters the text does not need to render (see `_needed_invisible()`): zero-width spaces, direction overrides, tag characters, variation selectors used to carry data.                                                                                           |
| [`scan_links`](#liaise.detect.scan_links)(scan)                                  | An `exfiltration` finding for each link or image whose host is not allowlisted.                                                                                                                                                                                                                                              |
| [`scan_personal_terms`](#liaise.detect.scan_personal_terms)(scan)                         | A `personal` finding for each whole-word occurrence of a personal term.                                                                                                                                                                                                                                                      |
| [`scan_private_addresses`](#liaise.detect.scan_private_addresses)(scan)                      | An `exfiltration` finding for each IPv4 or IPv6 address in [`INTERNAL_NETWORKS`](#liaise.detect.INTERNAL_NETWORKS).                                                                                                                                                                                               |
| [`secret_detector`](#liaise.detect.secret_detector)([rules])                          | A detector of `rules`: a `secret` finding for each match, severity 5.                                                                                                                                                                                                                                                        |
| [`visible`](#liaise.detect.visible)(text)                                     | `text` with each invisible or control character written as `<U+XXXX>`.                                                                                                                                                                                                                                                       |

### Classes

| [`Finding`](#liaise.detect.Finding)(\*, kind, start, end[, entity, ...])   | Something a detector found in a message: never the matched text.                  |
|-------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------|
| [`FoldedTerm`](#liaise.detect.FoldedTerm)(text, breaks[, lead, trail])        | A term as normalisation folds it, and where its own words break.                  |
| [`Normalised`](#liaise.detect.Normalised)(source, text[, origins, ends, ...]) | A reading of a message folded for matching terms.                                 |
| [`PathRule`](#liaise.detect.PathRule)(rule, pattern[, literals])            | A local-path shape: `rule` names it in findings, `pattern` finds where it starts. |
| [`Scan`](#liaise.detect.Scan)(text[, disclosure, allowlist, ...])       | What detectors look at: the message, the disclosure and the terms, and the key.   |
| [`SecretRule`](#liaise.detect.SecretRule)(rule, pattern[, literals, ...])     | A secret's shape: `rule` names it in findings, `pattern` is its expression.       |
| [`View`](#liaise.detect.View)(source, text[, origins, ends])            | `text` derived from `source`, and where each of its characters came from.         |

### Exceptions

| [`FingerprintKeyError`](#liaise.detect.FingerprintKeyError)   | The fingerprint key file exists but cannot be used.   |
|------------------------------------------------------------------------|-------------------------------------------------------|

### liaise.detect.CONFUSABLES_RESOURCE *= 'confusables.json'*

The package data file of confusable characters.

### liaise.detect.DFLT_DETECTORS *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Callable](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[[Scan](#liaise.detect.Scan)], [Iterable](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterable)[[Finding](#liaise.detect.Finding)]], ...]* *= (<function secret_detector.<locals>.detect_secrets>, <function detect_canaries>, <function detect_vocabulary>, <function chain.<locals>.chained>, <function chain.<locals>.chained>, <function detect_third_parties>)*

The detectors [`detect()`](#liaise.detect.detect) runs by default, one per kind of [`KINDS`](#liaise.detect.KINDS).

### liaise.detect.DFLT_ENTITY_LABEL *= 'amber'*

amber for a project or an organisation,
green for a person (discussion §4.2).

* **Type:**
  The label of a vocabulary entry that has none

### liaise.detect.DFLT_STATE_DIR *= PosixPath('~/.local/share/liaise')*

Where the fingerprint key lives when no liaise config names a state directory.

### liaise.detect.Detector

one detector, or one check within a detector.

* **Type:**
  `(scan) -> findings`

alias of [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[[`Scan`](#liaise.detect.Scan)], [`Iterable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterable)[[`Finding`](#liaise.detect.Finding)]]

### liaise.detect.EMAIL_PATTERN *= '(?:(?<![\\\\w.%+-])[\\\\w.%+-]{1,64}|[\\\\w.%+-])@[A-Za-z0-9-]{1,63}(?:\\\\.[A-Za-z0-9-]{1,63}){0,127}\\\\.[A-Za-z]{2,63}'*

An email address. Its local part is anchored where it starts and bounded, so a long run
of word characters is tried once, not once per character, which made the scan quadratic
in its length. A local part longer than the bound is still found, from its last
character. Each domain label and the label count are bounded too.

### liaise.detect.ENV_FILE_PATTERN *= re.compile('(?<=[\\\\\\\\/])\\\\.env(?![\\\\w-]|\\\\.\\\\w)')*

A path ending in `.env`.

### liaise.detect.EXFILTRATION_SCANNERS *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Callable](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[[Scan](#liaise.detect.Scan)], [Iterable](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterable)[[Finding](#liaise.detect.Finding)]], ...]* *= (<function scan_links>, <function scan_base64_runs>, <function scan_hex_runs>, <function scan_invisible_characters>, <function scan_private_addresses>, <function local_path_scanner.<locals>.scan_local_paths>)*

The checks `detect_exfiltration` runs.

### liaise.detect.FOLDED_KINDS *= frozenset({'canary', 'personal', 'third_party', 'vocabulary'})*

The kinds whose fingerprint is taken over the normalised value.

### *class* liaise.detect.Finding(, kind, start, end, entity=None, label=None, sealed_from=(), rule, severity, fingerprint, part=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Something a detector found in a message: never the matched text.

`start` and `end` are offsets into the message as written. `entity`, `label`
and `sealed_from` are set for terms from the disclosure. `rule` names the pattern
or check that matched; `fingerprint` is the keyed HMAC of the value. `part` names
the part of the message the offsets are into when it is not the text (`title`,
`attachment name`); [`detect()`](#liaise.detect.detect) scans one part and leaves it None.

#### to_dict()

The finding as a JSON-ready dict; `part` only when it names one.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *exception* liaise.detect.FingerprintKeyError

Bases: [`Exception`](https://docs.python.org/3/builtins/exceptions.html#Exception)

The fingerprint key file exists but cannot be used.

### *class* liaise.detect.FoldedTerm(text, breaks, lead='', trail='')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A term as normalisation folds it, and where its own words break.

`breaks` holds each index `k` of `text` such that the term had a separator
between `text[k - 1]` and `text[k]`. `lead` and `trail` are the characters the
term starts and ends with that folding drops (the `~` of `~/notes`); a match
covers them too when the message has them.

### liaise.detect.INTERNAL_NETWORKS *= (IPv4Network('10.0.0.0/8'), IPv4Network('172.16.0.0/12'), IPv4Network('192.168.0.0/16'), IPv4Network('127.0.0.0/8'), IPv4Network('100.64.0.0/10'), IPv4Network('169.254.0.0/16'), IPv6Network('::1/128'), IPv6Network('fc00::/7'), IPv6Network('fe80::/10'))*

Private (RFC 1918 and unique-local), loopback, shared (RFC 6598) and link-local
networks: addresses that say something about the inside of a network.

### liaise.detect.KEY_READ_ATTEMPTS *= 20*

How often, and how far apart, a key file is read again while it is busy (on Windows a
reader can meet a sharing violation while another process moves its new key into
place) or shorter than a key (another process is still writing it).

### liaise.detect.KINDS *= ('secret', 'canary', 'vocabulary', 'exfiltration', 'personal', 'third_party')*

The kinds a finding can have, in the order [`DFLT_DETECTORS`](#liaise.detect.DFLT_DETECTORS) looks for them.

### liaise.detect.LABELS *= ('clear', 'green', 'amber', 'red')*

Traffic-light labels, least restrictive first (discussion decision 3).

### liaise.detect.LOCAL_PATH_LITERALS *= (('/Users/', '/home/', '/root/'), (), (), ('/private/var/', '/var/folders/'))*

The literals every match of each of [`LOCAL_PATH_PATTERNS`](#liaise.detect.LOCAL_PATH_PATTERNS) contains, in order.

### liaise.detect.LOCAL_PATH_PATTERNS *= (re.compile('(?<![\\\\w.~-])/(?:Users|home|root)/'), re.compile('\\\\b[A-Za-z]:(?:\\\\\\\\{1,2}|/)Users(?:\\\\\\\\{1,2}|/)', re.IGNORECASE), re.compile('(?<![\\\\w.~-])/mnt/[A-Za-z]/Users/', re.IGNORECASE), re.compile('(?<![\\\\w.~-])/(?:private/var|var/folders)/'))*

home directories on macOS, Linux and Windows (its backslashes
single, or doubled as JSON writes them), a Windows home through a WSL mount, and
macOS’s temporary directories.

* **Type:**
  Absolute local paths

### liaise.detect.LOCAL_PATH_RULES *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[PathRule](#liaise.detect.PathRule), ...]* *= (PathRule(rule='local-path', pattern=re.compile('(?<![\\\\w.~-])/(?:Users|home|root)/'), literals=('/Users/', '/home/', '/root/')), PathRule(rule='local-path', pattern=re.compile('\\\\b[A-Za-z]:(?:\\\\\\\\{1,2}|/)Users(?:\\\\\\\\{1,2}|/)', re.IGNORECASE), literals=()), PathRule(rule='local-path', pattern=re.compile('(?<![\\\\w.~-])/mnt/[A-Za-z]/Users/', re.IGNORECASE), literals=()), PathRule(rule='local-path', pattern=re.compile('(?<![\\\\w.~-])/(?:private/var|var/folders)/'), literals=('/private/var/', '/var/folders/')), PathRule(rule='env-file', pattern=re.compile('(?<=[\\\\\\\\/])\\\\.env(?![\\\\w-]|\\\\.\\\\w)'), literals=('.env',)))*

The 0.1 path patterns as rules.

### liaise.detect.MAX_DESTINATION *= 2048*

How much of a link destination is read for its host.

### liaise.detect.MAX_TAG_LOOKBACK *= 1024*

How far back an attribute looks for the `<` of the tag it would belong to.

### liaise.detect.MIN_BASE64_RUN *= 100*

75 bytes of data.

* **Type:**
  The shortest base64 run that is a finding

### liaise.detect.MIN_HEX_RUN *= 129*

longer than a SHA-512 digest, so digests and
commit hashes quoted in a message are not findings.

* **Type:**
  The shortest hex run that is a finding

### liaise.detect.MIN_WRAPPED_BASE64_LINE *= 40*

The narrowest width of a wrapped base64 block (PEM wraps at 64, MIME at 76), and the
shortest unpadded last line that reads as the end of the data.

### *class* liaise.detect.Normalised(source, text, origins=None, ends=None, reading='', reading_origins=None)

Bases: [`View`](#liaise.detect.View)

A reading of a message folded for matching terms.

`reading` is the text that was folded (the message, or its rendering) and
`reading_origins[i]` the index in it of the character `text[i]` came from. Word
boundaries and spacing are judged in the reading, as its reader sees them.

#### spans(term, , whole_word=True)

Where `term` occurs, as `(start, end)` in `source`.

With `whole_word`, a match must not continue a word of the message on either side,
and must not span a word break the term does not have, unless every letter of it is
spaced apart. Occurrences of the same term do not overlap.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`int`](https://docs.python.org/3/builtins/functions.html#int), [`int`](https://docs.python.org/3/builtins/functions.html#int)]]

### liaise.detect.PERSONAL_SCANNERS *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Callable](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[[Scan](#liaise.detect.Scan)], [Iterable](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterable)[[Finding](#liaise.detect.Finding)]], ...]* *= (<function scan_personal_terms>, <function scan_email_addresses>)*

The checks `detect_personal` runs.

### liaise.detect.PERSON_PREFIX *= 'person:'*

How a vocabulary entry’s entity names a person.

### liaise.detect.PRIVATE_KEY_PATTERN *= '-----BEGIN (?:(?:[A-Z0-9]{1,16} ){0,4}PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----'*

PEM’s `-----BEGIN ... PRIVATE KEY-----`, its type’s words
bounded, or PGP’s `-----BEGIN PGP PRIVATE KEY BLOCK-----`.

* **Type:**
  A private key’s first line

### *class* liaise.detect.PathRule(rule, pattern, literals=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A local-path shape: `rule` names it in findings, `pattern` finds where it starts.

`literals`, as for [`SecretRule`](#liaise.detect.SecretRule): strings one of which every match contains.
A case-insensitive pattern has none, and is always scanned.

#### may_match(text)

Whether `text` holds one of `literals`, or the rule has none.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### liaise.detect.SECRET_RULES *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[SecretRule](#liaise.detect.SecretRule), ...]* *= (SecretRule(rule='github-token', pattern='gh[pousr]_[A-Za-z0-9]{20,}', literals=('ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_'), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='github-fine-grained-token', pattern='github_pat_\\\\w{20,}', literals=('github_pat_',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='sk-api-key', pattern='sk-[\\\\w-]{20,}', literals=('sk-',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='aws-access-key', pattern='AKIA[0-9A-Z]{16}\\\\b', literals=('AKIA',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='hugging-face-token', pattern='hf_[A-Za-z0-9]{30,}', literals=('hf_',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='slack-token', pattern='xox[baprs]-[A-Za-z0-9-]{10,}', literals=('xoxb-', 'xoxa-', 'xoxp-', 'xoxr-', 'xoxs-'), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='private-key', pattern='-----BEGIN (?:(?:[A-Z0-9]{1,16} ){0,4}PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----', literals=('-----BEGIN ',), word_start=False, wrappable=False, ignore_case=False), SecretRule(rule='gitlab-token', pattern='glpat-[\\\\w-]{20}', literals=('glpat-',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='google-api-key', pattern='AIza[\\\\w-]{35}', literals=('AIza',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='npm-token', pattern='(?i:npm_[a-z0-9]{36})', literals=('npm_',), word_start=True, wrappable=True, ignore_case=True), SecretRule(rule='pypi-token', pattern='pypi-AgEIcHlwaS5vcmc[\\\\w-]{50,1000}', literals=('pypi-AgEIcHlwaS5vcmc',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='sendgrid-api-key', pattern='SG\\\\.[A-Za-z0-9=_.-]{66}', literals=('SG.',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='age-secret-key', pattern='AGE-SECRET-KEY-1[QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L]{58}', literals=('AGE-SECRET-KEY-1',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='telegram-bot-token', pattern='[0-9]{5,16}:A[\\\\w-]{34}(?![\\\\w-])', literals=(':A',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='stripe-key', pattern='(?:sk|rk)_(?:test|live|prod)_[A-Za-z0-9]{10,99}', literals=('k_test_', 'k_live_', 'k_prod_'), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='shopify-token', pattern='shpat_[a-fA-F0-9]{32}', literals=('shpat_',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='digitalocean-token', pattern='dop_v1_[a-f0-9]{64}', literals=('dop_v1_',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='slack-webhook', pattern='(?:https?://)?hooks\\\\.slack\\\\.com/(?:services|workflows|triggers)/[A-Za-z0-9+/]{43,56}', literals=('hooks.slack.com/',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='slack-app-token', pattern='(?i:xapp-\\\\d-[A-Z0-9]{1,64}-\\\\d{1,16}-[a-z0-9]{1,128})', literals=('xapp-',), word_start=True, wrappable=True, ignore_case=True), SecretRule(rule='doppler-token', pattern='dp\\\\.pt\\\\.[A-Za-z0-9]{43}', literals=('dp.pt.',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='linear-api-key', pattern='lin_api_[A-Za-z0-9]{40}', literals=('lin_api_',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='hugging-face-org-token', pattern='api_org_[A-Za-z]{34}', literals=('api_org_',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='aws-access-key-id', pattern='(?:A3T[A-Z0-9]|ASIA|ABIA|ACCA)[A-Z2-7]{16}\\\\b', literals=('A3T', 'ASIA', 'ABIA', 'ACCA'), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='postman-api-key', pattern='PMAK-[A-Fa-f0-9]{24}-[A-Fa-f0-9]{34}', literals=('PMAK-',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='grafana-service-account-token', pattern='(?i:glsa_[a-z0-9]{32}_[a-f0-9]{8})', literals=('glsa_',), word_start=True, wrappable=True, ignore_case=True), SecretRule(rule='perplexity-api-key', pattern='pplx-[A-Za-z0-9]{48}', literals=('pplx-',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='sentry-user-token', pattern='sntryu_[a-f0-9]{64}', literals=('sntryu_',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='pulumi-token', pattern='pul-[a-f0-9]{40}', literals=('pul-',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='vault-token', pattern='hvs\\\\.[\\\\w-]{90,120}', literals=('hvs.',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='1password-service-account-token', pattern='ops_eyJ[A-Za-z0-9+/]{250,}={0,3}', literals=('ops_eyJ',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='jwt', pattern='ey[A-Za-z0-9]{17,4096}\\\\.ey[A-Za-z0-9/\\\\\\\\_-]{17,4096}\\\\.(?:[A-Za-z0-9/\\\\\\\\_-]{10,4096}={0,2})?', literals=('.ey',), word_start=True, wrappable=False, ignore_case=False))*

the 0.1 token shapes and
private-key header, then distinctive-prefix rules adapted from gitleaks (the gitleaks id
precedes each).

* **Type:**
  The secret rules [`secret_detector()`](#liaise.detect.secret_detector) uses by default

### liaise.detect.SEVERITY_WIDE_AUDIENCE *= 2*

The severity of any other finding, when the least-cleared reader has clearance
`clear` (a public or unknown audience), and when not.

### *class* liaise.detect.Scan(text, disclosure=<factory>, allowlist=(), canary_terms=(), personal_terms=(), key=<function fingerprint_key>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What detectors look at: the message, the disclosure and the terms, and the key.

`key` is the fingerprint key, or a callable that returns it; it is read the first
time a finding is made.

#### allows(host)

Whether `host` is an allowlisted host or a subdomain of one.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

#### *property* audience_severity *: [int](https://docs.python.org/3/builtins/functions.html#int)*

The severity of a finding that only the audience makes serious.

#### finding(kind, start, end, , rule, severity, entity=None, label=None, sealed_from=(), material=None)

A [`Finding`](#liaise.detect.Finding) for `text[start:end]`, fingerprinted as its kind says, or
over `material` when given.

* **Return type:**
  [`Finding`](#liaise.detect.Finding)

#### fingerprint(start, end, , fold, material=None)

The keyed fingerprint of `text[start:end]`: normalised with `fold`, else as
a reader sees it; or of `material` exactly, when a check names what it found.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

#### key(, key_file='fingerprint.key', key_bytes=32, create=True)

The fingerprint key in `<state_dir>/<key_file>`, created on first use.

`state_dir` defaults to the liaise config’s, else [`DFLT_STATE_DIR`](#liaise.detect.DFLT_STATE_DIR). A new key
is `key_bytes` random bytes in a file only its owner can read, put in place
atomically, so processes racing to create it all read the same key. A directory
created here is owner-only. An existing file shorter than `key_bytes` raises
[`FingerprintKeyError`](#liaise.detect.FingerprintKeyError).

With `create` false (a dry run, which writes nothing), a missing key is not created:
the answer is a key used once, so its fingerprints correlate with nothing recorded.

* **Return type:**
  [`bytes`](https://docs.python.org/3/builtins/stdtypes.html#bytes)

#### *property* least_clearance *: [str](https://docs.python.org/3/builtins/stdtypes.html#str)*

The least-cleared reader’s clearance; `clear` when the disclosure has none.

#### *property* normalised *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Normalised](#liaise.detect.Normalised), ...]*

as written, and rendered.

* **Type:**
  The readings of the message folded for matching terms

#### *property* readers *: [frozenset](https://docs.python.org/3/builtins/stdtypes.html#frozenset)[[str](https://docs.python.org/3/builtins/stdtypes.html#str)]*

the readers.

* **Type:**
  The ids of the people the disclosure was computed for

#### *property* rendered *: [View](#liaise.detect.View) | [None](https://docs.python.org/3/builtins/constants.html#None)*

The message as a Markdown or HTML reader sees it, when that differs.

#### spans(term, , whole_word=True)

Where `term` occurs in any reading of the message, in order.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`int`](https://docs.python.org/3/builtins/functions.html#int), [`int`](https://docs.python.org/3/builtins/functions.html#int)]]

### *class* liaise.detect.SecretRule(rule, pattern, literals=(), word_start=True, wrappable=True, ignore_case=False)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A secret’s shape: `rule` names it in findings, `pattern` is its expression.

`literals`: strings one of which every match contains (in case-folded text, with
`ignore_case`). A text holding none is not scanned for the rule, which matters because
a pattern that starts at a word boundary cannot use the regular-expression engine’s
fast search for a literal prefix. Empty: always scanned.

`word_start`: a match must start a word. `wrappable`: the rule is also looked for in
the message with line breaks, invisible characters and emphasis marks removed, so a
token split by them is found; its word boundary is then checked against the message.
A wrappable pattern must not fail after an unbounded repetition, or that scan, which
has no word boundary to anchor it, turns quadratic.

#### *property* in_stripped *: [Pattern](https://docs.python.org/3/library/re.html#re.Pattern)*

The pattern as it is looked for in the message with the strippable removed.

#### *property* in_text *: [Pattern](https://docs.python.org/3/library/re.html#re.Pattern)*

The pattern as it is looked for in the message.

#### may_match(text)

Whether `text` holds one of `literals`, or the rule has none.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### liaise.detect.TOKEN_LITERALS *= (('ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_'), ('github_pat_',), ('sk-',), ('AKIA',), ('hf_',), ('xoxb-', 'xoxa-', 'xoxp-', 'xoxr-', 'xoxs-'))*

The literals every match of each of [`TOKEN_SHAPES`](#liaise.detect.TOKEN_SHAPES) contains, in order.

### liaise.detect.TOKEN_RULES *= ('github-token', 'github-fine-grained-token', 'sk-api-key', 'aws-access-key', 'hugging-face-token', 'slack-token')*

The rule id of each of [`TOKEN_SHAPES`](#liaise.detect.TOKEN_SHAPES), in order.

### liaise.detect.TOKEN_SHAPES *= ('gh[pousr]_[A-Za-z0-9]{20,}', 'github_pat_\\\\w{20,}', 'sk-[\\\\w-]{20,}', 'AKIA[0-9A-Z]{16}\\\\b', 'hf_[A-Za-z0-9]{30,}', 'xox[baprs]-[A-Za-z0-9-]{10,}')*

the
GitHub (`ghp_` and its siblings, `github_pat_`), `sk-` API key, AWS access key,
Hugging Face (`hf_`) and Slack (`xoxb-` and its siblings) shapes.

* **Type:**
  The token shapes of the 0.1 leak scan, each without the word boundary it starts at

### *class* liaise.detect.View(source, text, origins=None, ends=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

`text` derived from `source`, and where each of its characters came from.

`text[i]` came from `source[start_of(i):end_of(i)]`. `origins` is None when
`text` is `source`; `ends` is None when each character came from one character.

#### derive(text, origins, ends=None)

A view of `text`, whose characters came from this view’s text at `origins`
(to `ends`, or one character each), mapped back to this view’s source.

* **Return type:**
  [`View`](#liaise.detect.View)

#### end_of(index)

Where the source of `text[index]` ends.

* **Return type:**
  [`int`](https://docs.python.org/3/builtins/functions.html#int)

#### source_span(start, end)

The span of `source` that `text[start:end]` came from.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`int`](https://docs.python.org/3/builtins/functions.html#int), [`int`](https://docs.python.org/3/builtins/functions.html#int)]

#### start_of(index)

Where the source of `text[index]` starts.

* **Return type:**
  [`int`](https://docs.python.org/3/builtins/functions.html#int)

### liaise.detect.chain(\*detectors, name='chained')

One detector that yields the findings of `detectors`, in order.

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[[`Scan`](#liaise.detect.Scan)], [`Iterable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterable)[[`Finding`](#liaise.detect.Finding)]]

### liaise.detect.detect(text, \*, disclosure, allowlist=(), canary_terms=(), personal_terms=(), key=None, detectors=(<function secret_detector.<locals>.detect_secrets>, <function detect_canaries>, <function detect_vocabulary>, <function chain.<locals>.chained>, <function chain.<locals>.chained>, <function detect_third_parties>))

What `detectors` find in `text`, ordered by position, each finding once.

`disclosure` is the JSON of `acquaint.disclosure` (its `people`,
`least_clearance` and `vocabulary` are read; `{}` when there is none).
`allowlist` holds the hosts a link may point at, subdomains included.
`canary_terms` and `personal_terms` are the subject’s canaries and the operator’s
own addresses, handles and paths. `key` is the fingerprint key (or a callable that
returns it); by default [`fingerprint_key()`](#liaise.detect.fingerprint_key) with the configured state directory,
read only when there is a finding. A caller with a config in hand should pass it.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`Finding`](#liaise.detect.Finding), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

```pycon
>>> key = b"k" * 32
>>> [f.kind for f in detect("Use " + "ghp_" + "a" * 36, disclosure={}, key=key)]
['secret']
>>> vocabulary = [{"term": "Heron", "entity": "project:heron", "label": "amber"}]
>>> found = detect("Is he-RON late?", disclosure={"vocabulary": vocabulary}, key=key)
>>> [(f.kind, f.start, f.end, f.severity) for f in found]
[('vocabulary', 3, 9, 3)]
```

### liaise.detect.detect_canaries(scan)

A `canary` finding, severity 5, wherever a canary term occurs, even inside a word:
a canary is unique by construction, so a match anywhere is the alarm.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Finding`](#liaise.detect.Finding)]

### liaise.detect.detect_third_parties(scan)

A `third_party` finding for each whole-word occurrence of a person’s disclosure
term, when that person is not a reader.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Finding`](#liaise.detect.Finding)]

### liaise.detect.detect_vocabulary(scan)

A `vocabulary` finding for each whole-word occurrence of a disclosure term whose
entity is not a person.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Finding`](#liaise.detect.Finding)]

### liaise.detect.fingerprint_key(state_dir=None, , key_file='fingerprint.key', key_bytes=32, create=True)

The fingerprint key in `<state_dir>/<key_file>`, created on first use.

`state_dir` defaults to the liaise config’s, else [`DFLT_STATE_DIR`](#liaise.detect.DFLT_STATE_DIR). A new key
is `key_bytes` random bytes in a file only its owner can read, put in place
atomically, so processes racing to create it all read the same key. A directory
created here is owner-only. An existing file shorter than `key_bytes` raises
[`FingerprintKeyError`](#liaise.detect.FingerprintKeyError).

With `create` false (a dry run, which writes nothing), a missing key is not created:
the answer is a key used once, so its fingerprints correlate with nothing recorded.

* **Return type:**
  [`bytes`](https://docs.python.org/3/builtins/stdtypes.html#bytes)

### liaise.detect.fold_term(term)

`term` folded as [`normalise()`](#liaise.detect.normalise) folds a message, with its word breaks.

* **Return type:**
  [`FoldedTerm`](#liaise.detect.FoldedTerm)

### liaise.detect.key_source(state_dir=None, , key_file='fingerprint.key', key_bytes=32, create=True)

A callable that answers one key, however often it is asked: [`fingerprint_key()`](#liaise.detect.fingerprint_key), once.

One command may judge a message more than once — releasing a draft judges it, shows the
operator, and judges it again with their approval — and those judgements have to
fingerprint alike, or what the second one flags is not what the first one showed. With
`create` false a missing key is a key used once, so asking twice would otherwise
answer twice.

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[], [`bytes`](https://docs.python.org/3/builtins/stdtypes.html#bytes)]

```pycon
>>> source = key_source(create=False)
>>> source() == source()
True
```

### liaise.detect.link_urls(text)

Every link and image destination in `text`, in full, in order, each once.

Read as [`scan_links()`](#liaise.detect.scan_links) reads them (Markdown, HTML attributes, autolinks, plain
URLs, then any `//host` outside those), whatever their host: what the operator reads
before releasing a message, since a link’s title can say one place and its
destination another.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

```pycon
>>> link_urls("See [the docs](https://example.org/a) and https://example.com/b.")
('https://example.org/a', 'https://example.com/b')
```

### liaise.detect.local_path_scanner(rules=(PathRule(rule='local-path', pattern=re.compile('(?<![\\\\\\\\w.~-])/(?:Users|home|root)/'), literals=('/Users/', '/home/', '/root/')), PathRule(rule='local-path', pattern=re.compile('\\\\\\\\b[A-Za-z]:(?:\\\\\\\\\\\\\\\\{1,2}|/)Users(?:\\\\\\\\\\\\\\\\{1,2}|/)', re.IGNORECASE), literals=()), PathRule(rule='local-path', pattern=re.compile('(?<![\\\\\\\\w.~-])/mnt/[A-Za-z]/Users/', re.IGNORECASE), literals=()), PathRule(rule='local-path', pattern=re.compile('(?<![\\\\\\\\w.~-])/(?:private/var|var/folders)/'), literals=('/private/var/', '/var/folders/')), PathRule(rule='env-file', pattern=re.compile('(?<=[\\\\\\\\\\\\\\\\/])\\\\\\\\.env(?![\\\\\\\\w-]|\\\\\\\\.\\\\\\\\w)'), literals=('.env',))))

A check for `rules`: an `exfiltration` finding spanning each path, from where
its rule matched to the end of the path. A match inside a path already found by the
same rule is part of that path.

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[[`Scan`](#liaise.detect.Scan)], [`Iterable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterable)[[`Finding`](#liaise.detect.Finding)]]

### liaise.detect.normalise(text)

Fold `text` for matching, keeping where each folded character came from.

* **Return type:**
  [`Normalised`](#liaise.detect.Normalised)

```pycon
>>> folded = normalise("Ｈｅ\u200b-Ron!")
>>> folded.text
'heron!'
>>> list(folded.spans(fold_term("Heron")))
[(0, 7)]
```

### liaise.detect.render(text)

`text` as a Markdown or HTML reader sees it, or None when rendering changes nothing.

Comments (to `-->`, or to the end of the text when unclosed), tags, processing
instructions, declarations and backslash escapes are removed, a tag of a block
element or a line break reading as a space; character references and percent-escapes
are decoded. Each piece of markup is read once, so rendering is linear in the text.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`View`](#liaise.detect.View)]

```pycon
>>> render("He<b></b>r&#111;n%21").text
'Heron!'
>>> render("on<br>a").text
'on a'
```

### liaise.detect.scan_base64_runs(scan)

An `exfiltration` finding for each base64 or base64url run of at least
[`MIN_BASE64_RUN`](#liaise.detect.MIN_BASE64_RUN) characters, on one line or wrapped over several (indented or
quoted too), that mixes digits, capitals and small letters, which encoded data does and
a long word or path rarely does.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Finding`](#liaise.detect.Finding)]

### liaise.detect.scan_email_addresses(scan)

A `personal` finding for each email address.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Finding`](#liaise.detect.Finding)]

### liaise.detect.scan_hex_runs(scan)

An `exfiltration` finding for each run of at least [`MIN_HEX_RUN`](#liaise.detect.MIN_HEX_RUN) hex digits
that mixes digits and letters.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Finding`](#liaise.detect.Finding)]

### liaise.detect.scan_invisible_characters(scan)

An `exfiltration` finding for each word holding invisible characters the text does
not need to render (see `_needed_invisible()`): zero-width spaces, direction
overrides, tag characters, variation selectors used to carry data.

The runs within one word (no whitespace between them) are one finding, spanning them,
fingerprinted over the invisible characters alone: a word stuffed with them is one
thing to show the operator, and a message of them costs one finding, not thousands.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Finding`](#liaise.detect.Finding)]

### liaise.detect.scan_links(scan)

An `exfiltration` finding for each link or image whose host is not allowlisted.

Destinations are read in Markdown (inline and reference), HTML attributes, autolinks
and plain text, each as a browser resolves it; a destination is found when any
reading of it names a host outside the allowlist, or an authority whose host cannot
be read. Then every `//host` anywhere (`_loose_urls()`) not inside a URL
already found. The rule is `image-host` when the URL loads without a click (a
Markdown image, or an attribute such as `src`), else `link-host`. The finding
spans the URL.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Finding`](#liaise.detect.Finding)]

### liaise.detect.scan_personal_terms(scan)

A `personal` finding for each whole-word occurrence of a personal term.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Finding`](#liaise.detect.Finding)]

### liaise.detect.scan_private_addresses(scan)

An `exfiltration` finding for each IPv4 or IPv6 address in
[`INTERNAL_NETWORKS`](#liaise.detect.INTERNAL_NETWORKS).

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Finding`](#liaise.detect.Finding)]

### liaise.detect.secret_detector(rules=(SecretRule(rule='github-token', pattern='gh[pousr]_[A-Za-z0-9]{20,}', literals=('ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_'), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='github-fine-grained-token', pattern='github_pat_\\\\\\\\w{20,}', literals=('github_pat_',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='sk-api-key', pattern='sk-[\\\\\\\\w-]{20,}', literals=('sk-',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='aws-access-key', pattern='AKIA[0-9A-Z]{16}\\\\\\\\b', literals=('AKIA',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='hugging-face-token', pattern='hf_[A-Za-z0-9]{30,}', literals=('hf_',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='slack-token', pattern='xox[baprs]-[A-Za-z0-9-]{10,}', literals=('xoxb-', 'xoxa-', 'xoxp-', 'xoxr-', 'xoxs-'), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='private-key', pattern='-----BEGIN (?:(?:[A-Z0-9]{1,16} ){0,4}PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----', literals=('-----BEGIN ',), word_start=False, wrappable=False, ignore_case=False), SecretRule(rule='gitlab-token', pattern='glpat-[\\\\\\\\w-]{20}', literals=('glpat-',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='google-api-key', pattern='AIza[\\\\\\\\w-]{35}', literals=('AIza',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='npm-token', pattern='(?i:npm_[a-z0-9]{36})', literals=('npm_',), word_start=True, wrappable=True, ignore_case=True), SecretRule(rule='pypi-token', pattern='pypi-AgEIcHlwaS5vcmc[\\\\\\\\w-]{50,1000}', literals=('pypi-AgEIcHlwaS5vcmc',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='sendgrid-api-key', pattern='SG\\\\\\\\.[A-Za-z0-9=_.-]{66}', literals=('SG.',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='age-secret-key', pattern='AGE-SECRET-KEY-1[QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L]{58}', literals=('AGE-SECRET-KEY-1',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='telegram-bot-token', pattern='[0-9]{5,16}:A[\\\\\\\\w-]{34}(?![\\\\\\\\w-])', literals=(':A',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='stripe-key', pattern='(?:sk|rk)_(?:test|live|prod)_[A-Za-z0-9]{10,99}', literals=('k_test_', 'k_live_', 'k_prod_'), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='shopify-token', pattern='shpat_[a-fA-F0-9]{32}', literals=('shpat_',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='digitalocean-token', pattern='dop_v1_[a-f0-9]{64}', literals=('dop_v1_',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='slack-webhook', pattern='(?:https?://)?hooks\\\\\\\\.slack\\\\\\\\.com/(?:services|workflows|triggers)/[A-Za-z0-9+/]{43,56}', literals=('hooks.slack.com/',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='slack-app-token', pattern='(?i:xapp-\\\\\\\\d-[A-Z0-9]{1,64}-\\\\\\\\d{1,16}-[a-z0-9]{1,128})', literals=('xapp-',), word_start=True, wrappable=True, ignore_case=True), SecretRule(rule='doppler-token', pattern='dp\\\\\\\\.pt\\\\\\\\.[A-Za-z0-9]{43}', literals=('dp.pt.',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='linear-api-key', pattern='lin_api_[A-Za-z0-9]{40}', literals=('lin_api_',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='hugging-face-org-token', pattern='api_org_[A-Za-z]{34}', literals=('api_org_',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='aws-access-key-id', pattern='(?:A3T[A-Z0-9]|ASIA|ABIA|ACCA)[A-Z2-7]{16}\\\\\\\\b', literals=('A3T', 'ASIA', 'ABIA', 'ACCA'), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='postman-api-key', pattern='PMAK-[A-Fa-f0-9]{24}-[A-Fa-f0-9]{34}', literals=('PMAK-',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='grafana-service-account-token', pattern='(?i:glsa_[a-z0-9]{32}_[a-f0-9]{8})', literals=('glsa_',), word_start=True, wrappable=True, ignore_case=True), SecretRule(rule='perplexity-api-key', pattern='pplx-[A-Za-z0-9]{48}', literals=('pplx-',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='sentry-user-token', pattern='sntryu_[a-f0-9]{64}', literals=('sntryu_',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='pulumi-token', pattern='pul-[a-f0-9]{40}', literals=('pul-',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='vault-token', pattern='hvs\\\\\\\\.[\\\\\\\\w-]{90,120}', literals=('hvs.',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='1password-service-account-token', pattern='ops_eyJ[A-Za-z0-9+/]{250,}={0,3}', literals=('ops_eyJ',), word_start=True, wrappable=True, ignore_case=False), SecretRule(rule='jwt', pattern='ey[A-Za-z0-9]{17,4096}\\\\\\\\.ey[A-Za-z0-9/\\\\\\\\\\\\\\\\_-]{17,4096}\\\\\\\\.(?:[A-Za-z0-9/\\\\\\\\\\\\\\\\_-]{10,4096}={0,2})?', literals=('.ey',), word_start=True, wrappable=False, ignore_case=False)))

A detector of `rules`: a `secret` finding for each match, severity 5.

Each rule is looked for in the message and, when it holds markup, in its rendering;
each wrappable rule also in both with line breaks, invisible characters and emphasis
marks removed. A token found in several of these is one finding, as long as the
longest match.

* **Return type:**
  [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[[`Scan`](#liaise.detect.Scan)], [`Iterable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterable)[[`Finding`](#liaise.detect.Finding)]]

### liaise.detect.visible(text)

`text` with each invisible or control character written as `<U+XXXX>`.

What the operator reads before releasing a message: a zero-width space, a direction
override or a terminal escape shows as what it is, where it is.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> visible("He" + chr(0x200B) + "ron")
'He<U+200B>ron'
>>> visible("two\nlines\tand a tab")
'two\nlines\tand a tab'
```
