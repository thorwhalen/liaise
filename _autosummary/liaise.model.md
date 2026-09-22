# liaise.model

The liaise 0.1 data model: cases, ledger entries, outcomes, holds and runs.

Everything [`liaise.ledger`](liaise.ledger.md#module-liaise.ledger) keeps is one of these frozen dataclasses, stored as
the JSON-ready dict its `to_dict` returns and read back with `from_dict`:
datetimes as ISO-8601 strings, tuples as lists, enums (a correspond `Grade`) as
their values. `from_dict` rebuilds each field from its annotation, so a field’s
type is written down in one place.

The vocabularies live here too (case states, entry kinds, outcome kinds,
permissions, hold modes), so each has one home: [`liaise.projection`](liaise.projection.md#module-liaise.projection) takes its
label names from [`CASE_STATES`](#liaise.model.CASE_STATES).

Nothing in this module touches storage, a channel or the clock. The case helpers
are pure and take the time they record as an argument.

### Module Attributes

| [`CASE_STATES`](#liaise.model.CASE_STATES)        | \` label vocabulary, unchanged and in its order.                                                                                  |
|---------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------|
| [`INITIAL_CASE_STATE`](#liaise.model.INITIAL_CASE_STATE) | The state a new case opens in.                                                                                                    |
| [`ENTRY_KINDS`](#liaise.model.ENTRY_KINDS)        | What a [`LedgerEntry`](#liaise.model.LedgerEntry) records.                                                      |
| [`OUTCOME_KINDS`](#liaise.model.OUTCOME_KINDS)      | The closed vocabulary a processor run reports its outcomes in.                                                                    |
| [`PERMISSIONS`](#liaise.model.PERMISSIONS)        | What a role can grant on a subject (see [`liaise.subjects`](liaise.subjects.md#module-liaise.subjects)). |
| [`HOLD_MODES`](#liaise.model.HOLD_MODES)         | How a [`Hold`](#liaise.model.Hold) stops work in its scope.                                              |
| [`MESSAGE_STATES`](#liaise.model.MESSAGE_STATES)     | held for the operator, sent, or declined.                                                                                         |

### Functions

| [`require_one_of`](#liaise.model.require_one_of)(value, allowed, \*, what)   | Return `value` when it is in `allowed`; otherwise raise `ValueError` listing them.   |
|---------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------|
| [`to_jsonable`](#liaise.model.to_jsonable)(value)                         | `value` as plain JSON-ready data.                                                    |

### Classes

| [`Approval`](#liaise.model.Approval)(by, at[, payload_hash, ...])             | The operator's release of a held message, bound to the message and audience they saw.                            |
|----------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------|
| [`Case`](#liaise.model.Case)(id, subject, conversations, reporter, ...)   | One piece of work on a subject, from its first message to its delivery.                                          |
| [`Health`](#liaise.model.Health)(ok[, defer_until, error])                  | Whether a processor can take work now, and if not, until when or why.                                            |
| [`Hold`](#liaise.model.Hold)(scope, mode[, reason, set_by, set_at])       | A stop on work in `scope` (`global`, `subject:<slug>`, `repo:<o/r>`, ...).                                       |
| [`IssueCheck`](#liaise.model.IssueCheck)([read_at, failures])                   | The tick's reads of a case's GitHub issue state: when one last succeeded, and the failures since.                |
| [`LedgerEntry`](#liaise.model.LedgerEntry)(at, kind[, actor, grade, ...])        | One thing that happened on a case: appended, never changed.                                                      |
| [`OutboundMessage`](#liaise.model.OutboundMessage)(id, subject, recipient, ref, ...) | A message an agent sent, or asked to send, outside any case (`liaise message send`).                             |
| [`Outcome`](#liaise.model.Outcome)(kind[, text, questions, reason])          | One outcome a processor run reports: `kind` from [`OUTCOME_KINDS`](#liaise.model.OUTCOME_KINDS). |
| [`RunRecord`](#liaise.model.RunRecord)(run_id, case_id, subject, mode, ...)    | A processor run started on a case: how it was started, and where it is now.                                      |
| [`RunResult`](#liaise.model.RunResult)(run_id[, outcomes, usage, ...])         | What a finished run returned: its outcomes, what it cost, and how it ended.                                      |

### *class* liaise.model.Approval(by, at, payload_hash=None, audience_hash=None, verdict_id=None, justification='', rules_overridden=())

Bases: `_Record`

The operator’s release of a held message, bound to the message and audience they saw.

`by` released it at `at`. `payload_hash` and `audience_hash` are the hashes of
the verdict shown to them ([`liaise.policy.payload_hash()`](liaise.policy.md#liaise.policy.payload_hash),
[`liaise.policy.audience_hash()`](liaise.policy.md#liaise.policy.audience_hash)), and `verdict_id` names that verdict.
`rules_overridden` are the rules they released it past, and `justification` says
why, in their words.

The gate reads it from `liaise.gate.GateContext.approval` and runs every filter
again (liaise ADR 0002). The approval settles only a concern whose rule it names, whose
flow is at most `approve`, and only while both hashes still match the message and the
audience computed at send time: a changed text, title, recipient or readership voids it,
and a `refuse` is never settled. An approval without hashes binds to nothing. It is
recorded with the send.

#### binds(payload_hash, audience_hash)

Whether this approval was given for the message and audience these hashes name.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

```pycon
>>> from datetime import datetime, timezone
>>> at = datetime(2026, 9, 15, tzinfo=timezone.utc)
>>> Approval(by="operator", at=at, payload_hash="p", audience_hash="a").binds("p", "a")
True
>>> Approval(by="operator", at=at).binds("p", "a")  # bound to nothing
False
```

### liaise.model.CASE_STATES *= ('intake', 'paused', 'working', 'needs-partner', 'needs-owner', 'deployed', 'budget')*

\` label vocabulary, unchanged and in its
order. The design’s `delivered` is `deployed` here.

* **Type:**
  A case’s state
* **Type:**
  the 0.0.x 

  ```
  `
  ```

  liaise

### *class* liaise.model.Case(id, subject, conversations, reporter, state, created_at, updated_at, session_id=None, entries=(), drafts=(), outbox=(), defer_until=None)

Bases: `_Record`

One piece of work on a subject, from its first message to its delivery.

`id` is `<subject>-<n>`. `conversations` are the encoded refs
(`github:example/app#12`) whose messages belong to it; `reporter` is the
person who opened it; `state` is one of [`CASE_STATES`](#liaise.model.CASE_STATES). `entries` is the
append-only history and `drafts` the outbound messages diverted to the operator.
`outbox` holds the messages the gate gave `delay`: each waits, cancellable, until
its `release_at`, when the tick judges it again and sends it (liaise #38; see
[`liaise.outbox`](liaise.outbox.md#module-liaise.outbox)). `defer_until`, when set, is the earliest time the case may be dispatched again
(after a quota reset, a rate limit, or a busy workspace).

#### with_entry(entry)

This case with `entry` appended, and `updated_at` moved forward to it.

* **Return type:**
  [`Case`](#liaise.model.Case)

#### with_state(state, , at, actor=None, reason='')

This case in `state`, with a `transition` entry recording the change.

Raises `ValueError` for a state outside [`CASE_STATES`](#liaise.model.CASE_STATES).

* **Return type:**
  [`Case`](#liaise.model.Case)

### liaise.model.ENTRY_KINDS *= ('message', 'transition', 'outcome', 'gate', 'run', 'hold', 'projection', 'note')*

What a [`LedgerEntry`](#liaise.model.LedgerEntry) records. A `note` is a line of the operator’s digest,
which `liaise status` lists.

### liaise.model.HOLD_MODES *= ('block', 'drain', 'cancel')*

How a [`Hold`](#liaise.model.Hold) stops work in its scope.

### *class* liaise.model.Health(ok, defer_until=None, error=None)

Bases: `_Record`

Whether a processor can take work now, and if not, until when or why.

### *class* liaise.model.Hold(scope, mode, reason='', set_by=None, set_at=None)

Bases: `_Record`

A stop on work in `scope` (`global`, `subject:<slug>`, `repo:<o/r>`, …).

### liaise.model.INITIAL_CASE_STATE *= 'intake'*

The state a new case opens in.

### *class* liaise.model.IssueCheck(read_at=None, failures=0)

Bases: `_Record`

The tick’s reads of a case’s GitHub issue state: when one last succeeded, and the failures since.

`failures` counts the reads that failed in a row; a read that succeeds sets it back
to 0. With no read yet, `read_at` is None.

### *class* liaise.model.LedgerEntry(at, kind, actor=None, grade=None, permission=None, delivery_id=None, text=None, detail=<factory>)

Bases: `_Record`

One thing that happened on a case: appended, never changed.

`kind` is one of [`ENTRY_KINDS`](#liaise.model.ENTRY_KINDS). `actor` is a person id (for a
`message`, the person it is attributed to); `grade` and `permission` are
what access was judged on; `delivery_id` is the channel event it came from.
`detail` holds whatever else the kind needs, such as a transition’s `from`,
`to` and `reason`.

### liaise.model.MESSAGE_STATES *= ('held', 'sent', 'rejected')*

held for the operator, sent, or declined.

* **Type:**
  Where an [`OutboundMessage`](#liaise.model.OutboundMessage) stands

### liaise.model.OUTCOME_KINDS *= ('ask', 'reply', 'escalate', 'propose', 'deliver', 'decline', 'defer', 'note')*

The closed vocabulary a processor run reports its outcomes in. Validating an
[`Outcome`](#liaise.model.Outcome) (and treating `decline` as `escalate`) is `liaise.outcomes`’s job.

### *class* liaise.model.OutboundMessage(id, subject, recipient, ref, purpose, text, state, created_at, updated_at, title=None, reason=None, notes=(), entries=())

Bases: `_Record`

A message an agent sent, or asked to send, outside any case (`liaise message send`).

`id` is `<subject>-m<hex>`. `recipient` (a person id), `ref`, `purpose`,
`title` and `text` are the message, as it was last judged. `state` is one of
[`MESSAGE_STATES`](#liaise.model.MESSAGE_STATES), and while it is `held`, `reason` and `notes` say why.
`entries` is its append-only history of gate decisions, as a case keeps its own. A
message has no reporter, no run and no label: it is not a unit of work.

#### with_entry(entry)

This message with `entry` appended, and `updated_at` moved forward to it.

* **Return type:**
  [`OutboundMessage`](#liaise.model.OutboundMessage)

### *class* liaise.model.Outcome(kind, text='', questions=(), reason='')

Bases: `_Record`

One outcome a processor run reports: `kind` from [`OUTCOME_KINDS`](#liaise.model.OUTCOME_KINDS).

Not validated here: `liaise.outcomes` validates a run’s outcomes as a whole.

### liaise.model.PERMISSIONS *= ('report', 'request_work', 'approve_candidate')*

What a role can grant on a subject (see [`liaise.subjects`](liaise.subjects.md#module-liaise.subjects)).

### *class* liaise.model.RunRecord(run_id, case_id, subject, mode, status, started_at, pid=None, heartbeat_at=None, ended_at=None, session_id=None, stream_path=None, cancel_sent_at=None)

Bases: `_Record`

A processor run started on a case: how it was started, and where it is now.

`cancel_sent_at` is when the tick first cancelled the run for passing its wall clock:
the lost-run deadline counts from it (see [`liaise.tick`](liaise.tick.md#module-liaise.tick)).

### *class* liaise.model.RunResult(run_id, outcomes=(), usage=<factory>, cost_usd=None, rate_limit=None, error=None, session_id=None, summary='')

Bases: `_Record`

What a finished run returned: its outcomes, what it cost, and how it ended.

### liaise.model.require_one_of(value, allowed, , what)

Return `value` when it is in `allowed`; otherwise raise `ValueError` listing them.

* **Return type:**
  [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)

```pycon
>>> require_one_of("paused", CASE_STATES, what="case state")
'paused'
```

### liaise.model.to_jsonable(value)

`value` as plain JSON-ready data.

Datetimes become ISO-8601 strings, enums their values, records their
`to_dict()`, tuples lists. Mappings and lists are rebuilt rather than shared,
so storing the result can never alias the object it came from.

* **Return type:**
  [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)
