# liaise.policy

Policy and verdict for outbound messages: from findings and an audience to a flow.

[`evaluate()`](#liaise.policy.evaluate) is a pure function. It takes what the other packages and the detectors
established (liaise discussion 32, §1): the message, the `Audience` of its destination
(correspond’s record, as JSON), the `Disclosure` for its readers (acquaint’s record, as
JSON), the [`Finding`](liaise.detect.md#liaise.detect.Finding) records the detectors made, the run’s
[`Provenance`](#liaise.policy.Provenance) and the [`OutboundPolicy`](#liaise.policy.OutboundPolicy) in force, and returns a
[`Verdict`](#liaise.policy.Verdict): a flow, the route the operator sees, every rule that fired with a
sentence they can read, the five axes the decision was made on, and the two hashes an
approval binds to. It imports nothing from correspond or acquaint, reads no file, no clock
and no network, and returns the same verdict for the same inputs.

**Flows and routes** (discussion §5.5). Flows order by restriction: `send` < `delay` <
`revise` < `approve` < `approve_twice` < `refuse`. Three routes: `send` (sent
now, or held in the outbox for `delay`), `draft` (a flagged draft for the operator, or
back to the processor for `revise`), `block` (a draft that cannot be released as
written). `approve_twice` keeps its place in the order and is never produced (decision
9).

**The rule table** ([`RULES`](#liaise.policy.RULES), discussion §5.4) is declared data: a tuple of
[`Rule`](#liaise.policy.Rule) records, each a name, a predicate over the [`Facts`](#liaise.policy.Facts) and a minimum flow.
Every rule is evaluated; the verdict’s flow is the most restrictive that fired, and the
reasons are every hit, most restrictive first (decision 5). Two rows take their flow from a
condition the table states: *no write-down* and *co-ownership* are `revise` when the case
can resume and `approve` otherwise. Two decisions of this slice, recorded here because the
table and the scenario suite of research §9.3 disagree without them:

- *Exfiltration* refuses every exfiltration shape but a plain link: an image loads without a
  click, an encoded run or an invisible character carries data, a private address or a
  local path names the operator’s machine; a link a reader has to follow is shown to the
  operator in full and is `approve` (S22 must deliver; S21 must not).
- *Taint* is `approve` for a tainted or unknown run reaching past the operator, and
  `refuse` when that run’s message also names something the audience is not cleared
  for: an injection that got private content out is never released as written (S9).

The rows *no write-down* and *co-ownership* partition the labelled findings: a project’s,
organisation’s or group’s term is judged against the least-cleared reader; a person’s term
(`third_party`) reader by reader, so the reason names who is not cleared for whom. The
row *unknown audience* sets no flow: it explains that the ceiling is `clear`.

**The least-cleared reader** ([`least_cleared_reader()`](#liaise.policy.least_cleared_reader), discussion §4.4): `clear`
for a public or defaulted audience; for `org` and `group` with `complete` false, the
organisation’s recorded clearance (the disclosure’s `audience.ceiling`) else `clear`,
then the minimum over the resolved readers; for `named`, the minimum over the resolved
readers, an unresolved identity or recipient at `clear`; for `operator`, no ceiling.
Readers are the disclosure’s `people` and whatever `identities` resolves, each at
their `clearance`; a resolved person the disclosure does not know is at `clear`.

**Hashes.** [`payload_hash()`](#liaise.policy.payload_hash) is SHA-256 over the canonical JSON of the recipients,
`cc`, `bcc`, the reference, the title, the exact text and the attachment names
([`payload_of()`](#liaise.policy.payload_of)). [`audience_hash()`](#liaise.policy.audience_hash) is SHA-256 over the canonical JSON of the
audience record without `as_of` and `evidence`, the construction correspond’s
`Audience.hash` uses: canonical JSON is `json.dumps(data, sort_keys=True,
separators=(",", ":"))` with Python’s default ASCII escaping, encoded UTF-8, and the
record is normalised as correspond stores it (readers deduplicated and sorted by their
canonical JSON, with their derived `address`; classes, durability and widening sorted).
That equality is a cross-package contract, pinned by a fixture in the tests. Approvals
bind to both hashes (discussion §5.7).

**Inputs.** `outbound` is any object or mapping with `ref`, `channel`,
`recipient` (a person id or an address) and `text`, and optionally `title`, `cc`,
`bcc`, `attachments` and `case_id` ([`liaise.gate.Outbound`](liaise.gate.md#liaise.gate.Outbound) is one).
`audience` is the JSON of a correspond `Audience` (or an object with `to_dict`);
`None` is unknown, which resolves to public. `disclosure` is the JSON of
`acquaint.disclosure`; `{}` when there is none. `findings` are
[`Finding`](liaise.detect.md#liaise.detect.Finding) records or their dicts. `identities` maps an address
(a `cc` entry, a listed reader) to the person id it resolved to, or `None` when it
did not: identity resolution happens before policy (research §4.6), and this is where
its answer comes in. A `cc` or `bcc` entry that is neither a person of the disclosure
nor resolved by `identities` is a stranger. Every person of the disclosure is a
reader, so the disclosure must be computed for exactly this message’s readers: the
recipient, the copies and the audience’s listed readers.

### Module Attributes

| [`FLOWS`](#liaise.policy.FLOWS)   | The flows, least restrictive first (discussion §5.5).                                                                                                                       |
|----------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`ROUTES`](#liaise.policy.ROUTES)  | The route the operator sees for each flow.                                                                                                                                  |
| [`SCOPES`](#liaise.policy.SCOPES)  | correspond's audience scopes, narrowest first.                                                                                                                              |
| [`TIERS`](#liaise.policy.TIERS)   | acquaint's tiers, most permissive first, and the relationship axis's extra value.                                                                                           |
| [`AXES`](#liaise.policy.AXES)    | The axes of every verdict (research §7.1), in the order `to_dict` writes them.                                                                                              |
| [`RULES`](#liaise.policy.RULES)   | the rows and their flows are the design; `evaluate(rules=...)` exists so a test can remove or weaken one row and show that a scenario then gets a less restrictive verdict. |

### Functions

| [`above`](#liaise.policy.above)(label, clearance)                           | Whether `label` is more restrictive than `clearance` (`None`: no ceiling).                                                                                          |
|----------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`audience_hash`](#liaise.policy.audience_hash)(audience)                           | SHA-256, in hex, of the canonical JSON of [`audience_record()`](#liaise.policy.audience_record) without `as_of` and `evidence`.                        |
| [`audience_in_words`](#liaise.policy.audience_in_words)(audience)                       | The audience in one line, as correspond's `Audience.in_words` says it.                                                                                              |
| [`audience_record`](#liaise.policy.audience_record)(audience)                         | `audience` as the dict correspond's `Audience.to_dict` writes, normalised alike.                                                                                    |
| [`canonical_json`](#liaise.policy.canonical_json)(data)                              | The canonical JSON both hashes are taken over: sorted keys, no spaces, ASCII.                                                                                       |
| [`evaluate`](#liaise.policy.evaluate)(outbound, \*, audience, disclosure, ...) | The [`Verdict`](#liaise.policy.Verdict) for `outbound`, through every rule of the table.                                                       |
| [`facts_of`](#liaise.policy.facts_of)(outbound, \*, audience, disclosure, ...) | The [`Facts`](#liaise.policy.Facts) of [`evaluate()`](#liaise.policy.evaluate)'s inputs, for a rule or a report to read. |
| [`flow_rank`](#liaise.policy.flow_rank)(flow)                                   | Where `flow` stands in [`FLOWS`](#liaise.policy.FLOWS); a `ValueError` for an unknown flow.                                                  |
| [`least_cleared_reader`](#liaise.policy.least_cleared_reader)(audience, disclosure, \*)    | The reader whose clearance is the content ceiling (discussion §4.4).                                                                                                |
| [`most_restrictive`](#liaise.policy.most_restrictive)(flows)                           | The highest of `flows`, or `send` when there are none.                                                                                                              |
| [`payload_hash`](#liaise.policy.payload_hash)(outbound)                            | SHA-256, in hex, of the canonical JSON of [`payload_of()`](#liaise.policy.payload_of).                                                            |
| [`payload_of`](#liaise.policy.payload_of)(outbound)                              | The message as the payload hash sees it: recipients, copies, ref, title, text, attachments.                                                                         |

### Classes

| [`Facts`](#liaise.policy.Facts)(\*, payload, audience, disclosure, ...)   | Everything a rule may look at, computed once from the inputs of [`evaluate()`](#liaise.policy.evaluate).                  |
|--------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------|
| [`Hit`](#liaise.policy.Hit)(text[, finding, reader, flow])              | One thing a rule found: its sentence, the finding and reader concerned, and the flow the rule asks for (`None`: the rule's declared minimum). |
| [`OutboundPolicy`](#liaise.policy.OutboundPolicy)(\*[, reply_mode, ...])           | The subject's policy as it applies to one message (discussion §5.4).                                                                          |
| [`Provenance`](#liaise.policy.Provenance)([tainted, evidence])                 | What the run that wrote the message read: tainted, clean, or unknown.                                                                         |
| [`Reader`](#liaise.policy.Reader)(clearance, who[, person])                | A reader the ceiling is judged on: their clearance (`None`: no ceiling), in words.                                                            |
| [`Reason`](#liaise.policy.Reason)(\*, rule, flow, text[, finding, reader]) | One rule that fired: the finding and the reader it concerns, and a sentence.                                                                  |
| [`Rule`](#liaise.policy.Rule)(name, predicate, flow)                     | One row of the table: a name, what it looks for, and its minimum flow.                                                                        |
| [`Verdict`](#liaise.policy.Verdict)(\*, flow, route, reasons, axes, ...)    | What the policy decided about one message, and why (discussion §5.1).                                                                         |

### liaise.policy.AXES *= ('audience', 'sensitivity', 'relationship', 'irreversible', 'tainted')*

The axes of every verdict (research §7.1), in the order `to_dict` writes them.

### liaise.policy.FLOWS *= ('send', 'delay', 'revise', 'approve', 'approve_twice', 'refuse')*

The flows, least restrictive first (discussion §5.5). `approve_twice` is reserved.

### *class* liaise.policy.Facts(, payload, audience, disclosure, findings, provenance, policy, now, readers, recipients, unresolved, least_cleared, resumable, case_id)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Everything a rule may look at, computed once from the inputs of [`evaluate()`](#liaise.policy.evaluate).

#### entry(person)

The disclosure’s entry for `person` (`{}` when it has none).

* **Return type:**
  [`Mapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Mapping)

#### lapsed(entry)

Whether a permissive tier is past its review date, by the entry or by `now`.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

#### *property* leaks *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Finding](liaise.detect.md#liaise.detect.Finding), ...]*

The findings that name something a reader is not cleared for, or sealed from.

#### of_kind(\*kinds)

The findings whose kind is one of `kinds`.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`Finding`](liaise.detect.md#liaise.detect.Finding), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

#### *property* ref *: [str](https://docs.python.org/3/builtins/stdtypes.html#str)*

The destination, for the operator’s sentence.

#### *property* scope *: [str](https://docs.python.org/3/builtins/stdtypes.html#str)*

The audience’s scope.

#### sealed_readers(finding)

The resolved readers the finding’s entity is sealed from.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

#### *property* tainted *: [bool](https://docs.python.org/3/builtins/functions.html#bool) | [None](https://docs.python.org/3/builtins/constants.html#None)*

Whether the run is tainted; `None` when unknown.

### *class* liaise.policy.Hit(text, finding=None, reader=None, flow=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One thing a rule found: its sentence, the finding and reader concerned, and the flow
the rule asks for (`None`: the rule’s declared minimum).

### *class* liaise.policy.OutboundPolicy(, reply_mode='direct', tainted_runs='approve', resumable=None, ai_tolerance=None, disclosure_decision=None, mode='enforce', outbox=False)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

The subject’s policy as it applies to one message (discussion §5.4).

`reply_mode` is the mode in force for the recipient (`draft` waits for the
operator). `tainted_runs` is `approve` (the taint rule applies) or `send` (the
subject waives it). `resumable` is whether a `revise` can go back to the
processor; `None` means “when the message belongs to a case”. `ai_tolerance` is the
recipient’s, from their record, and `disclosure_decision` the decision recorded on
the draft about saying the text is machine-written (`None`: none recorded).
`mode` is `enforce` or `shadow` and is carried on the verdict. `outbox` is
whether the delay outbox exists (slice L5): until it does, a `delay` verdict routes
to `draft`, as §5.5 degrades it.

The other subject-policy values §5.4 names (`link_allowlist`, `canary_terms`,
`leak_terms`, `public_channels`) are the detectors’ inputs, not this record’s;
[`of()`](#liaise.policy.OutboundPolicy.of) refuses them so a caller notices.

#### *classmethod* of(value)

`value` as an [`OutboundPolicy`](#liaise.policy.OutboundPolicy): a record, its dict, or None (defaults).

* **Return type:**
  [`OutboundPolicy`](#liaise.policy.OutboundPolicy)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* liaise.policy.Provenance(tainted=None, evidence=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What the run that wrote the message read: tainted, clean, or unknown.

`tainted` is `None` when nobody can say (the hook path), which counts as tainted
(decision 10). `evidence` says why: the messages read and the grades and roles that
the subject does not trust, in words.

#### *classmethod* clean(\*evidence)

A run that read only what the subject trusts.

* **Return type:**
  [`Provenance`](#liaise.policy.Provenance)

#### *classmethod* of(value)

`value` as a [`Provenance`](#liaise.policy.Provenance): a record, its dict, a bool, or None.

* **Return type:**
  [`Provenance`](#liaise.policy.Provenance)

#### *classmethod* tainted_by(\*evidence)

A run that read something the subject does not trust for `request_work`.

* **Return type:**
  [`Provenance`](#liaise.policy.Provenance)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

#### *classmethod* unknown(\*evidence)

A run nobody can vouch for.

* **Return type:**
  [`Provenance`](#liaise.policy.Provenance)

### liaise.policy.ROUTES *= {'approve': 'draft', 'approve_twice': 'draft', 'delay': 'send', 'refuse': 'block', 'revise': 'draft', 'send': 'send'}*

The route the operator sees for each flow.

### liaise.policy.RULES *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Rule](#liaise.policy.Rule), ...]* *= (Rule(name='secrets', predicate=<function secrets>, flow='refuse'), Rule(name='seals', predicate=<function seals>, flow='refuse'), Rule(name='exfiltration', predicate=<function exfiltration>, flow='refuse'), Rule(name='personal, public', predicate=<function personal_public>, flow='refuse'), Rule(name='no write-down', predicate=<function no_write_down>, flow='revise'), Rule(name='co-ownership', predicate=<function co_ownership>, flow='revise'), Rule(name='personal, private', predicate=<function personal_private>, flow='approve'), Rule(name='tier', predicate=<function tier>, flow='approve'), Rule(name='stranger', predicate=<function stranger>, flow='approve'), Rule(name='disclosure stance', predicate=<function disclosure_stance>, flow='approve'), Rule(name='taint', predicate=<function taint>, flow='approve'), Rule(name='reply mode', predicate=<function reply_mode>, flow='approve'), Rule(name='irreversibility', predicate=<function irreversibility>, flow='delay'), Rule(name='unknown audience', predicate=<function unknown_audience>, flow='send'))*

the rows and their flows
are the design; `evaluate(rules=...)` exists so a test can remove or weaken one row
and show that a scenario then gets a less restrictive verdict.

* **Type:**
  The policy table (discussion §5.4), in its order. Not a seam

### *class* liaise.policy.Reader(clearance, who, person=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A reader the ceiling is judged on: their clearance (`None`: no ceiling), in words.

`person` is the person id when the reader is one; a class of readers (“anyone”) has
none.

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* liaise.policy.Reason(, rule, flow, text, finding=None, reader=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One rule that fired: the finding and the reader it concerns, and a sentence.

`flow` is the minimum flow this hit asks for. `text` is what the operator reads;
it never holds the matched text.

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* liaise.policy.Rule(name, predicate, flow)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One row of the table: a name, what it looks for, and its minimum flow.

#### hits(facts)

The reasons this rule contributes for `facts`.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Reason`](#liaise.policy.Reason)]

### liaise.policy.SCOPES *= ('operator', 'named', 'group', 'org', 'public')*

correspond’s audience scopes, narrowest first.

### liaise.policy.TIERS *= ('open', 'involved', 'need-to-know', 'reviewed')*

acquaint’s tiers, most permissive first, and the relationship axis’s extra value.

### *class* liaise.policy.Verdict(, flow, route, reasons, axes, least_cleared, findings, payload_hash, audience_hash, audience, readers, as_of, mode)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What the policy decided about one message, and why (discussion §5.1).

`flow` is one of [`FLOWS`](#liaise.policy.FLOWS) and `route` its [`ROUTES`](#liaise.policy.ROUTES) entry. `reasons`
are every rule that fired, most restrictive first. `axes` are the values the decision
was made on: `audience` (the scope), `sensitivity` (the highest finding severity,
0 when nothing was found), `relationship` (the most restrictive standing among the
explicit recipients, a tier or `stranger`), `irreversible` (the audience is not
retractable) and `tainted` (true, false, or `None` for unknown). `least_cleared`
is the reader the content ceiling came from. `payload_hash` and `audience_hash`
are what an approval binds to; `audience` is the snapshot the hash was taken over
and `readers` the standing (tier, clearance) of every reader consulted, so the
ledger entry explains itself (§5.7); `as_of` is the `now` the verdict was made
at, and `mode` the policy’s. `route` is `draft` for `delay` until the outbox
exists (`OutboundPolicy.outbox`).

#### *property* rules *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), ...]*

The names of the rules that fired, most restrictive first, each once.

#### to_dict()

JSON-ready: what the ledger records for a gated message.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### liaise.policy.above(label, clearance)

Whether `label` is more restrictive than `clearance` (`None`: no ceiling).

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

```pycon
>>> above("amber", "clear"), above("amber", "amber"), above("red", None)
(True, False, False)
```

### liaise.policy.audience_hash(audience)

SHA-256, in hex, of the canonical JSON of [`audience_record()`](#liaise.policy.audience_record) without `as_of` and `evidence`.

Equal to correspond’s `Audience.hash` for the same record: the cross-package contract
an approval binds to.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.policy.audience_in_words(audience)

The audience in one line, as correspond’s `Audience.in_words` says it.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.policy.audience_record(audience)

`audience` as the dict correspond’s `Audience.to_dict` writes, normalised alike.

`None`, or a record without a scope, is the unknown audience: public, defaulted,
with every reason in its evidence. A defaulted audience is public whatever its
`scope` says, as correspond’s constructor insists. Unknown keys are dropped.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### liaise.policy.canonical_json(data)

The canonical JSON both hashes are taken over: sorted keys, no spaces, ASCII.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.policy.evaluate(outbound, \*, audience, disclosure, findings, provenance, policy=None, now, identities=None, \_rules=(Rule(name='secrets', predicate=<function secrets>, flow='refuse'), Rule(name='seals', predicate=<function seals>, flow='refuse'), Rule(name='exfiltration', predicate=<function exfiltration>, flow='refuse'), Rule(name='personal, public', predicate=<function personal_public>, flow='refuse'), Rule(name='no write-down', predicate=<function no_write_down>, flow='revise'), Rule(name='co-ownership', predicate=<function co_ownership>, flow='revise'), Rule(name='personal, private', predicate=<function personal_private>, flow='approve'), Rule(name='tier', predicate=<function tier>, flow='approve'), Rule(name='stranger', predicate=<function stranger>, flow='approve'), Rule(name='disclosure stance', predicate=<function disclosure_stance>, flow='approve'), Rule(name='taint', predicate=<function taint>, flow='approve'), Rule(name='reply mode', predicate=<function reply_mode>, flow='approve'), Rule(name='irreversibility', predicate=<function irreversibility>, flow='delay'), Rule(name='unknown audience', predicate=<function unknown_audience>, flow='send')))

The [`Verdict`](#liaise.policy.Verdict) for `outbound`, through every rule of the table.

Pure: no I/O, no clock (`now` is given), and the same verdict for the same inputs.
See the module docstring for what each input is. `_rules` is for the mutation
checks of the test suite only (the table is not a seam); it must hold rules of the
table by name.

* **Return type:**
  [`Verdict`](#liaise.policy.Verdict)

```pycon
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
```

### liaise.policy.facts_of(outbound, , audience, disclosure, findings, provenance, policy=None, now, identities=None)

The [`Facts`](#liaise.policy.Facts) of [`evaluate()`](#liaise.policy.evaluate)’s inputs, for a rule or a report to read.

* **Return type:**
  [`Facts`](#liaise.policy.Facts)

### liaise.policy.flow_rank(flow)

Where `flow` stands in [`FLOWS`](#liaise.policy.FLOWS); a `ValueError` for an unknown flow.

* **Return type:**
  [`int`](https://docs.python.org/3/builtins/functions.html#int)

### liaise.policy.least_cleared_reader(audience, disclosure, , identities=None, recipients=())

The reader whose clearance is the content ceiling (discussion §4.4).

`recipients` are the explicit recipients (person ids or addresses) to count among
the readers, an unresolved one at `clear`. The result’s `clearance` is `None`
for the operator alone, and `clear` for anything public, defaulted or unresolved.

* **Return type:**
  [`Reader`](#liaise.policy.Reader)

```pycon
>>> ada = {"people": {"ada": {"tier": "open", "clearance": "amber"}}}
>>> email = {"ref": "email:ada", "scope": "named", "readers": ["email:ada"]}
>>> least_cleared_reader(email, ada, identities={"email:ada": "ada"}, recipients=["ada"])
Reader(clearance='amber', who='ada', person='ada')
>>> least_cleared_reader(email, ada, recipients=["ada"]).who  # nobody tied the address to Ada
'email:ada, who has no record'
>>> least_cleared_reader({"ref": "github:example/app#1", "scope": "public"}, ada).clearance
'clear'
```

### liaise.policy.most_restrictive(flows)

The highest of `flows`, or `send` when there are none.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.policy.payload_hash(outbound)

SHA-256, in hex, of the canonical JSON of [`payload_of()`](#liaise.policy.payload_of).

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.policy.payload_of(outbound)

The message as the payload hash sees it: recipients, copies, ref, title, text, attachments.

Copies and attachments are sorted, so a listing order does not void an approval; a
message that differs in any of them does.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

```pycon
>>> payload_of({"ref": "github:example/app#12", "recipient": "ada", "text": "hi"})
{'recipients': ['ada'], 'cc': [], 'bcc': [], 'ref': 'github:example/app#12', 'title': None, 'text': 'hi', 'attachments': []}
```
