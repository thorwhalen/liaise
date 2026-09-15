# liaise.cases

Cases as the operator sees and moves them: `liaise case list`, `show` and `set-state`.

The tick moves a case through its states on its own (see [`liaise.tick`](liaise.tick.html.md#module-liaise.tick)), except where
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

### Module Attributes

| [`OPERATOR_ACTOR`](#liaise.cases.OPERATOR_ACTOR)       | Who a state set with [`set_case_state()`](#liaise.cases.set_case_state) is recorded as set by.   |
|-----------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------|
| [`DFLT_OPERATOR_REASON`](#liaise.cases.DFLT_OPERATOR_REASON) | The reason recorded for a state the operator set without giving one.                                            |
| [`TICK_ONLY_STATES`](#liaise.cases.TICK_ONLY_STATES)     | `working` says a run is in flight, which only a start makes so.                                                 |
| [`NONE_SHOWN`](#liaise.cases.NONE_SHOWN)           | How the case commands print an empty or unset value.                                                            |
| [`DFLT_SHOW_ENTRIES`](#liaise.cases.DFLT_SHOW_ENTRIES)    | How many of a case's latest entries `liaise case show` lists.                                                   |
| [`SHOW_TEXT_CHARS`](#liaise.cases.SHOW_TEXT_CHARS)      | How many characters of an entry's text `liaise case show` puts on the entry's line.                             |
| [`ESCALATION_KINDS`](#liaise.cases.ESCALATION_KINDS)     | The outcome kinds whose reason `liaise case show` gives as the last escalation's.                               |
| [`TEXT_INDENT`](#liaise.cases.TEXT_INDENT)          | How `liaise case show` indents a draft's text and a deploy's output.                                            |

### Functions

| [`case_lines`](#liaise.cases.case_lines)(store, \*[, state])                    | What `liaise case list` prints: `<case id>\t<state>\t<conversations>` per case.         |
|----------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------|
| [`case_show_lines`](#liaise.cases.case_show_lines)(store, case_id, \*[, entries])    | What `liaise case show` prints: the case `case_id`, with all a notification leaves out. |
| [`set_case_state`](#liaise.cases.set_case_state)(ledger, case_id, state, \*[, ...]) | Move the case `case_id` to `state` as the operator; return the case as it is now.       |

### liaise.cases.DFLT_OPERATOR_REASON *= 'set by the operator'*

The reason recorded for a state the operator set without giving one.

### liaise.cases.DFLT_SHOW_ENTRIES *= 12*

How many of a case’s latest entries `liaise case show` lists.

### liaise.cases.ESCALATION_KINDS *= ('escalate', 'decline')*

The outcome kinds whose reason `liaise case show` gives as the last escalation’s.

### liaise.cases.NONE_SHOWN *= '(none)'*

How the case commands print an empty or unset value.

### liaise.cases.OPERATOR_ACTOR *= 'operator'*

Who a state set with [`set_case_state()`](#liaise.cases.set_case_state) is recorded as set by.

### liaise.cases.SHOW_TEXT_CHARS *= 200*

How many characters of an entry’s text `liaise case show` puts on the entry’s line.

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
[`CASE_STATES`](liaise.model.html.md#liaise.model.CASE_STATES).

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.cases.case_show_lines(store, case_id, , entries=12)

What `liaise case show` prints: the case `case_id`, with all a notification leaves out.

Its state and conversations; the reason of its last `escalate` or `decline`; its
last failed deploy, with the tail of the command’s output; each draft waiting for the
operator, with its whole text; and its `entries` latest ledger entries, oldest first,
a line each with its detail and the start of its text. Reads only. Raises
`ValueError` for a case the ledger `store` does not hold.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.cases.set_case_state(ledger, case_id, state, , reason='', now=None)

Move the case `case_id` to `state` as the operator; return the case as it is now.

The move is a `transition` entry whose actor is `operator`, with `reason` (or
[`DFLT_OPERATOR_REASON`](#liaise.cases.DFLT_OPERATOR_REASON)), stamped `now` (the current UTC time when None). A case
already in `state` is returned as it is, and nothing is recorded. Its GitHub labels
follow on the next tick.

Raises `ValueError`, writing nothing, for a state outside
[`CASE_STATES`](liaise.model.html.md#liaise.model.CASE_STATES) or in [`TICK_ONLY_STATES`](#liaise.cases.TICK_ONLY_STATES), for a case the ledger
does not hold, and for a case with a run in flight, whose state the tick sets when it
collects that run.

* **Return type:**
  [`Case`](liaise.model.html.md#liaise.model.Case)
