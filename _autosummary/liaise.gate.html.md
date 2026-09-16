# liaise.gate

The outbound gate: the checks every message passes before liaise sends it, and the verdict they reach.

A processor run reports outcomes, [`liaise.outcomes`](liaise.outcomes.html.md#module-liaise.outcomes) plans them into actions, and each
[`Send`](liaise.outcomes.html.md#liaise.outcomes.Send) among those is an [`Outbound`](#liaise.gate.Outbound) that the tick hands to
[`run_gate()`](#liaise.gate.run_gate) before anything reaches a channel. A message outside a case, and a draft
the operator releases, pass the same gate. It runs [`DFLT_OUTBOUND_FILTERS`](#liaise.gate.DFLT_OUTBOUND_FILTERS), in this
order:

1. [`outside_a_case()`](#liaise.gate.outside_a_case): a message outside any case waits for the operator: its sender
   chose where it goes and to whom (liaise #28).
2. [`outbound_policy()`](#liaise.gate.outbound_policy): the policy of liaise discussion 32. Who can read the
   destination (the audience on the context), what each reader may be told (the
   disclosure), what the message holds (the detectors, over its text, title and attachment
   names) and what the run that wrote it read (the provenance on the context), through the
   rule table of [`liaise.policy`](liaise.policy.html.md#module-liaise.policy). Draft reply mode is a row of that table. It
   replaces 0.1’s leak scan.
3. [`writing_card()`](#liaise.gate.writing_card): a note with the recipient’s acquaint writing card.
4. [`deslop()`](#liaise.gate.deslop): nothing acquaint’s style lint finds machine-sounding.
5. [`notify_recipient()`](#liaise.gate.notify_recipient): on GitHub, the message starts with `@<login>`, since
   GitHub notifies only the people a comment mentions.

**Every filter runs** (liaise ADR 0002, which amends ADR 0001’s “the first divert ends the
gate”). A filter is `(outbound, ctx) -> Pass | Divert`. A [`Divert`](#liaise.gate.Divert) says how far
the message must be held back: its `flow`, one of [`liaise.policy.FLOWS`](liaise.policy.html.md#liaise.policy.FLOWS)
(`approve` when it does not say). Each divert is one or more [`Concern`](#liaise.gate.Concern) records,
the policy’s one per rule that fired. The decision’s flow is the most restrictive concern
still standing, and its reasons are all of them, most restrictive first. A message goes out
only when that flow is `send`: `delay` waits for the operator until the delay outbox
exists (liaise #38). A filter that raises, or answers anything but a `Pass` or a
`Divert`, contributes an `approve` concern with the error as its reason. The order stays
fixed and is not a seam: the mention, the one rewrite, comes last, so every filter judges
the text as it was written, and the rewrite reaches a send only when nothing held it back.

**Approvals** (discussion §5.7). The operator’s [`Approval`](liaise.model.html.md#liaise.model.Approval) on
`GateContext.approval` is bound to the message, the audience and the verdict it was
given for: the hashes of the message the filters judged and of the audience on the context
([`liaise.policy.payload_hash()`](liaise.policy.html.md#liaise.policy.payload_hash), [`liaise.policy.audience_hash()`](liaise.policy.html.md#liaise.policy.audience_hash)), and the name of
what that verdict flagged ([`liaise.outbound.verdict_id()`](liaise.outbound.html.md#liaise.outbound.verdict_id)). While all three still hold,
it settles each concern whose rule it names and whose flow is at most `approve`. A
`refuse` is never settled, nor is a concern with no rule (deslop, a missing handle, a
filter that failed). An approval that no longer binds settles nothing, and is itself the
first concern the operator reads, naming what changed — a widened audience, an edited text,
or a disclosure that now flags something else under the same rule. [`approval_for()`](#liaise.gate.approval_for)
makes the approval for a decision the operator was shown.

The gate only decides. [`liaise.release.gate_and_send()`](liaise.release.html.md#liaise.release.gate_and_send) computes the audience, runs the
gate, and sends `GateDecision.send`; its callers keep a diverted message as a draft
(see [`liaise.outcomes.make_draft()`](liaise.outcomes.html.md#liaise.outcomes.make_draft)) and record [`GateDecision.record()`](#liaise.gate.GateDecision.record). acquaint is
optional (`liaise[people]`): without it the disclosure has every reader at
`need-to-know` and the subject’s `leak_terms` as its vocabulary, and filters 3 and 4
add a note and let the message through.

### Module Attributes

| [`OUTSIDE_A_CASE`](#liaise.gate.OUTSIDE_A_CASE)        | The rule [`outside_a_case()`](#liaise.gate.outside_a_case) holds a message for, which an approval names to release it.   |
|------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------|
| [`MENTION_CHANNEL`](#liaise.gate.MENTION_CHANNEL)       | The channel whose messages must @mention their recipient to reach them.                                                                  |
| [`DELAY_HELD`](#liaise.gate.DELAY_HELD)            | What a decision held back as `delay` says, until the outbox (liaise #38) exists.                                                         |
| [`GATE_CONCERN`](#liaise.gate.GATE_CONCERN)          | a void approval, a message it cannot hash.                                                                                               |
| [`MAX_WRAPPED_FILTERS`](#liaise.gate.MAX_WRAPPED_FILTERS)   | How far [`filter_name()`](#liaise.gate.filter_name) unwraps a filter to find the name of the check it runs.           |
| [`OutboundFilter`](#liaise.gate.OutboundFilter)        | one check of the gate.                                                                                                                   |
| [`DFLT_OUTBOUND_FILTERS`](#liaise.gate.DFLT_OUTBOUND_FILTERS) | The gate's filters, in the order they run.                                                                                               |

### Functions

| [`approval_for`](#liaise.gate.approval_for)(decision, \*, by, at[, ...])       | The approval of `by`, at `at`, of the message and audience `decision` judged.                |
|--------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| [`binds`](#liaise.gate.binds)(approval, hashes, verdict)                | Whether `approval` was given for this message, this audience and this verdict.               |
| [`deslop`](#liaise.gate.deslop)(outbound, ctx)                           | Divert a message acquaint's style lint finds machine-sounding for its recipient.             |
| [`filter_name`](#liaise.gate.filter_name)(outbound_filter)                    | How the gate names a filter: its name, the name of what a partial wraps, else its type.      |
| [`notify_recipient`](#liaise.gate.notify_recipient)(outbound, ctx)                 | On GitHub, make the message start with an `@mention` of its recipient.                       |
| [`outbound_policy`](#liaise.gate.outbound_policy)(outbound, ctx, \*[, ...])       | Hold back what the outbound policy (discussion §5.4) does not let go now.                    |
| [`outside_a_case`](#liaise.gate.outside_a_case)(outbound, ctx)                   | Hold a message outside any case (`ctx.case` None) for the operator, whatever its reply mode. |
| [`run_gate`](#liaise.gate.run_gate)(outbound, ctx, \*[, outbound_filters]) | Run `outbound` through every one of `outbound_filters`, in order, and decide.                |
| [`writing_card`](#liaise.gate.writing_card)(outbound, ctx)                     | Note the recipient's acquaint writing card, for the ledger and the next run.                 |

### Classes

| [`Concern`](#liaise.gate.Concern)(\*, filter, flow, text[, rule, findings])   | One reason the gate holds a message back: the filter, the rule, how far, and why.                                    |
|------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------|
| [`Divert`](#liaise.gate.Divert)(reason[, notes, flow, findings, ...])        | A filter's verdict to hold the message back: `reason`, and how far (`flow`).                                         |
| [`GateContext`](#liaise.gate.GateContext)(\*, subject, now[, case, ...])          | What the filters may consult about one message.                                                                      |
| [`GateDecision`](#liaise.gate.GateDecision)(send, diverted[, notes, ...])          | What [`run_gate()`](#liaise.gate.run_gate) decided: `send` a message, or why it is `diverted`. |
| [`Outbound`](#liaise.gate.Outbound)(\*, ref, channel, recipient, ...[, ...])   | A message liaise would send: `text` for `recipient` (a person id) at `ref`.                                          |
| [`Pass`](#liaise.gate.Pass)(outbound[, notes, judgement])                  | A filter's verdict to go on, with `outbound` as the filter left it.                                                  |

### *class* liaise.gate.Concern(, filter, flow, text, rule=None, findings=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One reason the gate holds a message back: the filter, the rule, how far, and why.

`text` is what the operator reads, and never holds a matched value. `rule` is None
for a concern no approval settles.

#### *property* settleable *: [bool](https://docs.python.org/3/builtins/functions.html#bool)*

a rule, and a flow at most `approve`.

* **Type:**
  Whether an approval naming its rule settles it

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### liaise.gate.DELAY_HELD *= 'a delay is held for the operator until the delay outbox exists (liaise #38)'*

What a decision held back as `delay` says, until the outbox (liaise #38) exists.

### liaise.gate.DFLT_OUTBOUND_FILTERS *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Callable](https://docs.python.org/3/library/typing.html#typing.Callable)[[[Outbound](#liaise.gate.Outbound), [GateContext](#liaise.gate.GateContext)], [Pass](#liaise.gate.Pass) | [Divert](#liaise.gate.Divert)], ...]* *= (<function outside_a_case>, <function outbound_policy>, <function writing_card>, <function deslop>, <function notify_recipient>)*

The gate’s filters, in the order they run. The order is part of the design, not a
setting: the mention, the one rewrite, comes after every filter that judges the text.

### *class* liaise.gate.Divert(reason, notes=(), flow='approve', findings=(), rule=None, judgement=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A filter’s verdict to hold the message back: `reason`, and how far (`flow`).

`flow` is one of [`liaise.policy.FLOWS`](liaise.policy.html.md#liaise.policy.FLOWS) other than `send`; a divert that does
not say is `approve`, a flagged draft for the operator. `findings` are what it
found. `rule` names what an approval may settle it by; a divert without one is
settled only by changing the message. `judgement` is the policy’s, whose rules become
the concerns.

### liaise.gate.GATE_CONCERN *= 'gate'*

a void approval, a message it cannot hash.

* **Type:**
  The name the gate files its own concerns under

### *class* liaise.gate.GateContext(, subject, now, case=None, approval=None, audience=None, provenance=None, mode=None, fingerprint_key=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What the filters may consult about one message.

`subject` is the subject whose policy applies, `now` the time of the decision, and
`case` the case the message belongs to (None outside any). `audience` is
correspond’s record of who can read the destination, computed right before the gate
runs (None: unknown, so public). `provenance` is what the run that wrote the message
read (None: unknown, so tainted). `mode` overrides the subject’s `policy.mode`.
`approval` is the operator’s release of this message, None for every message sent
without one. `fingerprint_key` is the key findings are fingerprinted with (None: the
one in the configured state directory).

### *class* liaise.gate.GateDecision(send, diverted, notes=(), diverted_by=None, flow='send', concerns=(), settled=(), verdict=None, consulted=<factory>, approval=None, payload_hash=None, audience_hash=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What [`run_gate()`](#liaise.gate.run_gate) decided: `send` a message, or why it is `diverted`.

Exactly one of `send` (the message as the filters left it) and `diverted` (every
standing concern’s text, most restrictive first) is set. `flow` is the decision’s,
`concerns` what still holds the message back and `settled` what the approval
released it past. `notes` holds every filter’s notes, in order. `diverted_by` names
the filter of the most restrictive concern, as an operator notification may say it: the
reason can quote what a filter raised. `verdict` and `consulted` are the policy’s
verdict and what it consulted. `approval` is the one on the context, and
`payload_hash` and `audience_hash` what it had to match.

#### *property* audience_words *: [str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None)*

The audience the policy judged, in words; None when the policy did not run.

#### *property* bound *: [bool](https://docs.python.org/3/builtins/functions.html#bool)*

Whether the approval binds to this message, this audience and this verdict.

#### *property* overridable *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), ...]*

The rules of the standing concerns an approval could settle, each once.

#### record()

What a ledger `gate` entry records of the decision (discussion §5.7).

The flow and every concern with its findings (kinds, positions and fingerprints,
never the value), what the approval settled, the policy’s verdict (its audience
snapshot, the readers’ tiers and clearances, the mode), the labels, seals and
provenance consulted, and the approval with whether it bound.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

#### summary()

What a held draft keeps of the decision: the flow, the audience in words, the reasons.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### liaise.gate.MAX_WRAPPED_FILTERS *= 8*

How far [`filter_name()`](#liaise.gate.filter_name) unwraps a filter to find the name of the check it runs.

### liaise.gate.MENTION_CHANNEL *= 'github'*

The channel whose messages must @mention their recipient to reach them.

### liaise.gate.OUTSIDE_A_CASE *= 'a message outside a case'*

The rule [`outside_a_case()`](#liaise.gate.outside_a_case) holds a message for, which an approval names to release it.

### *class* liaise.gate.Outbound(, ref, channel, recipient, purpose, text, title=None, case_id=None, cc=(), bcc=(), attachments=(), project=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A message liaise would send: `text` for `recipient` (a person id) at `ref`.

`ref` is the encoded conversation or address it goes to (`github:example/app#12`,
or `github:example/app` to open an issue there), and `channel` is that ref’s
channel. `purpose` is the outcome kind it carries out (`ask`, `reply`,
`propose`, `deliver`). `title` is the title of the issue it opens, when it opens
one. `case_id` is the case the message belongs to, or None for a message an agent
sends outside any case (`liaise message send`). `cc` and `bcc` are further
recipients (addresses), on channels that have them; `attachments` are the names of
attached files, and `project` the project the message is about, when one is named.
The policy judges every one of these, and the payload hash covers all but `project`.

### liaise.gate.OutboundFilter

one check of the gate.

* **Type:**
  `(outbound, ctx) -> Pass | Divert`

alias of `Callable`[[[`Outbound`](#liaise.gate.Outbound), [`GateContext`](#liaise.gate.GateContext)], [`Pass`](#liaise.gate.Pass) | [`Divert`](#liaise.gate.Divert)]

### *class* liaise.gate.Pass(outbound, notes=(), judgement=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A filter’s verdict to go on, with `outbound` as the filter left it.

`judgement` is the policy’s, when the filter is the policy.

### liaise.gate.approval_for(decision, , by, at, justification='')

The approval of `by`, at `at`, of the message and audience `decision` judged.

It overrides every concern of the decision an approval can settle, so the operator must
have been shown `decision`: its hashes bind the approval to exactly that message and
audience.

* **Return type:**
  [`Approval`](liaise.model.html.md#liaise.model.Approval)

### liaise.gate.binds(approval, hashes, verdict)

Whether `approval` was given for this message, this audience and this verdict.

The one rule the gate settles by, so what a decision records as bound is what its
concerns were judged by: both hashes as the filters computed them, and the name of
what the verdict flags ([`liaise.outbound.verdict_id()`](liaise.outbound.html.md#liaise.outbound.verdict_id)), which is `None` when no
policy judged the message.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### liaise.gate.deslop(outbound, ctx)

Divert a message acquaint’s style lint finds machine-sounding for its recipient.

Calls `acquaint.style_lint(text, recipient=recipient)`. When that is not `ok`,
the message is diverted, with each enforced finding as a note. Without acquaint, or
when acquaint fails, the note says why and the message goes on. It never raises.

* **Return type:**
  `Union`[[`Pass`](#liaise.gate.Pass), [`Divert`](#liaise.gate.Divert)]

### liaise.gate.filter_name(outbound_filter)

How the gate names a filter: its name, the name of what a partial wraps, else its type.

Never its repr, which can hold what a filter was bound to, such as a local path; the
name reaches the operator’s notification. A filter configured at a seam is a
`functools.partial`, whose own name is its arguments: its function’s name is what
tells the operator which check held their message back.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.gate.notify_recipient(outbound, ctx)

On GitHub, make the message start with an `@mention` of its recipient.

GitHub notifies only the people a comment mentions, and an issue an app files
subscribes its partner to nothing. The login is the first valid one among the
recipient’s `github:` addresses, best first (see
[`notify_addresses_for()`](liaise.subjects.html.md#liaise.subjects.Subject.notify_addresses_for)). A missing mention is
prefixed, the one rewrite the gate makes. A recipient with no GitHub address is
diverted. Other channels pass unchanged.

* **Return type:**
  `Union`[[`Pass`](#liaise.gate.Pass), [`Divert`](#liaise.gate.Divert)]

### liaise.gate.outbound_policy(outbound, ctx, \*, disclosure=<function acquaint_disclosure>, detectors=(<function secret_detector.<locals>.detect_secrets>, <function detect_canaries>, <function detect_vocabulary>, <function chain.<locals>.chained>, <function chain.<locals>.chained>, <function detect_third_parties>), resolver=<function resolve_person>)

Hold back what the outbound policy (discussion §5.4) does not let go now.

The audience is the context’s (unknown, so public, when it has none), the disclosure
comes through `disclosure` (acquaint’s, or every reader at `need-to-know` without
it), and the provenance is the context’s (unknown, so tainted, when it has none). See
[`liaise.outbound.judge()`](liaise.outbound.html.md#liaise.outbound.judge). A `send` verdict passes, noting the audience; any
other diverts at its flow, one concern per rule that fired. It never redacts: what it
found is for the operator to fix, and its reasons say where, never what.

* **Return type:**
  `Union`[[`Pass`](#liaise.gate.Pass), [`Divert`](#liaise.gate.Divert)]

### liaise.gate.outside_a_case(outbound, ctx)

Hold a message outside any case (`ctx.case` None) for the operator, whatever its reply mode.

Its sender chose where it goes and to whom, so a sender who picks a person in
`direct` mode must not reach an audience that way (discussion §6.2). An approval
naming [`OUTSIDE_A_CASE`](#liaise.gate.OUTSIDE_A_CASE), bound to the message, releases it.

* **Return type:**
  `Union`[[`Pass`](#liaise.gate.Pass), [`Divert`](#liaise.gate.Divert)]

### liaise.gate.run_gate(outbound, ctx, \*, outbound_filters=(<function outside_a_case>, <function outbound_policy>, <function writing_card>, <function deslop>, <function notify_recipient>))

Run `outbound` through every one of `outbound_filters`, in order, and decide.

Each [`Pass`](#liaise.gate.Pass) hands its message, possibly rewritten, to the next filter; each
[`Divert`](#liaise.gate.Divert) adds its concerns. An approval on the context settles what it binds to
and names (see the module docstring). The decision’s flow is the most restrictive
concern left, and only `send` sends. The gate fails closed: a filter that raises, or
returns anything but a `Pass` (of an [`Outbound`](#liaise.gate.Outbound)) or a `Divert`, adds an
`approve` concern naming it.

* **Return type:**
  [`GateDecision`](#liaise.gate.GateDecision)

### liaise.gate.writing_card(outbound, ctx)

Note the recipient’s acquaint writing card, for the ledger and the next run.

Calls `acquaint.brief(recipient, purpose=purpose)` and notes its summary. It never
diverts and never raises: without acquaint, or when acquaint fails (an unknown
person raises `AcquaintError`), the note says why the card is unavailable.

* **Return type:**
  [`Pass`](#liaise.gate.Pass)
