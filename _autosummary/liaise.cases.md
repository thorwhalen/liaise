# liaise.cases

Cases as the operator sees and moves them: `liaise case list`, `show`, `set-state`, `send-draft` and `reject-draft`.

The tick moves a case through its states on its own (see [`liaise.tick`](liaise.tick.md#module-liaise.tick)), except where
a state waits on the operator: nothing the tick does moves a `needs-owner` case on, and a
`deployed` case never starts again. [`set_case_state()`](#liaise.cases.set_case_state) is how the operator moves one,
recorded on the case as a `transition` entry by the operator.

A case’s `liaise:` label on GitHub is a projection of its state in the ledger, so
relabelling an issue by hand changes nothing, and the tick overwrites it. After
[`set_case_state()`](#liaise.cases.set_case_state), the label follows on the next tick.

**What a notification leaves out.** No operator notification carries anything a case holds
(see `liaise.notify.notice_body()`); it names the case and points at `liaise case
show`. [`case_show_lines()`](#liaise.cases.case_show_lines) is where the operator reads, on their own machine, the
drafts with their text, the escalation’s reason and a failed deploy’s output.

**Drafts.** A message liaise did not send stays on its case as a draft. It may have been
held by `draft` reply mode, diverted by another filter of the gate, kept by a hold,
refused by its channel, or it is an escalation’s text. [`send_draft()`](#liaise.cases.send_draft) is how the
operator sends one. It runs the gate again on the final text, with the operator’s
[`Approval`](liaise.model.md#liaise.model.Approval) on the context, so draft reply mode lets it through while
every other filter still judges it, an edited text included. [`reject_draft()`](#liaise.cases.reject_draft) records
that the operator declined one, and why.

### Module Attributes

| [`OPERATOR_ACTOR`](#liaise.cases.OPERATOR_ACTOR)         | Who a state set with [`set_case_state()`](#liaise.cases.set_case_state) is recorded as set by.                                                                                                                                                  |
|-------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`DFLT_OPERATOR_REASON`](#liaise.cases.DFLT_OPERATOR_REASON)   | The reason recorded for a state the operator set without giving one.                                                                                                                                                                                           |
| [`TICK_ONLY_STATES`](#liaise.cases.TICK_ONLY_STATES)       | `working` says a run is in flight, which only a start makes so.                                                                                                                                                                                                |
| [`NONE_SHOWN`](#liaise.cases.NONE_SHOWN)             | How the case commands print an empty or unset value.                                                                                                                                                                                                           |
| [`DFLT_SHOW_ENTRIES`](#liaise.cases.DFLT_SHOW_ENTRIES)      | How many of a case's latest entries `liaise case show` lists.                                                                                                                                                                                                  |
| [`SHOW_TEXT_CHARS`](#liaise.cases.SHOW_TEXT_CHARS)        | How many characters of an entry's text `liaise case show` puts on the entry's line.                                                                                                                                                                            |
| [`ESCALATION_KINDS`](#liaise.cases.ESCALATION_KINDS)       | The outcome kinds whose reason `liaise case show` gives as the last escalation's.                                                                                                                                                                              |
| [`TEXT_INDENT`](#liaise.cases.TEXT_INDENT)            | How `liaise case show` indents a draft's text and a deploy's output.                                                                                                                                                                                           |
| [`NEEDS_OWNER`](#liaise.cases.NEEDS_OWNER)            | The state a held message leaves its case waiting on the operator in.                                                                                                                                                                                           |
| [`STATE_AFTER_SENT_DRAFT`](#liaise.cases.STATE_AFTER_SENT_DRAFT) | Where a case in [`NEEDS_OWNER`](#liaise.cases.NEEDS_OWNER) goes once the operator sends its last draft, by the outcome that draft carries out: a question, a reply or a proposal now waits on the reporter, as it does when a run sends one. |
| [`AUDIENCE_UNKNOWN`](#liaise.cases.AUDIENCE_UNKNOWN)       | How a held message's audience reads when no verdict names it.                                                                                                                                                                                                  |

### Functions

| [`case_lines`](#liaise.cases.case_lines)(store, \*[, state])                    | What `liaise case list` prints: `<case id>\t<state>\t<conversations>` per case.                                    |
|----------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| [`case_show_lines`](#liaise.cases.case_show_lines)(store, case_id, \*[, entries])    | What `liaise case show` prints: the case `case_id`, with all a notification leaves out.                            |
| [`entry_line`](#liaise.cases.entry_line)(entry)                                 | One entry on one line: when, what, by whom, its detail, and the start of its text.                                 |
| [`find_draft`](#liaise.cases.find_draft)(ledger, case_id, \*[, index])          | `(index, draft)` of the case `case_id`, as [`pick_draft()`](#liaise.cases.pick_draft) picks it. |
| [`gate_summary`](#liaise.cases.gate_summary)(detail)                              | What a `gate` entry's `detail` says of its decision, as a held draft keeps it.                                     |
| [`held_lines`](#liaise.cases.held_lines)(text, \*[, gate, indent])              | A held message as the operator reads it before releasing it (discussion §5.7).                                     |
| [`pick_draft`](#liaise.cases.pick_draft)(case[, index])                         | `(index, draft)`: `case`'s draft at `index`, or its only draft when `index` is None.                               |
| [`reject_draft`](#liaise.cases.reject_draft)(ledger, case_id, \*, reason[, ...])  | Decline the case `case_id`'s draft at `index` as `by`, recording `reason`.                                         |
| [`send_draft`](#liaise.cases.send_draft)(ledger, subjects, case_id, \*[, ...])  | Send the case `case_id`'s draft at `index` as `by`, through the gate again.                                        |
| [`set_case_state`](#liaise.cases.set_case_state)(ledger, case_id, state, \*[, ...]) | Move the case `case_id` to `state` as the operator; return the case as it is now.                                  |

### Classes

| [`DraftRejection`](#liaise.cases.DraftRejection)(index, draft, case)                | What [`reject_draft()`](#liaise.cases.reject_draft) did: the draft it took off the case, and the case after it.   |
|----------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------|
| [`DraftRelease`](#liaise.cases.DraftRelease)(index, draft, attempt, filters, ...) | What [`send_draft()`](#liaise.cases.send_draft) did with one of a case's drafts.                                |

### liaise.cases.AUDIENCE_UNKNOWN *= 'not judged'*

How a held message’s audience reads when no verdict names it.

### liaise.cases.DFLT_OPERATOR_REASON *= 'set by the operator'*

The reason recorded for a state the operator set without giving one.

### liaise.cases.DFLT_SHOW_ENTRIES *= 12*

How many of a case’s latest entries `liaise case show` lists.

### *class* liaise.cases.DraftRejection(index, draft, case)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What [`reject_draft()`](#liaise.cases.reject_draft) did: the draft it took off the case, and the case after it.

### *class* liaise.cases.DraftRelease(index, draft, attempt, filters, edited, case, moved=None, approval=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What [`send_draft()`](#liaise.cases.send_draft) did with one of a case’s drafts.

`index` and `draft` are the draft as the case held it. `attempt` is the gate’s
decision and the send (see [`SendAttempt`](liaise.release.md#liaise.release.SendAttempt)), and `filters` is
how many filters the gate ran it through. `edited` says whether the operator’s text
replaced the draft’s. `case` is the case as the release left it, or would leave it in
a dry run, and `moved` is its `(from, to)` states when the send moved it on.
`approval` is the approval the gate was given: the one to pass back to send exactly
what was judged.

### liaise.cases.ESCALATION_KINDS *= ('escalate', 'decline')*

The outcome kinds whose reason `liaise case show` gives as the last escalation’s.

### liaise.cases.NEEDS_OWNER *= 'needs-owner'*

The state a held message leaves its case waiting on the operator in.

### liaise.cases.NONE_SHOWN *= '(none)'*

How the case commands print an empty or unset value.

### liaise.cases.OPERATOR_ACTOR *= 'operator'*

Who a state set with [`set_case_state()`](#liaise.cases.set_case_state) is recorded as set by.

### liaise.cases.SHOW_TEXT_CHARS *= 200*

How many characters of an entry’s text `liaise case show` puts on the entry’s line.

### liaise.cases.STATE_AFTER_SENT_DRAFT *= mappingproxy({'ask': 'needs-partner', 'reply': 'needs-partner', 'propose': 'needs-partner'})*

Where a case in [`NEEDS_OWNER`](#liaise.cases.NEEDS_OWNER) goes once the operator sends its last draft, by the
outcome that draft carries out: a question, a reply or a proposal now waits on the
reporter, as it does when a run sends one. Anything else leaves the state for the
operator to set. An escalation’s text may be a refusal, which a reply from the partner
must not restart work on. A `deliver` message does not make its delivery happen, and
the tick’s own notices (a nudge, the daily cap) move nothing.

### liaise.cases.TEXT_INDENT *= '    '*

How `liaise case show` indents a draft’s text and a deploy’s output.

### liaise.cases.TICK_ONLY_STATES *= ('working',)*

`working` says a run is in flight, which only a start makes so.

* **Type:**
  States only the tick sets

### liaise.cases.case_lines(store, , state=None)

What `liaise case list` prints: `<case id>\t<state>\t<conversations>` per case.

Every case in the ledger `store`, or only those in `state`, by subject and then
oldest first. Reads only. Raises `ValueError` for a state outside
[`CASE_STATES`](liaise.model.md#liaise.model.CASE_STATES).

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.cases.case_show_lines(store, case_id, , entries=12)

What `liaise case show` prints: the case `case_id`, with all a notification leaves out.

Its state and conversations; the reason of its last `escalate` or `decline`; its
last failed deploy, with the tail of the command’s output; each draft waiting for the
operator, with the gate’s flow, the audience in words, its whole text with invisible
characters made visible and every link in full ([`held_lines()`](#liaise.cases.held_lines)); each message held
in the outbox (liaise #38), with when it sends and how to cancel it, shown the same way;
and its
`entries` latest ledger entries, oldest first, a line each with its detail and the
start of its text. Reads only. Raises `ValueError` for a case the ledger `store`
does not hold.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.cases.entry_line(entry)

One entry on one line: when, what, by whom, its detail, and the start of its text.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cases.find_draft(ledger, case_id, , index=None)

`(index, draft)` of the case `case_id`, as [`pick_draft()`](#liaise.cases.pick_draft) picks it.

Raises `ValueError` for a case the ledger does not hold, and as [`pick_draft()`](#liaise.cases.pick_draft) does.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`int`](https://docs.python.org/3/builtins/functions.html#int), [`Mapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Mapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]]

### liaise.cases.gate_summary(detail)

What a `gate` entry’s `detail` says of its decision, as a held draft keeps it.

None for an entry that records no verdict (one written before liaise ADR 0002, a
rejection, a nudge).

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]]

### liaise.cases.held_lines(text, , gate=None, indent='    ')

A held message as the operator reads it before releasing it (discussion §5.7).

What the gate decided and the audience in words, when `gate` (a draft’s, or
[`gate_summary()`](#liaise.cases.gate_summary)’s) says; the text, each invisible or control character written as
`<U+XXXX>`; and every link and image destination in full, since a link’s title can
say one place and its destination another.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.cases.pick_draft(case, index=None)

`(index, draft)`: `case`’s draft at `index`, or its only draft when `index` is None.

Raises `ValueError`, saying which drafts there are, for a case with none, for an index
it holds no draft at, and for no index on a case holding several.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`int`](https://docs.python.org/3/builtins/functions.html#int), [`Mapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Mapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]]

### liaise.cases.reject_draft(ledger, case_id, , reason, index=None, by='operator', now=None, dry_run=False)

Decline the case `case_id`’s draft at `index` as `by`, recording `reason`.

The draft leaves the case, and a `gate` entry by `by`, stamped `now`, keeps its
text, where it would have gone, why it was held and `reason`. Nothing is sent, and the
case’s state stays as it is: move it on with [`set_case_state()`](#liaise.cases.set_case_state). A dry run writes
nothing.

Raises `ValueError`, writing nothing, for a blank `reason`, a case the ledger does
not hold, and a draft [`pick_draft()`](#liaise.cases.pick_draft) cannot pick.

* **Return type:**
  [`DraftRejection`](#liaise.cases.DraftRejection)

### liaise.cases.send_draft(ledger, subjects, case_id, \*, index=None, text=None, seen=None, by, now=None, registry=None, send=True, dry_run=False, outbound_filters=(<function outside_a_case>, <function outbound_policy>, <function writing_card>, <function deslop>, <function notify_recipient>), approval=None, approve_shown=False, justification='', fingerprint_key=None)

Send the case `case_id`’s draft at `index` as `by`, through the gate again.

The message is the draft’s text, or `text` when the operator edited it. It goes out
through [`liaise.release.release_draft()`](liaise.release.md#liaise.release.release_draft), with an [`Approval`](liaise.model.md#liaise.model.Approval)
by `by` at `now` (the current UTC time when None) on the gate’s context, bound to
the message and the audience its channel reports at send time. The approval settles
what it names and binds to, draft reply mode among them, and every other concern of the
gate holds, a `refuse` always.

It asks no one, and `by` has no default: the caller says who releases the draft. Its
caller shows the operator the message and the gate’s verdict first, from a dry run with
`approve_shown`, and passes the draft they saw as `seen` and that dry run’s
`approval`, as `liaise case send-draft` does after asking at a terminal. With
neither, nothing is settled and a draft the gate holds back stays held. A text, an
audience or a verdict that changed since the approval voids it, and nothing is sent.

- **Sent:** the draft leaves the case. A `gate` entry by `by` records the text as
  it went out, its url, the approval and why the draft was held. Once no draft is
  left, a case in `needs-owner` moves as [`STATE_AFTER_SENT_DRAFT`](#liaise.cases.STATE_AFTER_SENT_DRAFT) says.
- **Diverted, or refused by its channel:** nothing is sent. The draft stays at its
  index, now holding the text the operator gave (without the mention the gate adds)
  and the new reason, and a `gate` entry records the attempt.

`seen` is the draft as the operator read it: a draft that has changed since is not
sent. `send=False` asks the channel for its plan and sends nothing. A divert or a
refusal is then recorded as above, and a message the gate would pass changes nothing.
A dry run judges and plans as a send would, and writes nothing.

Raises `ValueError`, sending and writing nothing, for any of these:

- a case the ledger does not hold, one with a run in flight, or one whose subject is
  not in `subjects`;
- a draft [`pick_draft()`](#liaise.cases.pick_draft) cannot pick, or one that changed since `seen`;
- anything [`liaise.release.release_draft()`](liaise.release.md#liaise.release.release_draft) refuses: no destination or no text, a
  `deliver` message a hold kept, a hold that keeps the case’s effects waiting.

Raises `DraftSentNotRecorded` when the message went out and the ledger then
failed to record it.

* **Return type:**
  [`DraftRelease`](#liaise.cases.DraftRelease)

### liaise.cases.set_case_state(ledger, case_id, state, , reason='', now=None)

Move the case `case_id` to `state` as the operator; return the case as it is now.

The move is a `transition` entry whose actor is `operator`, with `reason` (or
[`DFLT_OPERATOR_REASON`](#liaise.cases.DFLT_OPERATOR_REASON)), stamped `now` (the current UTC time when None). A case
already in `state` is returned as it is, and nothing is recorded. Its GitHub labels
follow on the next tick.

Raises `ValueError`, writing nothing, for a state outside
[`CASE_STATES`](liaise.model.md#liaise.model.CASE_STATES) or in [`TICK_ONLY_STATES`](#liaise.cases.TICK_ONLY_STATES), for a case the ledger
does not hold, and for a case with a run in flight, whose state the tick sets when it
collects that run.

* **Return type:**
  [`Case`](liaise.model.md#liaise.model.Case)
