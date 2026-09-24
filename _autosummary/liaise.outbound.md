# liaise.outbound

What the outbound gate’s policy filter gathers before the policy decides.

[`liaise.policy.evaluate()`](liaise.policy.md#liaise.policy.evaluate) is pure: it takes a message, an audience, a disclosure, the
findings and the run’s provenance, and returns a verdict. This module gathers those inputs
for one message (liaise discussion 32, §5.3 and §5.6; liaise ADR 0002), and
[`liaise.gate.outbound_policy()`](liaise.gate.md#liaise.gate.outbound_policy) hands them over through [`judge()`](#liaise.outbound.judge):

- **The audience** is what the gate’s context carries, correspond’s record for the
  destination. None is unknown, which resolves to public ([`audience_snapshot()`](#liaise.outbound.audience_snapshot)).
- **The disclosure** comes through the `disclosure=` seam: [`acquaint_disclosure()`](#liaise.outbound.acquaint_disclosure),
  which is `acquaint.disclosure` when acquaint imports and, without it, every reader at
  `need-to-know` ([`need_to_know_disclosure()`](#liaise.outbound.need_to_know_disclosure)). The subject’s `policy.leak_terms`
  join its vocabulary as a label no reader is cleared for ([`with_leak_terms()`](#liaise.outbound.with_leak_terms)).
- **The readers’ identities**: each copy and listed reader of the audience, resolved to a
  person by the subject’s resolver ([`identities_for()`](#liaise.outbound.identities_for)).
- **The findings** of the detectors, over the text, the title and each attachment name
  ([`findings_in()`](#liaise.outbound.findings_in)).
- **The provenance**: what the run that wrote the message read. [`case_provenance()`](#liaise.outbound.case_provenance)
  judges a case’s messages by the access pairs the ledger records.

After a message goes out, [`record_disclosure()`](#liaise.outbound.record_disclosure) appends an `interaction` entry to each
recipient’s acquaint record, naming the labelled records the message identified, never its
text (discussion §4.8).

Nothing here sends, and nothing here imports the gate.

### Module Attributes

| [`LEAK_TERMS_ENTITY`](#liaise.outbound.LEAK_TERMS_ENTITY)   | the most restrictive, so no reader of any channel but the operator's own devices is cleared for it, as the 0.1 leak scan held a leak term back from every public channel.   |
|----------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`NEED_TO_KNOW`](#liaise.outbound.NEED_TO_KNOW)        | the default tier.                                                                                                                                                           |
| [`FROM_ACQUAINT`](#liaise.outbound.FROM_ACQUAINT)       | Where a disclosure came from, as the ledger records it.                                                                                                                     |
| [`REQUEST_WORK`](#liaise.outbound.REQUEST_WORK)        | The permission a message's author must hold, at a grade it accepts, for a run that read the message to count as clean (discussion decision 10).                             |
| [`MESSAGE_KIND`](#liaise.outbound.MESSAGE_KIND)        | The ledger entry kind a case's inbound messages are recorded as.                                                                                                            |
| [`CHANNEL_HOSTS`](#liaise.outbound.CHANNEL_HOSTS)       | The hosts a link may point at on a channel, beside the subject's `link_allowlist`.                                                                                          |
| [`IDENTIFYING_KINDS`](#liaise.outbound.IDENTIFYING_KINDS)   | The finding kinds that identify a labelled record, and so what "already told" counts.                                                                                       |
| [`DISCLOSURE_SOURCE`](#liaise.outbound.DISCLOSURE_SOURCE)   | Who an `interaction` entry liaise appends to an acquaint record is sourced to.                                                                                              |
| [`TITLE_PART`](#liaise.outbound.TITLE_PART)          | How a finding names the part of a message it was found in, when that is not the text.                                                                                       |
| [`DisclosureSource`](#liaise.outbound.DisclosureSource)    | the `disclosure=` seam.                                                                                                                                                     |

### Functions

| [`acquaint_disclosure`](#liaise.outbound.acquaint_disclosure)(people, \*[, projects, ...])   | What each of `people` may be told, from acquaint; everyone at `need-to-know` without it.             |
|-----------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------|
| [`allowlist_for`](#liaise.outbound.allowlist_for)(outbound, subject)                   | The hosts a link in `outbound` may point at: its channel's own and `policy.link_allowlist`.          |
| [`audience_snapshot`](#liaise.outbound.audience_snapshot)(audience, ref)                   | `audience` as the policy and the hashes see it; None is the unknown audience of `ref`.               |
| [`case_provenance`](#liaise.outbound.case_provenance)(case, subject, ledger)             | Whether a run on `case` read anything `subject` does not trust for `request_work`.                   |
| [`consulted_of`](#liaise.outbound.consulted_of)(disclosure, \*, identities, ...)      | What the ledger keeps of a judgement's inputs: the labels and seals consulted, never a term.         |
| [`disclosed_records`](#liaise.outbound.disclosed_records)(verdict)                         | The labelled records `verdict`'s message identified, each once, in order: never a term.              |
| [`findings_in`](#liaise.outbound.findings_in)(outbound, disclosure, \*, subject)     | What `detectors` find in the text, the title and each attachment name of `outbound`.                 |
| [`identities_for`](#liaise.outbound.identities_for)(outbound, audience, \*, subject)    | The person each copy and each listed reader of `audience` resolves to on `subject`; None for nobody. |
| [`judge`](#liaise.outbound.judge)(outbound, \*, subject, now[, audience, ...]) | The policy's verdict on `outbound`, sent to its destination on `subject`, with what it consulted.    |
| [`need_to_know_disclosure`](#liaise.outbound.need_to_know_disclosure)(people, \*[, today])       | A disclosure without acquaint: every named reader at `need-to-know`, cleared to `clear`.             |
| [`record_disclosure`](#liaise.outbound.record_disclosure)(outbound, verdict, consulted)    | Append an `interaction` entry to each recipient's acquaint record naming what `outbound` identified. |
| [`verdict_id`](#liaise.outbound.verdict_id)(verdict)                                | A name for what an approval of `verdict` releases: the rules that fired and what they found.         |
| [`with_leak_terms`](#liaise.outbound.with_leak_terms)(disclosure, leak_terms)            | `disclosure` with each of `leak_terms` added to its vocabulary, at `LEAK_TERMS_LABEL`.               |

### Classes

| [`Judgement`](#liaise.outbound.Judgement)(verdict, consulted[, notes])   | What [`judge()`](#liaise.outbound.judge) decided about one message, and what it consulted to decide.   |
|-------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------|

### liaise.outbound.CHANNEL_HOSTS *: [Mapping](https://docs.python.org/3/library/collections.abc.html#collections.abc.Mapping)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), ...]]* *= mappingproxy({'github': ('github.com',)})*

The hosts a link may point at on a channel, beside the subject’s `link_allowlist`.

### liaise.outbound.DISCLOSURE_SOURCE *= 'liaise'*

Who an `interaction` entry liaise appends to an acquaint record is sourced to.

### liaise.outbound.DisclosureSource

the `disclosure=` seam. The
answer is `acquaint.disclosure`’s JSON; `audience` is correspond’s record, as a dict.

* **Type:**
  `(people, *, projects, audience, today) -> disclosure`

alias of [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[…], [`Mapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Mapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]]

### liaise.outbound.FROM_ACQUAINT *= 'acquaint'*

Where a disclosure came from, as the ledger records it.

### liaise.outbound.IDENTIFYING_KINDS *= frozenset({'third_party', 'vocabulary'})*

The finding kinds that identify a labelled record, and so what “already told” counts.

### *class* liaise.outbound.Judgement(verdict, consulted, notes=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What [`judge()`](#liaise.outbound.judge) decided about one message, and what it consulted to decide.

`verdict` is the policy’s. `consulted` is what the ledger keeps of the inputs
([`consulted_of()`](#liaise.outbound.consulted_of)), and `notes` the lines the operator reads beside the verdict:
the audience in words, and the rules that fired without holding the message back.

### liaise.outbound.LEAK_TERMS_ENTITY *= 'policy.leak_terms'*

the most restrictive,
so no reader of any channel but the operator’s own devices is cleared for it, as the 0.1
leak scan held a leak term back from every public channel.

* **Type:**
  The entity `policy.leak_terms` are scanned as, and its label

### liaise.outbound.MESSAGE_KIND *= 'message'*

The ledger entry kind a case’s inbound messages are recorded as.

### liaise.outbound.NEED_TO_KNOW *= 'need-to-know'*

the default tier.

* **Type:**
  A reader’s standing when nobody can say more (discussion §4.1)

### liaise.outbound.REQUEST_WORK *= 'request_work'*

The permission a message’s author must hold, at a grade it accepts, for a run that read
the message to count as clean (discussion decision 10).

### liaise.outbound.TITLE_PART *= 'title'*

How a finding names the part of a message it was found in, when that is not the text.

### liaise.outbound.acquaint_disclosure(people, , projects=(), audience=None, today=None)

What each of `people` may be told, from acquaint; everyone at `need-to-know` without it.

The default of the `disclosure=` seam. acquaint that imports and then fails raises,
so the gate flags the message rather than judging it with less than it should know.

* **Return type:**
  [`Mapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Mapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### liaise.outbound.allowlist_for(outbound, subject)

The hosts a link in `outbound` may point at: its channel’s own and `policy.link_allowlist`.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

### liaise.outbound.audience_snapshot(audience, ref)

`audience` as the policy and the hashes see it; None is the unknown audience of `ref`.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

```pycon
>>> snapshot = audience_snapshot(None, "github:example/app#12")
>>> snapshot["scope"], snapshot["defaulted"], snapshot["ref"]
('public', True, 'github:example/app#12')
```

### liaise.outbound.case_provenance(case, subject, ledger)

Whether a run on `case` read anything `subject` does not trust for `request_work`.

Every `message` entry of the case counts, not only those a run had read when it
started: the ledger does not say which a resumed session saw, and counting one it did
not can only hold a message back. A message is trusted when the channel’s own account
wrote it, or its author has a role that grants `request_work` at the grade it was
received at.

`ledger` is read for the same reason: a message whose author has no role at all is
refused at intake and queued as unrouted, so it is on the conversation the run reads
and on no entry of the case. It counts too, which is what makes the rule cover the
stranger it exists for (liaise discussion 32, §5.3).

* **Return type:**
  [`Provenance`](liaise.policy.md#liaise.policy.Provenance)

```pycon
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
```

### liaise.outbound.consulted_of(disclosure, , identities, provenance)

What the ledger keeps of a judgement’s inputs: the labels and seals consulted, never a term.

The tiers and clearances of the readers are the verdict’s own (`Verdict.readers`).

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### liaise.outbound.disclosed_records(verdict)

The labelled records `verdict`’s message identified, each once, in order: never a term.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

### liaise.outbound.findings_in(outbound, disclosure, \*, subject, key=None, detectors=(<function secret_detector.<locals>.detect_secrets>, <function detect_canaries>, <function detect_vocabulary>, <function chain.<locals>.chained>, <function chain.<locals>.chained>, <function detect_third_parties>, <function detect_link_terms>))

What `detectors` find in the text, the title and each attachment name of `outbound`.

A finding’s offsets are into the part it was found in, which its `part` names when
that is not the text.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`Finding`](liaise.detect.md#liaise.detect.Finding), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

### liaise.outbound.identities_for(outbound, audience, \*, subject, resolver=<function resolve_person>)

The person each copy and each listed reader of `audience` resolves to on `subject`; None for nobody.

`audience` is a snapshot ([`audience_snapshot()`](#liaise.outbound.audience_snapshot)). A resolver that raises resolves
to nobody, which the policy counts at `clear`.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]]

### liaise.outbound.judge(outbound, \*, subject, now, audience=None, provenance=None, mode=None, key=None, disclosure=<function acquaint_disclosure>, detectors=(<function secret_detector.<locals>.detect_secrets>, <function detect_canaries>, <function detect_vocabulary>, <function chain.<locals>.chained>, <function chain.<locals>.chained>, <function detect_third_parties>, <function detect_link_terms>), resolver=<function resolve_person>)

The policy’s verdict on `outbound`, sent to its destination on `subject`, with what it consulted.

`audience` is correspond’s record for the destination (None: unknown, so public).
`provenance` is the run’s (None: unknown, so tainted). `mode` overrides the
subject’s `policy.mode`. `key` is the fingerprint key (see
[`liaise.detect.detect()`](liaise.detect.md#liaise.detect.detect)). `disclosure`, `detectors` and `resolver` are the
seams of those names. The disclosure is asked for exactly this message’s readers: its
recipient and copies by name, and the audience’s listed readers through the record.

* **Return type:**
  [`Judgement`](#liaise.outbound.Judgement)

### liaise.outbound.need_to_know_disclosure(people, , today=None)

A disclosure without acquaint: every named reader at `need-to-know`, cleared to `clear`.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

```pycon
>>> need_to_know_disclosure(["ada"])["people"]["ada"]["clearance"]
'clear'
```

### liaise.outbound.record_disclosure(outbound, verdict, consulted)

Append an `interaction` entry to each recipient’s acquaint record naming what `outbound` identified.

The recipients are the message’s recipient and every copy that resolved to a person.
Nothing is written when the message identified no labelled record, or acquaint does not
import. Returns why a record could not be written, or None: the message has gone out,
so a failure here is for the operator to read, never a reason to send again.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.outbound.verdict_id(verdict)

A name for what an approval of `verdict` releases: the rules that fired and what they found.

Each reason’s rule, flow and reader, the fingerprint and entity of the finding it names
(never its value), and the two hashes an approval binds to. The time the verdict was
made is left out, so the same message judged twice has the same name — and a verdict
that flags something else does not, which is how an approval given for one finding
cannot settle another (liaise ADR 0002).

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.outbound.with_leak_terms(disclosure, leak_terms)

`disclosure` with each of `leak_terms` added to its vocabulary, at `LEAK_TERMS_LABEL`.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

```pycon
>>> found = with_leak_terms({"vocabulary": []}, ["the-bird-board", " "])["vocabulary"]
>>> [(term["term"], term["label"]) for term in found]
[('the-bird-board', 'red')]
```
