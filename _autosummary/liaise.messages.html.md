# liaise.messages

Messages outside a case: what an agent says to a person on its own initiative, through the gate.

A case is a unit of feedback work, with a reporter, a state machine, runs and a delivery. A
question an agent decides to ask has none of those, so it is not a case, and `liaise
message send` opens none (liaise #28). It is an [`OutboundMessage`](liaise.model.html.md#liaise.model.OutboundMessage) in
the ledger, judged by the same gate as every message liaise sends, with no case on the
gate’s context.

- [`send_message()`](#liaise.messages.send_message) takes a GitHub issue, or a repository and a title to open an issue,
  and the subject from the bindings that take it in
  ([`liaise.subjects.subject_for_ref()`](liaise.subjects.html.md#liaise.subjects.subject_for_ref)), so the caller cannot pick a laxer policy. The
  gate judges it with that subject’s policy and no case on its context, which in 0.1 means
  it is held for the operator: its sender chose where it goes, and only the operator’s
  release lets such a message out (see `liaise.gate.reply_mode()`). The operator is
  told, without its text, when a subject’s held queue stops being empty. A hold on the
  subject, the recipient, the repository or the checkout keeps it before the gate judges
  it. Every message is recorded.
- [`send_held_message()`](#liaise.messages.send_held_message) is how the operator sends a held one, through
  [`liaise.release.release_draft()`](liaise.release.html.md#liaise.release.release_draft), as a case’s draft is sent.
- [`reject_message()`](#liaise.messages.reject_message) records that the operator declined one, and why.
- [`message_lines()`](#liaise.messages.message_lines) and [`message_show_lines()`](#liaise.messages.message_show_lines) are `liaise message list` and
  `liaise message show`.

A message has no label, so nothing here writes to a repository beyond the message itself.

### Module Attributes

| [`MESSAGE_PURPOSES`](#liaise.messages.MESSAGE_PURPOSES)   | a question, a reply or a proposal.                                              |
|---------------------------------------------------------------------|---------------------------------------------------------------------------------|
| [`DFLT_SENDER`](#liaise.messages.DFLT_SENDER)        | an agent, never the operator, whose word is what releases a held one.           |
| [`OPERATOR_ACTOR`](#liaise.messages.OPERATOR_ACTOR)     | Who a held message is released or rejected by.                                  |
| [`SEND_REFUSED_CAUSE`](#liaise.messages.SEND_REFUSED_CAUSE) | What a notification names a failed send by when the channel gave no error kind. |

### Functions

| [`find_message`](#liaise.messages.find_message)(ledger, message_id)                   | The message `message_id`.                                                                                                                                   |
|-----------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`held_message`](#liaise.messages.held_message)(ledger, message_id, \*[, verb])       | The held message `message_id`.                                                                                                                              |
| [`message_draft`](#liaise.messages.message_draft)(message)                             | `message` as a held draft: the shape [`liaise.release.release_draft()`](liaise.release.html.md#liaise.release.release_draft) releases. |
| [`message_lines`](#liaise.messages.message_lines)(store, \*[, state])                  | What `liaise message list` prints: `<id>\t<state>\t<purpose> to <person> on <ref>`.                                                                         |
| [`message_show_lines`](#liaise.messages.message_show_lines)(store, message_id, \*[, ...])   | What `liaise message show` prints: the message `message_id`, with its text.                                                                                 |
| [`reject_message`](#liaise.messages.reject_message)(ledger, message_id, \*, reason)     | Decline the held message `message_id` as `by`, recording `reason`; return it.                                                                               |
| [`send_held_message`](#liaise.messages.send_held_message)(ledger, subjects, ...[, ...])    | Send the held message `message_id` as `by`, through the gate again.                                                                                         |
| [`send_message`](#liaise.messages.send_message)(ledger, subjects, recipient, \*, ...) | Send `text` to `recipient` (a person id) at `ref` outside any case, or hold it.                                                                             |

### Classes

| [`MessageRelease`](#liaise.messages.MessageRelease)(draft, attempt, filters, ...)   | What [`send_held_message()`](#liaise.messages.send_held_message) did, as [`liaise.cases.DraftRelease`](liaise.cases.html.md#liaise.cases.DraftRelease) does for a case.   |
|-------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`MessageSent`](#liaise.messages.MessageSent)(message, attempt, filters[, hold]) | What [`send_message()`](#liaise.messages.send_message) did: the message as recorded, the gate's attempt, any hold.                                                                          |

### liaise.messages.DFLT_SENDER *= 'agent'*

an agent, never the
operator, whose word is what releases a held one.

* **Type:**
  Who a message is recorded as sent by when its sender does not say

### liaise.messages.MESSAGE_PURPOSES *= ('ask', 'reply', 'propose')*

a question, a reply or a proposal.

* **Type:**
  What a message outside a case may carry out

### *class* liaise.messages.MessageRelease(draft, attempt, filters, edited, message, approval=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What [`send_held_message()`](#liaise.messages.send_held_message) did, as [`liaise.cases.DraftRelease`](liaise.cases.html.md#liaise.cases.DraftRelease) does for a case.

`draft` is the held message as the operator saw it, `attempt` the gate’s decision
and the send, `filters` how many filters ran, `edited` whether the operator’s text
replaced the message’s, and `message` the record as the release left it.
`approval` is the approval the gate was given.

### *class* liaise.messages.MessageSent(message, attempt, filters, hold=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What [`send_message()`](#liaise.messages.send_message) did: the message as recorded, the gate’s attempt, any hold.

`message` is the record (as it would be, in a dry run). `attempt` is None when
`hold` kept the message before the gate judged it, and `filters` is how many
filters the gate had.

#### *property* sent *: [bool](https://docs.python.org/3/builtins/functions.html#bool)*

Whether the message went out; in a dry run, whether it would have.

### liaise.messages.OPERATOR_ACTOR *= 'operator'*

Who a held message is released or rejected by.

### liaise.messages.SEND_REFUSED_CAUSE *= 'refused'*

What a notification names a failed send by when the channel gave no error kind.

### liaise.messages.find_message(ledger, message_id)

The message `message_id`. Raises `ValueError` for one the ledger does not hold.

* **Return type:**
  [`OutboundMessage`](liaise.model.html.md#liaise.model.OutboundMessage)

### liaise.messages.held_message(ledger, message_id, , verb='send')

The held message `message_id`. Raises `ValueError` for one missing or not held.

`verb` says, in the error, what there is nothing to do.

* **Return type:**
  [`OutboundMessage`](liaise.model.html.md#liaise.model.OutboundMessage)

### liaise.messages.message_draft(message)

`message` as a held draft: the shape [`liaise.release.release_draft()`](liaise.release.html.md#liaise.release.release_draft) releases.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### liaise.messages.message_lines(store, , state=None)

What `liaise message list` prints: `<id>\t<state>\t<purpose> to <person> on <ref>`.

Every message outside a case in the ledger `store`, or only those in `state`,
oldest first. Reads only. Raises `ValueError` for a state outside
[`MESSAGE_STATES`](liaise.model.html.md#liaise.model.MESSAGE_STATES).

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.messages.message_show_lines(store, message_id, , entries=12)

What `liaise message show` prints: the message `message_id`, with its text.

Its subject, state, recipient, reference, purpose and title; why it is held, with the
gate’s notes; the gate’s last flow and the audience in words, its whole text with
invisible characters made visible, and every link in full
([`liaise.cases.held_lines()`](liaise.cases.html.md#liaise.cases.held_lines)); and its `entries` latest entries. Reads only.
Raises `ValueError` for a message the ledger `store` does not hold.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.messages.reject_message(ledger, message_id, , reason, by='operator', now=None, dry_run=False)

Decline the held message `message_id` as `by`, recording `reason`; return it.

Nothing is sent. The message is recorded as rejected, with an entry keeping its text,
where it would have gone, why it was held and `reason`. A dry run writes nothing.

Raises `ValueError`, writing nothing, for a blank `reason`, a message the ledger
does not hold, and one that is not held.

* **Return type:**
  [`OutboundMessage`](liaise.model.html.md#liaise.model.OutboundMessage)

### liaise.messages.send_held_message(ledger, subjects, message_id, \*, by, text=None, title=None, seen=None, now=None, registry=None, send=True, dry_run=False, outbound_filters=(<function outside_a_case>, <function outbound_policy>, <function writing_card>, <function deslop>, <function notify_recipient>), approval=None, approve_shown=False, justification='', fingerprint_key=None)

Send the held message `message_id` as `by`, through the gate again.

It goes out through [`liaise.release.release_draft()`](liaise.release.html.md#liaise.release.release_draft) with `by`’s approval on the
context, as a case’s draft does ([`liaise.cases.send_draft()`](liaise.cases.html.md#liaise.cases.send_draft)), with `text` and
`title` when the operator edited them. `by` has no default: this function asks no
one, and `liaise message send-draft` passes the operator only after asking at a
terminal. Sent, the message is recorded as sent. Diverted or refused,
it stays held with the text that was judged and the new reason. Either way an entry
by `by` records the attempt. `seen` is the message as the operator saw it
([`message_draft()`](#liaise.messages.message_draft)): one that changed since is not sent. `send=False`,
`dry_run`, `approval` (the dry run’s, bound to what the operator was shown),
`approve_shown`, `justification` and `fingerprint_key` are as
[`release_draft()`](liaise.release.html.md#liaise.release.release_draft) has them: with neither an approval nor
`approve_shown`, nothing is settled and a held message stays held.

Raises `ValueError`, sending and writing nothing, for a message the ledger does not
hold, one that is not held, one whose subject is not in `subjects`, one that changed
since `seen`, and anything [`release_draft()`](liaise.release.html.md#liaise.release.release_draft) refuses.

* **Return type:**
  [`MessageRelease`](#liaise.messages.MessageRelease)

### liaise.messages.send_message(ledger, subjects, recipient, \*, ref, text, title=None, purpose='ask', by='agent', now=None, registry=None, notify_fn=None, dry_run=False, outbound_filters=(<function outside_a_case>, <function outbound_policy>, <function writing_card>, <function deslop>, <function notify_recipient>), fingerprint_key=None)

Send `text` to `recipient` (a person id) at `ref` outside any case, or hold it.

`ref` is a GitHub issue a subject binds (`github:example/app#12`), or a repository
it binds with a `title`, to open an issue there; it is kept as
[`sendable_ref()`](liaise.release.html.md#liaise.release.sendable_ref) gives it. Its subject is the one
[`subject_for_ref()`](liaise.subjects.html.md#liaise.subjects.subject_for_ref) names. The message is judged by the gate
(`outbound_filters`) with no case on the context, then sent through correspond on
`registry`, or held:

- **Held**, because the gate diverted it (in 0.1 it always does, for the operator to
  release), its channel refused it, or a hold on the subject, the recipient, the
  repository or the checkout keeps effects waiting. It is recorded as held, with the
  reason and the text as given. When no other message of the subject was held yet,
  the operator is told through `notify_fn` (`liaise.notify.notify()` when None)
  that a message on the subject waits. The notification carries neither the text nor
  the recipient.
- **Sent**, when a gate without that rule passes it: recorded as sent, with the text
  as it went out and its url.

Each record’s one entry is by `by`, at `now`, with the gate’s audit record. The gate
judges it with its provenance unknown (nobody can say what its sender read), and
`fingerprint_key` is as [`GateContext`](liaise.gate.html.md#liaise.gate.GateContext) has it. A dry run judges
and plans the same, and records and tells nothing.

Raises `ValueError`, sending and recording nothing, for any of these:

- a purpose outside [`MESSAGE_PURPOSES`](#liaise.messages.MESSAGE_PURPOSES), or a blank recipient or text;
- a `ref` that is not a GitHub issue or repository, or that no subject binds;
- a title on an issue, or a repository with no title.

Raises [`DraftSentNotRecorded`](liaise.release.html.md#liaise.release.DraftSentNotRecorded) when the message went out and
the ledger failed to record it.

* **Return type:**
  [`MessageSent`](#liaise.messages.MessageSent)
