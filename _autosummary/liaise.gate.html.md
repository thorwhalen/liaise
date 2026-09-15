# liaise.gate

The outbound gate: the checks a message passes before liaise sends it.

A processor run reports outcomes, [`liaise.outcomes`](liaise.outcomes.html.md#module-liaise.outcomes) plans them into actions, and
each [`Send`](liaise.outcomes.html.md#liaise.outcomes.Send) among those is an [`Outbound`](#liaise.gate.Outbound) that the tick
hands to [`run_gate()`](#liaise.gate.run_gate) before anything reaches a channel. The gate runs
[`DFLT_OUTBOUND_FILTERS`](#liaise.gate.DFLT_OUTBOUND_FILTERS), in this order:

1. [`reply_mode()`](#liaise.gate.reply_mode): nothing goes directly to a person in `draft` reply mode.
2. [`leak_scan()`](#liaise.gate.leak_scan): on a public channel, nothing holding an absolute local path, a
   `.env` path, an email address, a private key, a token (wrapped across lines or not)
   or one of `policy.leak_terms`. It never redacts.
3. [`writing_card()`](#liaise.gate.writing_card): a note with the recipient’s acquaint writing card.
4. [`deslop()`](#liaise.gate.deslop): nothing acquaint’s style lint finds machine-sounding.
5. [`notify_recipient()`](#liaise.gate.notify_recipient): on GitHub, the message starts with `@<login>`, since
   GitHub notifies only the people a comment mentions.

A filter is `(outbound, ctx) -> Pass | Divert`. A [`Pass`](#liaise.gate.Pass) hands the message,
possibly rewritten, to the next filter; only [`notify_recipient()`](#liaise.gate.notify_recipient) rewrites. The
first [`Divert`](#liaise.gate.Divert) ends the gate: the message is not sent, and goes to the operator
instead. Notes accumulate across the filters that ran. acquaint is optional
(`liaise[people]`): without it, or for a person it does not know, filters 3 and 4
add a note and let the message through.

The gate only decides. The tick sends `GateDecision.send`, or stores the diverted
message on the case as a draft (see [`liaise.outcomes.make_draft()`](liaise.outcomes.html.md#liaise.outcomes.make_draft)) and notifies
the operator.

### Module Attributes

| [`DRAFT_REPLY_MODE`](#liaise.gate.DRAFT_REPLY_MODE)      | The reply mode in which liaise sends nothing without the operator.      |
|------------------------------------------------------------------------|-------------------------------------------------------------------------|
| [`MENTION_CHANNEL`](#liaise.gate.MENTION_CHANNEL)       | The channel whose messages must @mention their recipient to reach them. |
| [`OutboundFilter`](#liaise.gate.OutboundFilter)        | one check of the gate.                                                  |
| [`DFLT_OUTBOUND_FILTERS`](#liaise.gate.DFLT_OUTBOUND_FILTERS) | The gate's filters, in the order they run.                              |

### Functions

| [`deslop`](#liaise.gate.deslop)(outbound, ctx)                           | Divert a message acquaint's style lint finds machine-sounding for its recipient.   |
|--------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------|
| [`leak_scan`](#liaise.gate.leak_scan)(outbound, ctx)                        | On a public channel, divert a message holding what must not be made public.        |
| [`notify_recipient`](#liaise.gate.notify_recipient)(outbound, ctx)                 | On GitHub, make the message start with an `@mention` of its recipient.             |
| [`reply_mode`](#liaise.gate.reply_mode)(outbound, ctx)                       | Divert when the recipient's reply mode is `draft`.                                 |
| [`run_gate`](#liaise.gate.run_gate)(outbound, ctx, \*[, outbound_filters]) | Run `outbound` through `outbound_filters` in order, stopping at the first divert.  |
| [`writing_card`](#liaise.gate.writing_card)(outbound, ctx)                     | Note the recipient's acquaint writing card, for the ledger and the next run.       |

### Classes

| [`Divert`](#liaise.gate.Divert)(reason[, notes])                         | A filter's verdict to send nothing and hand the message to the operator.                                              |
|--------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------|
| [`GateContext`](#liaise.gate.GateContext)(subject, case, now)                 | What the filters may consult: the subject and its policy, the case, the time.                                         |
| [`GateDecision`](#liaise.gate.GateDecision)(send, diverted[, notes, ...])      | What [`run_gate()`](#liaise.gate.run_gate) decided: `send` a message, or why it was `diverted`. |
| [`Outbound`](#liaise.gate.Outbound)(case_id, ref, channel, recipient, ...) | A message liaise would send: `text` for `recipient` (a person id) at `ref`.                                           |
| [`Pass`](#liaise.gate.Pass)(outbound[, notes])                         | A filter's verdict to go on, with `outbound` as the filter left it.                                                   |

### liaise.gate.DFLT_OUTBOUND_FILTERS *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Callable](https://docs.python.org/3/library/typing.html#typing.Callable)[[[Outbound](#liaise.gate.Outbound), [GateContext](#liaise.gate.GateContext)], [Pass](#liaise.gate.Pass) | [Divert](#liaise.gate.Divert)], ...]* *= (<function reply_mode>, <function leak_scan>, <function writing_card>, <function deslop>, <function notify_recipient>)*

The gate’s filters, in the order they run. The order is part of the design, not a
setting: a draft is diverted before anything else looks at it.

### liaise.gate.DRAFT_REPLY_MODE *= 'draft'*

The reply mode in which liaise sends nothing without the operator.

### *class* liaise.gate.Divert(reason, notes=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A filter’s verdict to send nothing and hand the message to the operator.

### *class* liaise.gate.GateContext(subject, case, now)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What the filters may consult: the subject and its policy, the case, the time.

### *class* liaise.gate.GateDecision(send, diverted, notes=(), diverted_by=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What [`run_gate()`](#liaise.gate.run_gate) decided: `send` a message, or why it was `diverted`.

Exactly one of `send` (the message as the filters left it) and `diverted` (the
reason) is set. `notes` holds the notes of every filter that ran, in order.
`diverted_by` names the filter that diverted, as an operator notification may say it:
the reason can quote what a filter raised.

### liaise.gate.MENTION_CHANNEL *= 'github'*

The channel whose messages must @mention their recipient to reach them.

### *class* liaise.gate.Outbound(case_id, ref, channel, recipient, purpose, text)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A message liaise would send: `text` for `recipient` (a person id) at `ref`.

`ref` is the encoded conversation or address it goes to
(`github:example/app#12`), and `channel` that ref’s channel. `purpose` is the
outcome kind it carries out (`ask`, `reply`, `propose`, `deliver`).

### liaise.gate.OutboundFilter

one check of the gate.

* **Type:**
  `(outbound, ctx) -> Pass | Divert`

alias of `Callable`[[[`Outbound`](#liaise.gate.Outbound), [`GateContext`](#liaise.gate.GateContext)], [`Pass`](#liaise.gate.Pass) | [`Divert`](#liaise.gate.Divert)]

### *class* liaise.gate.Pass(outbound, notes=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A filter’s verdict to go on, with `outbound` as the filter left it.

### liaise.gate.deslop(outbound, ctx)

Divert a message acquaint’s style lint finds machine-sounding for its recipient.

Calls `acquaint.style_lint(text, recipient=recipient)`. When that is not `ok`,
the message is diverted, with each enforced finding as a note. Without acquaint, or
when acquaint fails, the note says why and the message goes on. It never raises.

* **Return type:**
  `Union`[[`Pass`](#liaise.gate.Pass), [`Divert`](#liaise.gate.Divert)]

### liaise.gate.leak_scan(outbound, ctx)

On a public channel, divert a message holding what must not be made public.

That is an absolute local path (a home directory on macOS, Linux or Windows, written
with single or JSON-doubled backslashes, a Windows home through a WSL mount, or a
macOS temporary directory), a path ending in `.env`, an email address, a private
key’s `-----BEGIN ... PRIVATE KEY-----` or `-----BEGIN PGP PRIVATE KEY BLOCK-----`
line, a token shape (`ghp_`,
`github_pat_`, `sk-`, `AKIA`, `hf_`, `xoxb-`), or one of
`policy.leak_terms` as a whole word in any case. Tokens are also looked for with the
text’s line breaks removed, so a token wrapped across lines is found. The reason
names each kind found and the notes say where, never what. It never redacts: a leak
is for the operator to fix. A channel outside `policy.public_channels` passes
unscanned.

* **Return type:**
  `Union`[[`Pass`](#liaise.gate.Pass), [`Divert`](#liaise.gate.Divert)]

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

### liaise.gate.reply_mode(outbound, ctx)

Divert when the recipient’s reply mode is `draft`.

The mode is the person’s `policy.reply_modes` override, else the subject’s
`default_reply_mode` (see [`reply_mode_for()`](liaise.subjects.html.md#liaise.subjects.Subject.reply_mode_for)).

* **Return type:**
  `Union`[[`Pass`](#liaise.gate.Pass), [`Divert`](#liaise.gate.Divert)]

### liaise.gate.run_gate(outbound, ctx, \*, outbound_filters=(<function reply_mode>, <function leak_scan>, <function writing_card>, <function deslop>, <function notify_recipient>))

Run `outbound` through `outbound_filters` in order, stopping at the first divert.

Each [`Pass`](#liaise.gate.Pass) hands its message, possibly rewritten, to the next filter. The
first [`Divert`](#liaise.gate.Divert) ends the gate with nothing to send. Notes accumulate across
the filters that ran. The gate fails closed: a filter that raises, or returns
anything but a `Pass` or a `Divert`, diverts the message with a reason naming it.

* **Return type:**
  [`GateDecision`](#liaise.gate.GateDecision)

### liaise.gate.writing_card(outbound, ctx)

Note the recipient’s acquaint writing card, for the ledger and the next run.

Calls `acquaint.brief(recipient, purpose=purpose)` and notes its summary. It never
diverts and never raises: without acquaint, or when acquaint fails (an unknown
person raises `AcquaintError`), the note says why the card is unavailable.

* **Return type:**
  [`Pass`](#liaise.gate.Pass)
