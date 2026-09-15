# liaise.errors

Processor error taxonomy (design §3.6): classify how a run ended, and what the tick does.

A run is classified from its `claude` stream-JSON output. Structured fields are checked
before strings, and the first match wins, so a run that failed authentication and then
crashed is `auth_expired`, whose action (hold the processor, count no dispatch) is the right
one. A run whose final result is a success is never classified by strings it happens to
contain.

`ERROR_ACTIONS` is the single source of truth for what each class does to a case;
the tick consults it and decides nothing about error classes on its own.

### Module Attributes

| [`DFLT_QUOTA_DEFER`](#liaise.errors.DFLT_QUOTA_DEFER)   | Fallbacks and caps for deferrals, named rather than inlined.   |
|---------------------------------------------------------------------|----------------------------------------------------------------|

### Functions

| [`classify`](#liaise.errors.classify)(summary, \*[, stderr_text, ...])          | The error class of a finished run, or None for a success with valid outcomes.             |
|-----------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| [`classify_delivery_failure`](#liaise.errors.classify_delivery_failure)(output)                  | `effect_blocked` when a failed delivery says it was refused (CI minutes, billing, 403).   |
| [`defer_until`](#liaise.errors.defer_until)(error, \*, now[, attempt, rate_limit]) | When a case whose run ended in `error` may be dispatched again, or None for no deferral.  |
| [`parse_stream`](#liaise.errors.parse_stream)(lines)                                | Read stream-JSON lines: the final `result` event, the last rate-limit info, retry errors. |
| [`reset_time`](#liaise.errors.reset_time)(rate_limit)                             | When a rejected quota resets, from `rate_limit_info.resets_at` (epoch or ISO), if given.  |

### Classes

| [`ErrorAction`](#liaise.errors.ErrorAction)(state[, auto_hold, notify, ...])   | What the tick does with a case whose run ended in one error class.         |
|-------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------|
| [`StreamSummary`](#liaise.errors.StreamSummary)([result, rate_limit, ...])       | What classification and collection need from one run's stream-JSON output. |

### liaise.errors.DFLT_QUOTA_DEFER *= datetime.timedelta(seconds=1800)*

Fallbacks and caps for deferrals, named rather than inlined.

### *class* liaise.errors.ErrorAction(state, auto_hold=None, notify=False, counts=True, defer=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What the tick does with a case whose run ended in one error class.

`state` is the case’s next state, or None to leave it unchanged; `intake` makes the
case dispatchable again (its session id stays, so the next dispatch resumes).
`counts` is whether the run counts against the daily dispatch cap.

### *class* liaise.errors.StreamSummary(result=None, rate_limit=None, api_errors=(), event_count=0)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What classification and collection need from one run’s stream-JSON output.

### liaise.errors.classify(summary, , stderr_text='', timed_out=False, has_outcomes=True)

The error class of a finished run, or None for a success with valid outcomes.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.errors.classify_delivery_failure(output)

`effect_blocked` when a failed delivery says it was refused (CI minutes, billing, 403).

None for any other failure, which the tick reconciles to `needs-owner` as 0.0.x did.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.errors.defer_until(error, , now, attempt=0, rate_limit=None)

When a case whose run ended in `error` may be dispatched again, or None for no deferral.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`datetime`](https://docs.python.org/3/library/datetime.html#datetime.datetime)]

### liaise.errors.parse_stream(lines)

Read stream-JSON lines: the final `result` event, the last rate-limit info, retry errors.

Lines that are blank, not JSON, or not objects are skipped: a stream cut off mid-line
by a kill must still yield whatever came before it.

* **Return type:**
  [`StreamSummary`](#liaise.errors.StreamSummary)

### liaise.errors.reset_time(rate_limit)

When a rejected quota resets, from `rate_limit_info.resets_at` (epoch or ISO), if given.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`datetime`](https://docs.python.org/3/library/datetime.html#datetime.datetime)]
