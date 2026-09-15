# liaise.readiness

Readiness: whether a case is ready to dispatch, read off its own ledger entries.

The 0.0.x arithmetic of `liaise.intake`, unchanged, over a
[`Case`](liaise.model.html.md#liaise.model.Case) instead of a GitHub issue:

- **Quiet window.** Ready once `quiet_minutes` have passed since the partner’s last
  activity: the case’s creation, or a `message` entry by one of `partner_persons`.
  Nothing else moves that clock. That excludes other people’s messages, liaise’s own
  entries, and `case.updated_at`, which every entry moves (H-2 in 0.0.x).
- **Go marker.** A partner message holding the go marker makes the case ready
  `go_minutes` after it, when that comes first.
- **Wait marker.** A partner message holding the wait marker, later than the last go
  marker, pauses the case until the next go marker, overriding both.

Markers are literal, case-insensitive substrings of a message entry’s `text`.

### Functions

| [`compute_readiness`](#liaise.readiness.compute_readiness)(case, \*, quiet_minutes, ...)   | Is `case` ready to dispatch, right now?                                    |
|----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------|
| [`last_partner_activity`](#liaise.readiness.last_partner_activity)(case, \*, partner_persons)  | The latest of: the case's creation, and the partner's own message entries. |

### Classes

| [`Readiness`](#liaise.readiness.Readiness)(ready, paused, last_activity, ...)   | The result of [`compute_readiness()`](#liaise.readiness.compute_readiness).   |
|-------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------|

### *class* liaise.readiness.Readiness(ready, paused, last_activity, countdown, reason)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

The result of [`compute_readiness()`](#liaise.readiness.compute_readiness).

### liaise.readiness.compute_readiness(case, , quiet_minutes, go_minutes, markers, partner_persons, now=None)

Is `case` ready to dispatch, right now?

Ready when `now - last_activity >= quiet_minutes`, or when a go marker appeared
and `now - marker_time >= go_minutes`. A wait marker later than the last go
marker pauses the case until the next go marker, overriding both.

* **Return type:**
  [`Readiness`](#liaise.readiness.Readiness)

### liaise.readiness.last_partner_activity(case, , partner_persons)

The latest of: the case’s creation, and the partner’s own message entries.

* **Return type:**
  [`datetime`](https://docs.python.org/3/library/datetime.html#datetime.datetime)
