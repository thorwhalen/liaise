# liaise.legacy

What liaise 0.1’s gate would have decided: the counterfactual shadow mode measures against (liaise #39, #51).

Shadow mode does not loosen the gate. The policy of discussion 32 enforces on every
subject, `mode = "shadow"` included, and nothing sends that it holds back (the owner’s
decision on liaise #51, recorded in ADR 0002). What shadow mode adds is a measurement:
beside each verdict the gate records what 0.1 would have decided about the same message,
so `liaise gate report` can count how often the two agree and which messages 0.1 would
have let out that the policy found severe (decision 11’s *missed findings*).

0.1’s gate ran, and stopped at the first divert: `reply_mode` (a message outside a case,
or in `draft` reply mode, waits for the operator, unless an approval is on the context),
`leak_scan` (on a channel listed in `policy.public_channels` only: an absolute local
path, a `.env` path, an email address, a private key’s first line, a token shape, or one
of `policy.leak_terms` as a whole word, in the text or the title), `writing_card`,
`deslop` and `notify_recipient`. The last three are the gate’s filters today, unchanged,
so [`legacy_decision()`](#liaise.legacy.legacy_decision) takes what they held back as given rather than running them
twice. Its answer is 0.1’s own vocabulary: `send`, or a draft for the operator (recorded
as the flow `approve`, which is what a 0.1 divert was).

**Agreement is by flow class** ([`flow_class()`](#liaise.legacy.flow_class)): a message is either sent at once
(`send`) or held back (every other flow). 0.1 could say nothing finer, so a `revise` or
a `refuse` the policy chose agrees with a 0.1 divert. A `delay` counts as held even on a
subject whose outbox sends it unseen when its window passes, so on such a subject a 0.1
`send` against a policy `delay` is a disagreement: agreement errs low, never high.

The counterfactual records the kinds 0.1 would have diverted on, never a value or a
position: it is kept in the ledger beside the verdict, and the report reads counts only.

### Module Attributes

| [`LEGACY_VERSION`](#liaise.legacy.LEGACY_VERSION)         | The version of liaise whose gate [`legacy_decision()`](#liaise.legacy.legacy_decision) replays.                                                |
|-------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`LEGACY_PUBLIC_CHANNELS`](#liaise.legacy.LEGACY_PUBLIC_CHANNELS) | The channels 0.1 scanned when a subject named none, kept here so the replay outlives `policy.public_channels`, which the policy no longer reads (ADR 0002). |
| [`SENT`](#liaise.legacy.SENT)                   | out with no person looking, or held back.                                                                                                                   |
| [`HELD`](#liaise.legacy.HELD)                   | out with no person looking, or held back.                                                                                                                   |
| [`OUTSIDE_A_CASE`](#liaise.legacy.OUTSIDE_A_CASE)         | What 0.1 diverted a message outside a case, or in draft reply mode, for.                                                                                    |
| [`LEAK_SCAN`](#liaise.legacy.LEAK_SCAN)              | The prefix of a 0.1 leak-scan reason; the kinds follow it.                                                                                                  |

### Functions

| [`flow_class`](#liaise.legacy.flow_class)(flow)                                | [`SENT`](#liaise.legacy.SENT) for `send`, [`HELD`](#liaise.legacy.HELD) for every other flow: what agreement compares.   |
|--------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`leak_kinds`](#liaise.legacy.leak_kinds)(text, \*[, leak_terms])              | The kinds 0.1's leak scan found in `text`, each once, in the order 0.1 listed them.                                                                                      |
| [`legacy_decision`](#liaise.legacy.legacy_decision)(outbound, subject, \*, in_case) | What 0.1's gate would have decided about `outbound` on `subject`.                                                                                                        |

### Classes

| [`Counterfactual`](#liaise.legacy.Counterfactual)(flow[, reasons])   | What 0.1 would have decided: `flow` (`send` or `approve`) and why it held the message.   |
|------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------|

### *class* liaise.legacy.Counterfactual(flow, reasons=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What 0.1 would have decided: `flow` (`send` or `approve`) and why it held the message.

`reasons` name what 0.1 diverted on — a filter, or the leak scan’s kinds — never
what the message said.

#### to_dict()

JSON-ready, with the version it replays.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### liaise.legacy.HELD *= 'held'*

out with no person looking, or held back.

* **Type:**
  The two flow classes agreement is measured by

### liaise.legacy.LEAK_SCAN *= 'leak scan'*

The prefix of a 0.1 leak-scan reason; the kinds follow it.

### liaise.legacy.LEGACY_PUBLIC_CHANNELS *= ('github',)*

The channels 0.1 scanned when a subject named none, kept here so the replay outlives
`policy.public_channels`, which the policy no longer reads (ADR 0002).

### liaise.legacy.LEGACY_VERSION *= '0.1'*

The version of liaise whose gate [`legacy_decision()`](#liaise.legacy.legacy_decision) replays.

### liaise.legacy.OUTSIDE_A_CASE *= 'outside a case'*

What 0.1 diverted a message outside a case, or in draft reply mode, for.

### liaise.legacy.SENT *= 'sent'*

out with no person looking, or held back.

* **Type:**
  The two flow classes agreement is measured by

### liaise.legacy.flow_class(flow)

[`SENT`](#liaise.legacy.SENT) for `send`, [`HELD`](#liaise.legacy.HELD) for every other flow: what agreement compares.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> flow_class("send"), flow_class("delay"), flow_class("approve")
('sent', 'held', 'held')
```

### liaise.legacy.leak_kinds(text, , leak_terms=())

The kinds 0.1’s leak scan found in `text`, each once, in the order 0.1 listed them.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

```pycon
>>> leak_kinds("mail pat" + "@example.com, see /" + "Users" + "/pat/notes")
('local path', 'email')
>>> leak_kinds("ghp_" + "a" * 10 + "\n" + "b" * 10)
('token',)
>>> leak_kinds("the Orchid launch", leak_terms=["orchid"])
('leak term',)
```

### liaise.legacy.legacy_decision(outbound, subject, , in_case, approved=False, shared_diverts=())

What 0.1’s gate would have decided about `outbound` on `subject`.

`in_case` is whether the message belongs to a case; `approved` whether an approval
of any kind was on the context (0.1 bound it to nothing, so any approval released a
draft past its reply mode). `shared_diverts` names the filters the gate shares with
0.1 (the writing card, deslop, the mention) that held this message back today.

* **Return type:**
  [`Counterfactual`](#liaise.legacy.Counterfactual)
