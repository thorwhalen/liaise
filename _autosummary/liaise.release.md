# liaise.release

Releasing a message: through the gate, then through correspond, as one step.

Every message liaise sends goes through [`gate_and_send()`](#liaise.release.gate_and_send). That covers what the tick
sends for a run’s outcomes, the tick’s own notices, a message an agent sends outside any
case (`liaise message send`), and a draft the operator releases. It asks correspond who
can read the destination right then ([`audience_of()`](#liaise.release.audience_of), never cached), runs
[`liaise.gate.run_gate()`](liaise.gate.md#liaise.gate.run_gate) with that audience on the context, and only a message the gate
passed reaches `correspond.send`, as the filters left it. It records nothing in the
ledger: what a [`SendAttempt`](#liaise.release.SendAttempt) means for a case, a message or a notification is for its
caller to keep. Once a message has gone out, each recipient’s acquaint record is told which
labelled records it identified ([`liaise.outbound.record_disclosure()`](liaise.outbound.md#liaise.outbound.record_disclosure)).

**Releasing a held message.** [`release_draft()`](#liaise.release.release_draft) is the one way a message held for the
operator goes out, whether it waits on a case (`liaise case send-draft`) or outside any
(`liaise message send-draft`). It checks what must stop a release, computes the audience
afresh, and runs [`gate_and_send()`](#liaise.release.gate_and_send) with the operator’s [`Approval`](liaise.model.md#liaise.model.Approval)
on the context. That approval is bound to the payload hash and the audience hash of the
decision the operator was shown, so a text, a title or a readership that changed since
voids it, and the operator sees the new verdict instead of a send (liaise ADR 0002). It
hands back the ledger entry and the draft to keep, for the caller to record.

One path for every sender is what makes the gate a gate. A filter added to it applies to
all of them at once, and none of them has a way to send around it.

### Module Attributes

| [`DFLT_REFUSAL`](#liaise.release.DFLT_REFUSAL)        | Why a send failed when the channel said no without saying why.                    |
|----------------------------------------------------------------------|-----------------------------------------------------------------------------------|
| [`DELIVER_PURPOSE`](#liaise.release.DELIVER_PURPOSE)     | The outcome whose message announces a delivery.                                   |
| [`DRAFT_ENTRY_KIND`](#liaise.release.DRAFT_ENTRY_KIND)    | a gate decision, as the tick's are.                                               |
| [`GITHUB_CHANNEL`](#liaise.release.GITHUB_CHANNEL)      | The channel whose references name repositories.                                   |
| [`CASELESS_PROVENANCE`](#liaise.release.CASELESS_PROVENANCE) | its sender's reading is nobody's to vouch for.                                    |
| [`NO_ATTACHMENTS`](#liaise.release.NO_ATTACHMENTS)      | correspond's send takes none yet.                                                 |
| [`VALIDATION_KIND`](#liaise.release.VALIDATION_KIND)     | The failure kind of a message liaise refused to hand to its channel as it stands. |

### Functions

| [`audience_of`](#liaise.release.audience_of)(outbound, \*[, registry])             | Who can read `outbound`'s destination, asked of its channel now through correspond.                                                                          |
|----------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`error_text`](#liaise.release.error_text)(error)                                 | How liaise names an exception it recovered from: its class, then its message.                                                                                |
| [`gate_and_send`](#liaise.release.gate_and_send)(outbound, ctx, \*[, registry, ...]) | Put `outbound` through the gate and, only when it passes, send it through correspond.                                                                        |
| [`github_repo`](#liaise.release.github_repo)(ref)                                  | `owner/repo`, lower-cased, of the repository a GitHub reference names: itself or one of its issues.                                                          |
| [`release_draft`](#liaise.release.release_draft)(draft, \*, subject, ledger, ...)    | Release `draft` (a [`liaise.outcomes.make_draft()`](liaise.outcomes.md#liaise.outcomes.make_draft) item) as `by`, through the gate. |
| [`sendable_ref`](#liaise.release.sendable_ref)(ref)                                 | `ref` as a GitHub issue or repository reference, stripped and lower-cased.                                                                                   |

### Classes

| [`DraftOutcome`](#liaise.release.DraftOutcome)(attempt, filters, edited[, ...])   | What [`release_draft()`](#liaise.release.release_draft) did with one held message, for its caller to record.   |
|--------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------|
| [`SendAttempt`](#liaise.release.SendAttempt)(decision[, result, failure, ...])   | What [`gate_and_send()`](#liaise.release.gate_and_send) did with one message.                                  |

### Exceptions

| [`DraftSentNotRecorded`](#liaise.release.DraftSentNotRecorded)   | A released message went out, and the ledger then failed to record that it did.   |
|-------------------------------------------------------------------------|----------------------------------------------------------------------------------|

### liaise.release.CASELESS_PROVENANCE *= 'a message outside a case: nobody can say what its sender read'*

its sender’s reading is nobody’s to vouch for.

* **Type:**
  The provenance of a message outside a case

### liaise.release.DELIVER_PURPOSE *= 'deliver'*

The outcome whose message announces a delivery.

### liaise.release.DFLT_REFUSAL *= 'the channel refused it'*

Why a send failed when the channel said no without saying why.

### liaise.release.DRAFT_ENTRY_KIND *= 'gate'*

a gate decision, as the
tick’s are.

* **Type:**
  The entry kind a released or rejected draft is recorded as

### *class* liaise.release.DraftOutcome(attempt, filters, edited, entry=None, kept=None, approval=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What [`release_draft()`](#liaise.release.release_draft) did with one held message, for its caller to record.

`attempt` is the gate’s decision and the send, `filters` how many filters the gate
ran it through, and `edited` whether the operator’s text replaced the draft’s.
`entry` is the `gate` entry the release is recorded as. `kept` is the draft that
stays for the operator when nothing went out, and None once the message is sent. Both
are None for a plan (`send=False`) that the gate passed: nothing happened to record.
`approval` is the approval the gate was given, bound to what the operator was shown.

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

### liaise.release.NO_ATTACHMENTS *= 'correspond.send takes no attachments, so a message with some is not sent'*

correspond’s send takes none yet.

* **Type:**
  Why a passed message with attachments is not sent

### *class* liaise.release.SendAttempt(decision, result=None, failure=None, failure_kind=None, disclosure_failure=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What [`gate_and_send()`](#liaise.release.gate_and_send) did with one message.

`decision` is the gate’s. Once the gate has passed the message, `result` is
correspond’s `SendResult`, or None when sending raised. `failure` says why the
channel did not take the message, and is None once it did. `failure_kind` names that
failure: correspond’s `error_kind`, or the class of what was raised. A message the
gate diverted has none of the three. `disclosure_failure` says why a sent message’s
disclosure could not be written to its recipients’ acquaint records; None otherwise.

#### *property* outbound *: [Outbound](liaise.gate.md#liaise.gate.Outbound) | [None](https://docs.python.org/3/builtins/constants.html#None)*

The message as the gate’s filters left it, or None when the gate diverted it.

#### *property* sent *: [bool](https://docs.python.org/3/builtins/functions.html#bool)*

Whether the channel took the message; in a dry run, whether it would have.

### liaise.release.VALIDATION_KIND *= 'validation'*

The failure kind of a message liaise refused to hand to its channel as it stands.

### liaise.release.audience_of(outbound, , registry=None)

Who can read `outbound`’s destination, asked of its channel now through correspond.

The draft counts where its channel makes it count (an email’s copies). It never
raises: correspond resolves what it cannot compute to public, and so does anything that
fails before it can ask. Nothing is cached, so a release asks again at send time.

* **Return type:**
  `Audience`

### liaise.release.error_text(error)

How liaise names an exception it recovered from: its class, then its message.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> error_text(ValueError("no such channel"))
'ValueError: no such channel'
```

### liaise.release.gate_and_send(outbound, ctx, \*, registry=None, dry_run=False, outbound_filters=(<function outside_a_case>, <function outbound_policy>, <function writing_card>, <function deslop>, <function notify_recipient>))

Put `outbound` through the gate and, only when it passes, send it through correspond.

When the context carries no audience, it is computed now ([`audience_of()`](#liaise.release.audience_of)). The
gate is [`liaise.gate.run_gate()`](liaise.gate.md#liaise.gate.run_gate) with `outbound_filters`, and a message it holds
back is not sent. A passed message goes to `correspond.send` as the filters left it,
its title and copies included, on `registry` (correspond’s own when None); one with
attachments is not, since correspond sends none. `dry_run` asks correspond for its
plan and sends nothing. A channel that refuses the message, or raises, becomes a
`failure` on the attempt rather than an exception, so the caller still has the
message to keep. Once a real send succeeds, the recipients’ acquaint records are told
what it identified, and a failure there is `disclosure_failure`, never a raise.

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

### liaise.release.release_draft(draft, \*, subject, ledger, label, reject, by, now, case=None, text=None, title=None, detail=mappingproxy({}), registry=None, send=True, dry_run=False, outbound_filters=(<function outside_a_case>, <function outbound_policy>, <function writing_card>, <function deslop>, <function notify_recipient>), approval=None, approve_shown=False, justification='', fingerprint_key=None)

Release `draft` (a [`liaise.outcomes.make_draft()`](liaise.outcomes.md#liaise.outcomes.make_draft) item) as `by`, through the gate.

The message is the draft’s text and title, or `text` and `title` when the operator
edited them. It goes to the draft’s `ref`, for its `recipient`, carrying out its
`outcome`, on the case `case` or outside any when that is None. The audience is
asked of its channel now. The provenance is the case’s (see
[`liaise.outbound.case_provenance()`](liaise.outbound.md#liaise.outbound.case_provenance)), or unknown outside a case.

`approval` is the operator’s [`Approval`](liaise.model.md#liaise.model.Approval) of the decision they
were shown, bound to its hashes and to what that verdict flagged
([`liaise.gate.approval_for()`](liaise.gate.md#liaise.gate.approval_for)), as `liaise case send-draft` passes it after asking
at a terminal. It must be `by`’s. The message passes through [`gate_and_send()`](#liaise.release.gate_and_send)
with it on the context: what it binds to and names is settled, and every other concern
holds, a `refuse` always.

**Without an approval, nothing is settled**: a draft the gate holds back stays held,
even for a caller who says who releases it. `approve_shown` is how a caller that
shows the operator a decision and asks them releases it: the gate judges the message as
it stands, and the approval is `by`’s of exactly that decision, with
`justification`. Nothing else in this package sets it; `liaise case send-draft`
does, on the dry run it shows, and then sends what the operator answered to.
`fingerprint_key` is as [`GateContext`](liaise.gate.md#liaise.gate.GateContext) has it.

It asks no one and records nothing. Its caller records the outcome’s `entry` (with
`detail` added to it) and, when nothing went out, its `kept` draft. The kept draft
holds the operator’s text without the mention the gate adds, the new reason and what
the gate decided. `send=False` asks the channel only for its plan. A dry run judges
and plans as a send would. `label` names the message in errors, and `reject` is the
command that takes it off.

Raises `ValueError`, sending nothing, for any of these:

- a draft with no destination, or no text;
- a `deliver` message a hold kept, whose delivery never ran;
- a hold on the subject, the recipient, the repository, the checkout or, for a
  `deliver` message, the delivery, that keeps effects waiting;
- an `approval` given by someone other than `by`.

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
