# liaise.ledger

The ledger: liaise’s own record of what it has seen, opened, decided and started.

One [`Ledger`](#liaise.ledger.Ledger) over one `MutableMapping`: the store seam. The default is
[`default_ledger_store()`](#liaise.ledger.default_ledger_store), JSON files under `<state_dir>/ledger`. Every key is a
flat string with no “/”, since `dol.Jsons` would read one as a subdirectory:

```default
inbox__<sha1(delivery_id)[:16]>     {delivery_id, channel, kind, at}; presence means seen
case__<case_id>                     a Case
conversation__<encoded ref>         the id of the case that conversation belongs to
run__<run_id>                       a RunRecord
hold__<scope>                       a Hold
unrouted__<sha1(delivery_id)[:16]>  {delivery_id, subject, author, grade, reason, url, at}
cursor__<encoded ref>               a channel cursor (see Ledger.cursors)
counter__cases                      the last case number handed out
daily__<subject>__<YYYY-MM-DD>      that day's dispatches, as 0.0.x kept them
budget_notified__<subject>__<day>   when the operator heard that day's cap was reached
issue_check__<case_id>              an IssueCheck: the tick's reads of the case's issue state
```

The variable parts (ids, refs, scopes) are percent-encoded, so the scope
`repo:example/app` is stored under `hold__repo%3Aexample%2Fapp`. The result has
no “/”, no “:” (which a Windows filename cannot hold), and decodes back exactly.

**Dry runs.** Wrap the store as `ChainMap({}, store)`. Reads see the real ledger,
while writes land in the dict in front and vanish with it. A delete there cannot
reach the store behind, so it leaves a `None` tombstone that every read treats as
absent.

### Module Attributes

| [`DFLT_LEDGER_SUBDIR`](#liaise.ledger.DFLT_LEDGER_SUBDIR)   | The ledger's directory under `state_dir`.                                 |
|-----------------------------------------------------------------------|---------------------------------------------------------------------------|
| [`DFLT_DIGEST_LENGTH`](#liaise.ledger.DFLT_DIGEST_LENGTH)   | How many hex digits of a delivery id's sha1 inbox and unrouted keys keep. |

### Functions

| [`default_ledger_store`](#liaise.ledger.default_ledger_store)(state_dir)   | The default ledger store: one JSON file per key in `<state_dir>/ledger`.   |
|------------------------------------------------------------------------------------|----------------------------------------------------------------------------|

### Classes

| [`Ledger`](#liaise.ledger.Ledger)(store)   | What liaise has seen, its cases, runs and holds, the unrouted queue and the cursors.   |
|------------------------------------------------------------------|----------------------------------------------------------------------------------------|

### liaise.ledger.DFLT_DIGEST_LENGTH *= 16*

How many hex digits of a delivery id’s sha1 inbox and unrouted keys keep.

### liaise.ledger.DFLT_LEDGER_SUBDIR *= 'ledger'*

The ledger’s directory under `state_dir`.

### *class* liaise.ledger.Ledger(store)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What liaise has seen, its cases, runs and holds, the unrouted queue and the cursors.

`store` is any `MutableMapping` of JSON-ready values: a `dict` in tests,
[`default_ledger_store()`](#liaise.ledger.default_ledger_store) for real, `ChainMap({}, store)` for a dry run. The
ledger keeps no state of its own, so two ledgers over one store agree.

#### add_conversation(case_id, encoded_ref)

Attach `encoded_ref` to the case, indexing it. Idempotent.

* **Return type:**
  [`Case`](liaise.model.html.md#liaise.model.Case)

#### add_unrouted(\*\*fields)

Queue a message that matched a binding but failed resolution, grade or permission.

The fields are the spec’s `delivery_id, subject, author, grade, reason, url,
at`; only `delivery_id` is required, since the queue is keyed and
deduplicated on it.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### append(case_id, entry)

Append `entry` to the case’s history and save it.

* **Return type:**
  [`Case`](liaise.model.html.md#liaise.model.Case)

#### case_for_conversation(encoded_ref)

The case `encoded_ref` belongs to, or None.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Case`](liaise.model.html.md#liaise.model.Case)]

#### cases(, subject=None, state=None)

Every case, or those of `subject` and/or in `state`, in no set order.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Case`](liaise.model.html.md#liaise.model.Case)]

#### clear_hold(scope)

Lift the hold on `scope`. A scope with no hold is left as it is.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### *property* cursors *: [MutableMapping](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [str](https://docs.python.org/3/builtins/stdtypes.html#str)]*

The channel cursors, keyed by encoded conversation ref.

Hand it to correspond as `listen(ref, cursors=ledger.cursors)`.

#### daily_cap_notified(subject, day)

Whether the operator has been told that `subject` reached its cap on `day`.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

#### daily_count(subject, day)

How many dispatches `subject` had on `day` (a date, or `YYYY-MM-DD`).

* **Return type:**
  [`int`](https://docs.python.org/3/builtins/functions.html#int)

#### decrement_daily(subject, day)

Take back one dispatch counted for `subject` on `day`, returning the new count.

For a run that turned out not to count against the cap, such as one that found the
login expired. A day with no dispatches stays at zero, and nothing is written.

* **Return type:**
  [`int`](https://docs.python.org/3/builtins/functions.html#int)

#### get_case(case_id)

The case with `case_id`, or None.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Case`](liaise.model.html.md#liaise.model.Case)]

#### get_hold(scope)

The hold on `scope`, or None.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Hold`](liaise.model.html.md#liaise.model.Hold)]

#### get_issue_check(case_id)

The tick’s reads of the case’s issue state; an empty `IssueCheck` before any.

* **Return type:**
  [`IssueCheck`](liaise.model.html.md#liaise.model.IssueCheck)

#### get_run(run_id)

The record of the run `run_id`, or None.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`RunRecord`](liaise.model.html.md#liaise.model.RunRecord)]

#### holds()

Every hold, in no set order.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Hold`](liaise.model.html.md#liaise.model.Hold)]

#### increment_daily(subject, day)

Count one more dispatch for `subject` on `day`, returning the new count.

* **Return type:**
  [`int`](https://docs.python.org/3/builtins/functions.html#int)

#### mark_daily_cap_notified(subject, day, , at)

Record that the operator was told, `at`, that `subject` reached its cap on `day`.

[`daily_cap_notified()`](#liaise.ledger.Ledger.daily_cap_notified) then says so, so they are told once per subject per day.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### mark_seen(delivery_id, , channel, kind, at)

Record the event with `delivery_id` as taken in, so [`seen()`](#liaise.ledger.Ledger.seen) dedupes it.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### new_case(subject, conversation, , reporter, at)

Open a case on `conversation` (an encoded ref), numbered `<subject>-<n>`.

The case starts in `intake`. Raises `ValueError`, handing out no number, when
the conversation already belongs to a case: its messages go to that case.

* **Return type:**
  [`Case`](liaise.model.html.md#liaise.model.Case)

#### runs(, status=None)

Every run record, or those with `status`, in no set order.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`RunRecord`](liaise.model.html.md#liaise.model.RunRecord)]

#### save_case(case)

Write `case`, and index each of its conversations to it.

Raises `ValueError`, writing nothing, when one of its conversations belongs
to another case.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### save_issue_check(case_id, check)

Write `check`, replacing what the ledger held of the case’s issue state reads.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### save_run(record)

Write `record`, replacing any earlier record of that run.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### seen(delivery_id)

Whether the event with `delivery_id` has already been taken in.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

#### set_hold(hold)

Put `hold` on its scope, replacing any hold already there.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### transition(case_id, state, , at, actor=None, reason='')

Move the case to `state`, recording a `transition` entry.

Raises `ValueError`, writing nothing, for a state outside `CASE_STATES`.

* **Return type:**
  [`Case`](liaise.model.html.md#liaise.model.Case)

#### unrouted()

Every queued unrouted message, as a fresh dict, in no set order.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]]

### liaise.ledger.default_ledger_store(state_dir)

The default ledger store: one JSON file per key in `<state_dir>/ledger`.

Creates the directory, since `dol.Jsons` will not create one on write.

* **Return type:**
  [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]
