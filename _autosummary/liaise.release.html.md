# liaise.release

Releasing a message: through the gate, then through correspond, as one step.

Every message liaise sends goes through [`gate_and_send()`](#liaise.release.gate_and_send). That covers what the tick
sends for a run’s outcomes, the tick’s own notices, a message an agent sends outside any
case (`liaise message send`), and a draft the operator releases. It runs
[`liaise.gate.run_gate()`](liaise.gate.html.md#liaise.gate.run_gate), and only a message the gate passed reaches
`correspond.send`, as the filters left it. It records nothing: what a
[`SendAttempt`](#liaise.release.SendAttempt) means for a case, a message or a notification is for its caller to
keep.

**Releasing a held message.** [`release_draft()`](#liaise.release.release_draft) is the one way a message held for the
operator goes out, whether it waits on a case (`liaise case send-draft`) or outside any
(`liaise message send-draft`). It checks what must stop a release, runs
[`gate_and_send()`](#liaise.release.gate_and_send) with the operator’s [`Approval`](liaise.model.html.md#liaise.model.Approval) on the context,
and hands back the ledger entry and the draft to keep, for the caller to record.

One path for every sender is what makes the gate a gate. A filter added to it applies to
all of them at once, and none of them has a way to send around it.

### Module Attributes

| [`DFLT_REFUSAL`](#liaise.release.DFLT_REFUSAL)     | Why a send failed when the channel said no without saying why.   |
|-------------------------------------------------------------------|------------------------------------------------------------------|
| [`DELIVER_PURPOSE`](#liaise.release.DELIVER_PURPOSE)  | The outcome whose message announces a delivery.                  |
| [`DRAFT_ENTRY_KIND`](#liaise.release.DRAFT_ENTRY_KIND) | a gate decision, as the tick's are.                              |
| [`GITHUB_CHANNEL`](#liaise.release.GITHUB_CHANNEL)   | The channel whose references name repositories.                  |

### Functions

| [`error_text`](#liaise.release.error_text)(error)                                 | How liaise names an exception it recovered from: its class, then its message.                                                                                |
|----------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`gate_and_send`](#liaise.release.gate_and_send)(outbound, ctx, \*[, registry, ...]) | Put `outbound` through the gate and, only when it passes, send it through correspond.                                                                        |
| [`github_repo`](#liaise.release.github_repo)(ref)                                  | `owner/repo`, lower-cased, of the repository a GitHub reference names: itself or one of its issues.                                                          |
| [`release_draft`](#liaise.release.release_draft)(draft, \*, subject, ledger, ...)    | Release `draft` (a [`liaise.outcomes.make_draft()`](liaise.outcomes.html.md#liaise.outcomes.make_draft) item) as `by`, through the gate. |
| [`sendable_ref`](#liaise.release.sendable_ref)(ref)                                 | `ref` as a GitHub issue or repository reference, stripped and lower-cased.                                                                                   |

### Classes

| [`DraftOutcome`](#liaise.release.DraftOutcome)(attempt, filters, edited[, ...])   | What [`release_draft()`](#liaise.release.release_draft) did with one held message, for its caller to record.   |
|--------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------|
| [`SendAttempt`](#liaise.release.SendAttempt)(decision[, result, failure, ...])   | What [`gate_and_send()`](#liaise.release.gate_and_send) did with one message.                                  |

### Exceptions

| [`DraftSentNotRecorded`](#liaise.release.DraftSentNotRecorded)   | A released message went out, and the ledger then failed to record that it did.   |
|-------------------------------------------------------------------------|----------------------------------------------------------------------------------|

### liaise.release.DELIVER_PURPOSE *= 'deliver'*

The outcome whose message announces a delivery.

### liaise.release.DFLT_REFUSAL *= 'the channel refused it'*

Why a send failed when the channel said no without saying why.

### liaise.release.DRAFT_ENTRY_KIND *= 'gate'*

a gate decision, as the
tick’s are.

* **Type:**
  The entry kind a released or rejected draft is recorded as

### *class* liaise.release.DraftOutcome(attempt, filters, edited, entry=None, kept=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What [`release_draft()`](#liaise.release.release_draft) did with one held message, for its caller to record.

`attempt` is the gate’s decision and the send, `filters` how many filters the gate
ran it through, and `edited` whether the operator’s text replaced the draft’s.
`entry` is the `gate` entry the release is recorded as. `kept` is the draft that
stays for the operator when nothing went out, and None once the message is sent. Both
are None for a plan (`send=False`) that the gate passed: nothing happened to record.

### *exception* liaise.release.DraftSentNotRecorded

Bases: [`RuntimeError`](https://docs.python.org/3/builtins/exceptions.html#RuntimeError)

A released message went out, and the ledger then failed to record that it did.

#### *classmethod* after(label, attempt, error, , reject)

The error for `label`, which `attempt` sent and the ledger failed to record.

`reject` is the command that takes the held message off, so it is not sent twice,
or None when there is no record to take it off.

* **Return type:**
  [`DraftSentNotRecorded`](#liaise.release.DraftSentNotRecorded)

### liaise.release.GITHUB_CHANNEL *= 'github'*

The channel whose references name repositories.

### *class* liaise.release.SendAttempt(decision, result=None, failure=None, failure_kind=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What [`gate_and_send()`](#liaise.release.gate_and_send) did with one message.

`decision` is the gate’s. Once the gate has passed the message, `result` is
correspond’s `SendResult`, or None when sending raised. `failure` says why the
channel did not take the message, and is None once it did. `failure_kind` names that
failure: correspond’s `error_kind`, or the class of what was raised. A message the
gate diverted has none of the three.

#### *property* outbound *: [Outbound](liaise.gate.html.md#liaise.gate.Outbound) | [None](https://docs.python.org/3/builtins/constants.html#None)*

The message as the gate’s filters left it, or None when the gate diverted it.

#### *property* sent *: [bool](https://docs.python.org/3/builtins/functions.html#bool)*

Whether the channel took the message; in a dry run, whether it would have.

### liaise.release.error_text(error)

How liaise names an exception it recovered from: its class, then its message.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> error_text(ValueError("no such channel"))
'ValueError: no such channel'
```

### liaise.release.gate_and_send(outbound, ctx, \*, registry=None, dry_run=False, outbound_filters=(<function reply_mode>, <function leak_scan>, <function writing_card>, <function deslop>, <function notify_recipient>))

Put `outbound` through the gate and, only when it passes, send it through correspond.

The gate is [`liaise.gate.run_gate()`](liaise.gate.html.md#liaise.gate.run_gate) with `outbound_filters`, and a message it
diverts is not sent. A passed message goes to `correspond.send` as the filters left
it, its title included, on `registry` (correspond’s own when None). `dry_run` asks
correspond for its plan and sends nothing. A channel that refuses the message, or
raises, becomes a `failure` on the attempt rather than an exception, so the caller
still has the message to keep.

* **Return type:**
  [`SendAttempt`](#liaise.release.SendAttempt)

### liaise.release.github_repo(ref)

`owner/repo`, lower-cased, of the repository a GitHub reference names: itself or one of its issues.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

```pycon
>>> github_repo("github:Example/App#12"), github_repo("github:example/app")
('example/app', 'example/app')
>>> github_repo("webinbox:example-site") is None
True
```

### liaise.release.release_draft(draft, \*, subject, ledger, label, reject, by, now, case=None, text=None, title=None, detail=mappingproxy({}), registry=None, send=True, dry_run=False, outbound_filters=(<function reply_mode>, <function leak_scan>, <function writing_card>, <function deslop>, <function notify_recipient>))

Release `draft` (a [`liaise.outcomes.make_draft()`](liaise.outcomes.html.md#liaise.outcomes.make_draft) item) as `by`, through the gate.

The message is the draft’s text and title, or `text` and `title` when the operator
edited them. It goes to the draft’s `ref`, for its `recipient`, carrying out its
`outcome`, on the case `case` or outside any when that is None. It passes through
[`gate_and_send()`](#liaise.release.gate_and_send) with an [`Approval`](liaise.model.html.md#liaise.model.Approval) by `by` at `now` on
the context: draft reply mode lets it through, and every other filter judges it as it
judges any message, the mention included.

It asks no one and records nothing. Its caller shows the operator the verdict first,
from a dry run, and records the outcome’s `entry` (with `detail` added to it) and,
when nothing went out, its `kept` draft. The kept draft holds the operator’s text
without the mention the gate adds, and the new reason. `send=False` asks the channel
only for its plan. A dry run judges and plans as a send would. `label` names the
message in errors, and `reject` is the command that takes it off.

Raises `ValueError`, sending nothing, for any of these:

- a draft with no destination, or no text;
- a `deliver` message a hold kept, whose delivery never ran;
- a hold on the subject, the recipient, the repository, the checkout or, for a
  `deliver` message, the delivery, that keeps effects waiting.

* **Return type:**
  [`DraftOutcome`](#liaise.release.DraftOutcome)

### liaise.release.sendable_ref(ref)

`ref` as a GitHub issue or repository reference, stripped and lower-cased.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> sendable_ref(" github:Example/App#12 ")
'github:example/app#12'
```

Raises `ValueError` for anything else: another channel, a malformed reference, or an
issue number that is not a positive whole number.
