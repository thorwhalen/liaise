# liaise.outbox

The delay outbox: messages the gate gave `delay`, held for a cancellable window (liaise #38).

A send that cannot be withdrawn, to an organisation-wide or public place, gets the flow
`delay` from the outbound policy (discussion 32 §5.4, the `irreversibility` row). The
tick does not send it at once, and does not hand it to the operator as a draft either: it
keeps it on the case’s `outbox` until `release_at`
(`policy.delay_minutes` later), tells the operator only that a message is held and for
how long, and sends it on the first tick at or after that time. Until then the operator
takes it off with `liaise case cancel-send CASE [INDEX]` ([`cancel_send()`](#liaise.outbox.cancel_send)).

**What a release re-checks.** Each item carries the outbox’s own
[`Approval`](liaise.model.md#liaise.model.Approval) ([`liaise.gate.hold_for()`](liaise.gate.md#liaise.gate.hold_for)), bound to the message, its
audience and its verdict as the gate judged them at hold time, and overriding only the
delay. At release the tick runs the whole gate again, with the audience asked of the channel
then and the case’s provenance read from the ledger then, and that approval on the context.
While all three hold, the delay is settled and the message goes out; if anything changed —
the repository went public, a stranger commented and tainted the case, the disclosure now
flags something else — the approval is void and the message becomes a draft for the
operator, with the new verdict. It is never held again.

**What else keeps it from going out** (checked by the tick, in [`release_block()`](#liaise.outbox.release_block), on
every tick and for every item, due or not): a message the conversation has moved past — a
new inbound message on the case, the operator setting its state, its issue read closed
([`moved_on()`](#liaise.outbox.moved_on)) — becomes a draft, since it answers a question that may no longer stand; an item
reached more than `policy.delay_stale_minutes` after its release becomes a draft, since
nobody was watching the window it relied on; and an item a crash left claimed becomes a
draft that says to check the channel first, since it may have gone out. An effect hold keeps
an item where it is.

**At most once.** The tick marks an item `claimed_at` and saves the case before it sends,
and takes it off after: a send is never repeated by liaise, and one whose outcome is unknown
goes to the operator.

Every transition is a `gate` entry on the case whose `decision` is one of
`OUTBOX_DECISIONS`, and no operator notification carries anything the message says.

### Module Attributes

| [`HOLD`](#liaise.outbox.HOLD)                | held, cancelled by the operator, made a draft (`divert`), or made a draft for being reached too late (`lapse`).                                        |
|----------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`CANCEL`](#liaise.outbox.CANCEL)              | held, cancelled by the operator, made a draft (`divert`), or made a draft for being reached too late (`lapse`).                                        |
| [`LAPSE`](#liaise.outbox.LAPSE)               | held, cancelled by the operator, made a draft (`divert`), or made a draft for being reached too late (`lapse`).                                        |
| [`INTERRUPTED`](#liaise.outbox.INTERRUPTED)         | held, cancelled by the operator, made a draft (`divert`), or made a draft for being reached too late (`lapse`).                                        |
| [`ISSUE_CLOSED_EVENT`](#liaise.outbox.ISSUE_CLOSED_EVENT)  | The `detail["event"]` of the `run` entry the tick writes on reading a case's issue closed (`liaise.tick.RUN_ISSUE_CLOSED`, which imports this module). |
| [`OUTBOX_ENTRY_KIND`](#liaise.outbox.OUTBOX_ENTRY_KIND)   | The entry kind every outbox transition is recorded as.                                                                                                 |
| [`DFLT_CANCELLED_BY`](#liaise.outbox.DFLT_CANCELLED_BY)   | the operator.                                                                                                                                          |
| [`DFLT_CANCEL_REASON`](#liaise.outbox.DFLT_CANCEL_REASON)  | The reason a cancel records when the operator gives none.                                                                                              |
| [`MOVED_ON_REASON`](#liaise.outbox.MOVED_ON_REASON)     | Why a held message the conversation moved past is a draft, not a send.                                                                                 |
| [`LAPSED_REASON`](#liaise.outbox.LAPSED_REASON)       | Why a held message reached long after its release is a draft.                                                                                          |
| [`ISSUE_CLOSED_REASON`](#liaise.outbox.ISSUE_CLOSED_REASON) | Why a held message whose issue is closed at its release is a draft.                                                                                    |
| [`INTERRUPTED_REASON`](#liaise.outbox.INTERRUPTED_REASON)  | Why a message whose release was interrupted is a draft.                                                                                                |

### Functions

| [`cancel_send`](#liaise.outbox.cancel_send)(ledger, case_id, \*[, index, ...])     | Take the case `case_id`'s held message at `index` out of the outbox, unsent.             |
|-----------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------|
| [`held_at`](#liaise.outbox.held_at)(item)                                      | When `item` was held.                                                                    |
| [`held_message`](#liaise.outbox.held_message)(item, \*, case_id)                    | The message `item` holds, as the tick hands it to the gate again.                        |
| [`held_provenance`](#liaise.outbox.held_provenance)(item)                              | The provenance `item` was held with, or None for a run's (read from the ledger again).   |
| [`hold_of`](#liaise.outbox.hold_of)(item)                                      | The outbox's approval `item` carries, for the context of its release.                    |
| [`is_due`](#liaise.outbox.is_due)(item, now)                                  | Whether `item`'s window has passed at `now`.                                             |
| [`make_held`](#liaise.outbox.make_held)(outbound, decision, \*, at, delay, seen) | One item of a case's `outbox`: `outbound`, which `decision` gave `delay`, JSON-ready.    |
| [`moved_on`](#liaise.outbox.moved_on)(case, item)                               | What moved the conversation past `item` since it was planned, or None.                   |
| [`pick_held`](#liaise.outbox.pick_held)(case[, index])                           | `(index, item)`: `case`'s held message at `index`, or its only one when `index` is None. |
| [`release_at`](#liaise.outbox.release_at)(item)                                   | When `item` may go out.                                                                  |
| [`release_block`](#liaise.outbox.release_block)(case, item, \*, now, stale_after)    | `(decision, reason)` when `item`, due at `now`, must go to the operator instead.         |

### Classes

| [`Cancellation`](#liaise.outbox.Cancellation)(index, item, case)   | What [`cancel_send()`](#liaise.outbox.cancel_send) did: the `index` and `item` it took off, and the `case` after.   |
|------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------|

### liaise.outbox.CANCEL *= 'cancel'*

held, cancelled by the operator,
made a draft (`divert`), or made a draft for being reached too late (`lapse`). A
release that goes out is an ordinary `send` entry, with the hold as its approval.

* **Type:**
  The `decision` of a `gate` entry about the outbox

### *class* liaise.outbox.Cancellation(index, item, case)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What [`cancel_send()`](#liaise.outbox.cancel_send) did: the `index` and `item` it took off, and the `case` after.

### liaise.outbox.DFLT_CANCELLED_BY *= 'operator'*

the operator.

* **Type:**
  Who cancels a held message when nobody says

### liaise.outbox.DFLT_CANCEL_REASON *= 'cancelled by the operator'*

The reason a cancel records when the operator gives none.

### liaise.outbox.HOLD *= 'hold'*

held, cancelled by the operator,
made a draft (`divert`), or made a draft for being reached too late (`lapse`). A
release that goes out is an ordinary `send` entry, with the hold as its approval.

* **Type:**
  The `decision` of a `gate` entry about the outbox

### liaise.outbox.INTERRUPTED *= 'interrupted'*

held, cancelled by the operator,
made a draft (`divert`), or made a draft for being reached too late (`lapse`). A
release that goes out is an ordinary `send` entry, with the hold as its approval.

* **Type:**
  The `decision` of a `gate` entry about the outbox

### liaise.outbox.INTERRUPTED_REASON *= 'its release was interrupted at {claimed_at}, and it may have gone out: check {ref} before sending it'*

Why a message whose release was interrupted is a draft.

### liaise.outbox.ISSUE_CLOSED_EVENT *= 'issue_closed'*

The `detail["event"]` of the `run` entry the tick writes on reading a case’s issue
closed (`liaise.tick.RUN_ISSUE_CLOSED`, which imports this module).

### liaise.outbox.ISSUE_CLOSED_REASON *= 'its issue is closed: the operator decides whether it still goes'*

Why a held message whose issue is closed at its release is a draft.

### liaise.outbox.LAPSE *= 'lapse'*

held, cancelled by the operator,
made a draft (`divert`), or made a draft for being reached too late (`lapse`). A
release that goes out is an ordinary `send` entry, with the hold as its approval.

* **Type:**
  The `decision` of a `gate` entry about the outbox

### liaise.outbox.LAPSED_REASON *= 'held past its release by {late}, longer than the {limit} allowed: the operator decides'*

Why a held message reached long after its release is a draft.

### liaise.outbox.MOVED_ON_REASON *= 'the conversation moved on while this message was held ({what}): the operator decides'*

Why a held message the conversation moved past is a draft, not a send.

### liaise.outbox.OUTBOX_ENTRY_KIND *= 'gate'*

The entry kind every outbox transition is recorded as.

### liaise.outbox.cancel_send(ledger, case_id, , index=None, reason='', by='operator', now=None, dry_run=False)

Take the case `case_id`’s held message at `index` out of the outbox, unsent.

A `gate` entry by `by`, stamped `now`, keeps its text, where it would have gone,
when it would have, and `reason` ([`DFLT_CANCEL_REASON`](#liaise.outbox.DFLT_CANCEL_REASON) when blank). The case’s
state stays as it is. A dry run writes nothing. Raises `ValueError`, writing nothing,
for a case the ledger does not hold and an item [`pick_held()`](#liaise.outbox.pick_held) cannot pick.

* **Return type:**
  [`Cancellation`](#liaise.outbox.Cancellation)

### liaise.outbox.held_at(item)

When `item` was held.

* **Return type:**
  [`datetime`](https://docs.python.org/3/library/datetime.html#datetime.datetime)

### liaise.outbox.held_message(item, , case_id)

The message `item` holds, as the tick hands it to the gate again.

* **Return type:**
  [`Send`](liaise.outcomes.md#liaise.outcomes.Send)

### liaise.outbox.held_provenance(item)

The provenance `item` was held with, or None for a run’s (read from the ledger again).

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Provenance`](liaise.policy.md#liaise.policy.Provenance)]

### liaise.outbox.hold_of(item)

The outbox’s approval `item` carries, for the context of its release.

* **Return type:**
  [`Approval`](liaise.model.md#liaise.model.Approval)

### liaise.outbox.is_due(item, now)

Whether `item`’s window has passed at `now`.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### liaise.outbox.make_held(outbound, decision, , at, delay, seen, provenance=None)

One item of a case’s `outbox`: `outbound`, which `decision` gave `delay`, JSON-ready.

The message is kept **as it entered the gate** (no mention added): the hashes the hold
binds to are of that message, and the gate adds the mention again at release. The item
carries its `release_at` (`at` plus `delay`), the hold ([`liaise.gate.hold_for()`](liaise.gate.md#liaise.gate.hold_for)),
what the gate decided (`GateDecision.summary()`) and its notes, `seen` (how many
entries the run that wrote it could have read, for [`moved_on()`](#liaise.outbox.moved_on)), the
`provenance` it was judged with when the tick wrote it itself (None: a run’s, read
from the ledger again at release), and `claimed_at`, None until the tick starts
releasing it.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### liaise.outbox.moved_on(case, item)

What moved the conversation past `item` since it was planned, or None.

`item["seen"]` is how many entries the case had when the run’s actions were planned
(entries are append-only, so a count is exact where a time is not: a comment heard late
carries the time it was written). Among the entries after it, any of these moves it:

- a `message`, unless the channel’s own account wrote it (intake’s `self` role)
  with the text of a message liaise sent on the case: a comment of liaise’s heard back
  moves nothing, and one the operator wrote by hand on liaise’s account does;
- a `transition` by anyone but liaise itself: the operator set the case’s state;
- a `run` entry that read the case’s issue closed.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.outbox.pick_held(case, index=None)

`(index, item)`: `case`’s held message at `index`, or its only one when `index` is None.

Raises `ValueError`, saying which there are, for a case with none, an index it holds
nothing at, and no index on a case holding several.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`int`](https://docs.python.org/3/builtins/functions.html#int), [`Mapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Mapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]]

### liaise.outbox.release_at(item)

When `item` may go out.

* **Return type:**
  [`datetime`](https://docs.python.org/3/library/datetime.html#datetime.datetime)

### liaise.outbox.release_block(case, item, , now, stale_after)

`(decision, reason)` when `item`, due at `now`, must go to the operator instead.

In this order: a release a crash interrupted ([`INTERRUPTED`](#liaise.outbox.INTERRUPTED)), a conversation that
moved on since the run’s actions were planned (`divert`, [`moved_on()`](#liaise.outbox.moved_on)), and an item reached more than `stale_after` past
its release ([`LAPSE`](#liaise.outbox.LAPSE); None never lapses). None when nothing blocks it: the gate
decides the rest.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]]
