# liaise.tick

The tick: one pass of liaise 0.1’s loop (design §3.1).

[`run_once()`](#liaise.tick.run_once) takes in what arrived, reconciles the runs in flight, starts the cases
that are ready, nudges quiet deliveries, and shows each touched case’s state on its GitHub
labels:

```default
1. intake     each subject's bindings, through correspond (liaise.intake)
2. reconcile  each run the tick has not collected: cancelled for a cancel hold or its
              wall clock, refreshed, and collected once finished, as timed_out only if
              it ended past its wall clock or the tick cancelled it for that; or given
              up as timed_out when it still runs LOST_RUN_DEADLINE after the tick
              cancelled it for its wall clock. The processor signals a run's process
              only once it has verified, by its start, that the pid is still the run's.
              An error takes its action from liaise.errors; a
              success's outcomes are planned (liaise.outcomes) and carried out, every
              message through the gate (liaise.gate). A deploy per issue runs right
              after its case's outcomes and batch deploys run last, once per subject;
              nothing tells a partner a change is live before its deploy succeeded.
              Last, each case left working with no run in flight, its run lost, goes
              to needs-owner.
3. start      each ready case, in the order the triage seam gives, that passes holds,
              authorization, budget, an open GitHub issue, preflight and the workspace
              check, as a detached processor run. A case whose issue was read closed
              has it read again at most once per CLOSED_RECHECK_INTERVAL.
4. nudge      each deployed case its partner has gone quiet on, once, unless its issue
              is closed
5. project    the state label of each case this tick touched, and of each whose state
              is not the one last projected (liaise.projection)
```

The tick keeps its own clock: `now` stamps every entry, and a run’s wall clock counts
from the tick that started it. The ledger’s record of a run says `running` until the
tick collects it, whatever the processor says, so a run that ended at once (a spawn that
failed, an `EchoProcessor` run) is still collected, on the next tick. Any exception a
processor verb raises is a `crashed` run, never the tick’s end (#24). One case’s failure
is a problem line, never the other cases’ end: a checkout release or a deploy that raises
hands its cases to the owner. No operator notification carries anything a case holds: its
body comes from `liaise.notify.notice_body()`, and `liaise case show` has the rest.

**Dry run.** The ledger is `Ledger(ChainMap({}, store))`: every step runs on real state,
and every write vanishes with the overlay. Nothing is sent (`correspond.send` gets
`dry_run=True`), labelled, started, cancelled, deployed, locked, stamped, notified or
written (the processor is asked with `persist=False`), and preflight, which runs a
process, is skipped. The report’s plan lines say what would be.

**One tick at a time.** A tick holds the run lock in `state_dir` ([`run_lock_path()`](#liaise.tick.run_lock_path))
and stamps its start and end in the store ([`run_stamps()`](#liaise.tick.run_stamps)), so `liaise status` can
tell a running tick from a finished or an interrupted one. [`status_lines()`](#liaise.tick.status_lines) is what
`liaise status` prints.

### Module Attributes

| [`ELIGIBLE_STATES`](#liaise.tick.ELIGIBLE_STATES)          | The states a case may be started from.                                                                                                                                         |
|---------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`DFLT_DEFER`](#liaise.tick.DFLT_DEFER)               | How long a `defer` outcome puts a case aside.                                                                                                                                  |
| [`DFLT_RUNS_SUBDIR`](#liaise.tick.DFLT_RUNS_SUBDIR)         | The processor runs' directory under `state_dir`.                                                                                                                               |
| [`DFLT_STATUS_RECENT`](#liaise.tick.DFLT_STATUS_RECENT)       | How many of the latest unrouted messages and digest notes [`status_lines()`](#liaise.tick.status_lines) lists.                                               |
| [`TICK_ACTOR`](#liaise.tick.TICK_ACTOR)               | The actor of the entries the tick writes.                                                                                                                                      |
| [`REQUEST_WORK`](#liaise.tick.REQUEST_WORK)             | The permission a case's reporter needs for the tick to start work on it.                                                                                                       |
| [`PROCESSOR_SCOPE`](#liaise.tick.PROCESSOR_SCOPE)          | The hold scope of the processor, which the tick holds itself after some errors.                                                                                                |
| [`AUTH_PROBE_INTERVAL`](#liaise.tick.AUTH_PROBE_INTERVAL)      | How long an automatic `processor` hold stands before the tick probes it with preflight, lifting it if preflight passes.                                                        |
| [`LOST_RUN_DEADLINE`](#liaise.tick.LOST_RUN_DEADLINE)        | How long after the tick first cancelled a run for passing its wall clock it waits for the run to stop.                                                                         |
| [`CLOSED_RECHECK_INTERVAL`](#liaise.tick.CLOSED_RECHECK_INTERVAL)  | How long a case whose issue was read closed goes before the tick reads its issue again, to notice a reopening.                                                                 |
| [`STATE_READ_FAILURE_LIMIT`](#liaise.tick.STATE_READ_FAILURE_LIMIT) | How many reads of a case's issue state in a row may fail for good before the case goes to the owner, who is told once: a deleted or transferred issue never reads again.       |
| [`PERMANENT_READ_ERRORS`](#liaise.tick.PERMANENT_READ_ERRORS)    | The kinds of correspond `ChannelError` by which a failed state read counts toward that limit: the issue is not there, or not for this account.                                 |
| [`DEPLOY_DELIVERY`](#liaise.tick.DEPLOY_DELIVERY)          | A deploy that runs the subject's command, and a delivery that stops at a pull request.                                                                                         |
| [`PR_ONLY_DELIVERY`](#liaise.tick.PR_ONLY_DELIVERY)         | A deploy that runs the subject's command, and a delivery that stops at a pull request.                                                                                         |
| [`BATCH_DELIVERY_PER`](#liaise.tick.BATCH_DELIVERY_PER)       | A deploy that runs once per tick, after every case of the subject has been collected, and one that runs for each case right after its outcomes.                                |
| [`ISSUE_DELIVERY_PER`](#liaise.tick.ISSUE_DELIVERY_PER)       | A deploy that runs once per tick, after every case of the subject has been collected, and one that runs for each case right after its outcomes.                                |
| [`RUN_ID_SUFFIX_DIGITS`](#liaise.tick.RUN_ID_SUFFIX_DIGITS)     | How many hex digits of a uuid4 end a run id, so that no two runs share one.                                                                                                    |
| [`BUDGET_MESSAGE`](#liaise.tick.BUDGET_MESSAGE)           | The messages the tick sends to a partner on its own, through the gate, which adds the mention: when the daily cap trips, when a batch deploy is live, and on a quiet delivery. |
| [`BUDGET_PURPOSE`](#liaise.tick.BUDGET_PURPOSE)           | The `purpose` of those messages, as the gate and the ledger see it.                                                                                                            |
| [`DAILY_CAP_PRIORITY`](#liaise.tick.DAILY_CAP_PRIORITY)       | news for the operator, not a call to act.                                                                                                                                      |
| [`SEND_REFUSED_CAUSE`](#liaise.tick.SEND_REFUSED_CAUSE)       | What a notification names a failed send by when the channel gave no error kind.                                                                                                |
| [`RUN_STARTED`](#liaise.tick.RUN_STARTED)              | a start, a start that failed, a collection...                                                                                                                                  |
| [`RUN_LOST`](#liaise.tick.RUN_LOST)                 | ...a case found `working` with no run in flight, its run lost...                                                                                                               |
| [`RUN_ISSUE_CLOSED`](#liaise.tick.RUN_ISSUE_CLOSED)         | ...and the case's GitHub issue found closed, then open again, each once per change.                                                                                            |
| [`RUN_DEPLOY_FAILED`](#liaise.tick.RUN_DEPLOY_FAILED)        | ...and a deploy that failed, the tail of its output the entry's text, which `liaise case show` prints and no notification carries.                                             |
| [`PLAN_TEXT_CHARS`](#liaise.tick.PLAN_TEXT_CHARS)          | How many characters of a message a plan line shows.                                                                                                                            |
| [`DEPLOY_OUTPUT_TAIL_CHARS`](#liaise.tick.DEPLOY_OUTPUT_TAIL_CHARS) | How many of a failed deploy's last output characters its `run` entry keeps.                                                                                                    |
| [`WorkspaceFactory`](#liaise.tick.WorkspaceFactory)         | the workspace seam (see [`liaise.workspace.workspace_for()`](liaise.workspace.md#liaise.workspace.workspace_for)).                                     |
| [`Triage`](#liaise.tick.Triage)                   | the triage seam (#19).                                                                                                                                                         |
| [`OWN_MESSAGE_PROVENANCE`](#liaise.tick.OWN_MESSAGE_PROVENANCE)   | a fixed text no run wrote, so nothing a run read is in it.                                                                                                                     |
| [`RUN_LOCK_FILE`](#liaise.tick.RUN_LOCK_FILE)            | The run lock's file under `state_dir`.                                                                                                                                         |
| [`RUN_STARTED_KEY`](#liaise.tick.RUN_STARTED_KEY)          | Where the store keeps a tick's start and end.                                                                                                                                  |
| [`LEGACY_LAST_RUN_KEY`](#liaise.tick.LEGACY_LAST_RUN_KEY)      | a pass's start, written once the pass had finished.                                                                                                                            |

### Functions

| [`last_run_age`](#liaise.tick.last_run_age)(store, \*[, now])                   | Seconds since the latest tick that was not a dry run started, or None before any.                                                           |
|---------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------|
| [`run_lock`](#liaise.tick.run_lock)(lock_path)                              | Hold the run lock while the block runs: a tick, or an operator's change between ticks.                                                      |
| [`run_lock_path`](#liaise.tick.run_lock_path)(state_dir)                         | Where a tick keeps its run lock.                                                                                                            |
| [`run_once`](#liaise.tick.run_once)(subjects, store, \*, global_config)     | One tick over `subjects` (slug to [`Subject`](liaise.subjects.md#liaise.subjects.Subject)), on the ledger `store`. |
| [`run_stamps`](#liaise.tick.run_stamps)(store, \*[, lock_path])               | The run stamps a tick keeps in `store`.                                                                                                     |
| [`status_lines`](#liaise.tick.status_lines)(subjects, store, \*, global_config) | What `liaise status` prints: what the ledger in `store` says, read only.                                                                    |

### Classes

| [`Diversion`](#liaise.tick.Diversion)(outbound, reason)                  | A message that did not go out: the gate diverted it, or a hold kept it.                              |
|-----------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------|
| [`RunStamps`](#liaise.tick.RunStamps)([started_at, ended_at, lock_held]) | When the latest tick that was not a dry run started and ended.                                       |
| [`TickReport`](#liaise.tick.TickReport)([plan_lines, dispatched, ...])    | What one [`run_once()`](#liaise.tick.run_once) did or, in a dry run, would do. |

### Exceptions

| [`RunLockHeld`](#liaise.tick.RunLockHeld)   | Another live liaise process holds the run lock, so this tick did not start.   |
|----------------------------------------------------------------|-------------------------------------------------------------------------------|

### liaise.tick.AUTH_PROBE_INTERVAL *= datetime.timedelta(seconds=1800)*

How long an automatic `processor` hold stands before the tick probes it with
preflight, lifting it if preflight passes. A login that has expired where preflight
cannot see it then costs one failed run, and one notification, per interval, not a tick.

### liaise.tick.BATCH_DELIVERY_PER *= 'batch'*

A deploy that runs once per tick, after every case of the subject has been collected,
and one that runs for each case right after its outcomes.

### liaise.tick.BUDGET_MESSAGE *= "Today's limit on automatic work has been reached. This will pick back up tomorrow."*

The messages the tick sends to a partner on its own, through the gate, which adds the
mention: when the daily cap trips, when a batch deploy is live, and on a quiet delivery.

### liaise.tick.BUDGET_PURPOSE *= 'budget'*

The `purpose` of those messages, as the gate and the ledger see it.

### liaise.tick.CLOSED_RECHECK_INTERVAL *= datetime.timedelta(seconds=3600)*

How long a case whose issue was read closed goes before the tick reads its issue again,
to notice a reopening. correspond’s GitHub poll reports neither a closing nor a
reopening, and each read costs the issue and its comment pages, so a closed case that is
otherwise ready is not read every tick.

### liaise.tick.DAILY_CAP_PRIORITY *= 'default'*

news for the operator, not a call to act.

* **Type:**
  The ntfy priority of the daily-cap notice

### liaise.tick.DEPLOY_DELIVERY *= 'deploy'*

A deploy that runs the subject’s command, and a delivery that stops at a pull request.

### liaise.tick.DEPLOY_OUTPUT_TAIL_CHARS *= 2000*

How many of a failed deploy’s last output characters its `run` entry keeps.

### liaise.tick.DFLT_DEFER *= datetime.timedelta(days=1)*

How long a `defer` outcome puts a case aside.

### liaise.tick.DFLT_RUNS_SUBDIR *= 'runs'*

The processor runs’ directory under `state_dir`.

### liaise.tick.DFLT_STATUS_RECENT *= 5*

How many of the latest unrouted messages and digest notes [`status_lines()`](#liaise.tick.status_lines) lists.

### *class* liaise.tick.Diversion(outbound, reason)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A message that did not go out: the gate diverted it, or a hold kept it.

### liaise.tick.ELIGIBLE_STATES *= ('intake', 'needs-partner', 'paused', 'budget')*

The states a case may be started from. `working`, `needs-owner` and `deployed`
wait on something other than the partner’s clock. `budget` is one, since the cap is
per day (0.0.x H-4).

### liaise.tick.ISSUE_DELIVERY_PER *= 'issue'*

A deploy that runs once per tick, after every case of the subject has been collected,
and one that runs for each case right after its outcomes.

### liaise.tick.LEGACY_LAST_RUN_KEY *= 'last_run'*

a pass’s start, written once the pass had finished.

* **Type:**
  0.0.3 and earlier kept one stamp

### liaise.tick.LOST_RUN_DEADLINE *= datetime.timedelta(seconds=600)*

How long after the tick first cancelled a run for passing its wall clock it waits for
the run to stop. A run its processor still reports running by then is finished as
`timed_out`, sent nothing more (its pid may be another process’s by then), and its
case goes to the owner. A run no tick has cancelled is never given up.

### liaise.tick.OWN_MESSAGE_PROVENANCE *= "the tick's own message, a fixed text no run wrote"*

a
fixed text no run wrote, so nothing a run read is in it.

* **Type:**
  The provenance of a message the tick writes itself (the daily-cap message, a nudge)

### liaise.tick.PERMANENT_READ_ERRORS *= frozenset({'not_found', 'permission'})*

The kinds of correspond `ChannelError` by which a failed state read counts toward that
limit: the issue is not there, or not for this account. Any other failure (the network,
a rate limit, a login, an exception) counts nothing, and the case waits for the next tick.

### liaise.tick.PLAN_TEXT_CHARS *= 72*

How many characters of a message a plan line shows.

### liaise.tick.PROCESSOR_SCOPE *= 'processor'*

The hold scope of the processor, which the tick holds itself after some errors.

### liaise.tick.PR_ONLY_DELIVERY *= 'pr_only'*

A deploy that runs the subject’s command, and a delivery that stops at a pull request.

### liaise.tick.REQUEST_WORK *= 'request_work'*

The permission a case’s reporter needs for the tick to start work on it.

### liaise.tick.RUN_DEPLOY_FAILED *= 'deploy_failed'*

…and a deploy that failed, the tail of its output the entry’s text, which `liaise
case show` prints and no notification carries.

### liaise.tick.RUN_ID_SUFFIX_DIGITS *= 8*

How many hex digits of a uuid4 end a run id, so that no two runs share one.

### liaise.tick.RUN_ISSUE_CLOSED *= 'issue_closed'*

…and the case’s GitHub issue found closed, then open again, each once per change. A
case whose issue is closed is neither started nor nudged.

### liaise.tick.RUN_LOCK_FILE *= 'run.lock'*

The run lock’s file under `state_dir`.

### liaise.tick.RUN_LOST *= 'lost'*

…a case found `working` with no run in flight, its run lost…

### liaise.tick.RUN_STARTED *= 'started'*

a start, a start that failed, a collection…

* **Type:**
  What a `run` entry records

### liaise.tick.RUN_STARTED_KEY *= 'run_started_at'*

Where the store keeps a tick’s start and end.

### *exception* liaise.tick.RunLockHeld

Bases: [`RuntimeError`](https://docs.python.org/3/builtins/exceptions.html#RuntimeError)

Another live liaise process holds the run lock, so this tick did not start.

### *class* liaise.tick.RunStamps(started_at=None, ended_at=None, lock_held=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

When the latest tick that was not a dry run started and ended.

#### lock_held *: [bool](https://docs.python.org/3/builtins/functions.html#bool) | [None](https://docs.python.org/3/builtins/constants.html#None)* *= None*

Whether a live process held the run lock when these were read; None when the lock
was not checked.

#### *property* state *: [str](https://docs.python.org/3/builtins/stdtypes.html#str)*

`never`, `finished`, `running` or `interrupted`.

A tick that started later than the last one ended is `running`, unless the lock
was checked and no live process holds it. Then the tick was killed before it could
stamp its end (a `launchctl bootout`, a shutdown): `interrupted`, since a job
that stopped must not read as one that is busy.

### liaise.tick.SEND_REFUSED_CAUSE *= 'refused'*

What a notification names a failed send by when the channel gave no error kind.

### liaise.tick.STATE_READ_FAILURE_LIMIT *= 3*

How many reads of a case’s issue state in a row may fail for good before the case goes
to the owner, who is told once: a deleted or transferred issue never reads again.

### liaise.tick.TICK_ACTOR *= 'liaise'*

The actor of the entries the tick writes.

### *class* liaise.tick.TickReport(plan_lines=(), dispatched=(), collected=(), sent=(), diverted=(), problems=(), dry_run=False, held=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What one [`run_once()`](#liaise.tick.run_once) did or, in a dry run, would do.

`plan_lines` has a line per step, event, case and decision, for `--dry-run` to
print. `dispatched` and `collected` are run ids, `sent` the messages as they went
out (mention added), `diverted` those that stayed with the operator as drafts, and
`held` those the tick put in a case’s outbox (liaise #38).

### liaise.tick.Triage

the
triage seam (#19). The tick starts the cases group by group, each group in its order,
and a case left out is not started this tick. None keeps the tick’s own order.

* **Type:**
  `(the ready cases, in the tick's order) -> groups of them, in the order to start`

alias of `Callable`[[[`Sequence`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Sequence)[[`Case`](liaise.model.md#liaise.model.Case)]], [`Iterable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterable)[[`Iterable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterable)[[`Case`](liaise.model.md#liaise.model.Case)]]]

### liaise.tick.WorkspaceFactory

the
workspace seam (see [`liaise.workspace.workspace_for()`](liaise.workspace.md#liaise.workspace.workspace_for)).

* **Type:**
  `(subject, *, lock_dir, sessions_dir, own_pids) -> SharedCheckout | None`

alias of `Callable`[[…], [`SharedCheckout`](liaise.workspace.md#liaise.workspace.SharedCheckout) | [`None`](https://docs.python.org/3/builtins/constants.html#None)]

### liaise.tick.last_run_age(store, , now=None)

Seconds since the latest tick that was not a dry run started, or None before any.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`float`](https://docs.python.org/3/builtins/functions.html#float)]

### liaise.tick.run_lock(lock_path)

Hold the run lock while the block runs: a tick, or an operator’s change between ticks.

One tick at a time (0.0.x L-3). The lock is an exclusive OS lock on the open lock file
(see `_try_lock()`), taken without waiting. Finding it free and taking it are one
step, and it goes with the process that held it, so no lock is ever stale and none is
taken over. While held, the file holds this process’s pid, for `liaise status` and
for another tick’s error; it is emptied before the lock is released, and the file is
never removed. Raises [`RunLockHeld`](#liaise.tick.RunLockHeld) while another holds the lock, in this
process too.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`None`](https://docs.python.org/3/builtins/constants.html#None)]

### liaise.tick.run_lock_path(state_dir)

Where a tick keeps its run lock.

`liaise status` reads it too, to tell a running tick from one whose process died.

* **Return type:**
  [`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)

### liaise.tick.run_once(subjects, store, \*, global_config, registry=None, processor=None, resolver=<function resolve_person>, workspace=<function workspace_for>, labeler=None, notify_fn=None, sessions_dir=None, now=None, dry_run=False, only=None, outbound_filters=(<function outside_a_case>, <function outbound_policy>, <function writing_card>, <function deslop>, <function notify_recipient>), triage=None, lost_run_deadline=datetime.timedelta(seconds=600), closed_recheck_interval=datetime.timedelta(seconds=3600))

One tick over `subjects` (slug to [`Subject`](liaise.subjects.md#liaise.subjects.Subject)), on the ledger `store`.

See the module docstring for the steps. The seams, each with a working default:

- `registry`: correspond’s channel registry (its own when None);
- `processor`: a [`Processor`](liaise.processor.md#liaise.processor.Processor), by default
  `ClaudeHeadless(runs_dir=<state_dir>/runs)`;
- `resolver`: who a channel address is ([`liaise.access.resolve_person()`](liaise.access.md#liaise.access.resolve_person));
- `workspace`: a subject’s checkout ([`liaise.workspace.workspace_for()`](liaise.workspace.md#liaise.workspace.workspace_for)), with
  its locks in `<state_dir>/locks` and Claude Code’s session records read from
  `sessions_dir` (`~/.claude/sessions` when None);
- `labeler`: the GitHub labels (`GhCli()`);
- `notify_fn`: `(title, body, *, priority)`, by default `liaise.notify.notify()`
  on `global_config.notify.ntfy_topic_env`;
- `triage`: a [`Triage`](#liaise.tick.Triage) that groups and orders each subject’s ready cases
  before they start; None keeps the tick’s own order, oldest first.

`only` is a slug or slugs to run alone; an unknown one raises
[`ConfigError`](liaise.config.md#liaise.config.ConfigError). An inactive subject (`active = false`) is not
ticked: `only` may name one in a dry run alone, and outside one that raises
[`ConfigError`](liaise.config.md#liaise.config.ConfigError) too. `now` is the tick’s clock (the current UTC
time when None). `lost_run_deadline` is how long after the tick cancelled a run for
its wall clock a run that will not stop is waited on ([`LOST_RUN_DEADLINE`](#liaise.tick.LOST_RUN_DEADLINE)), and
`closed_recheck_interval` how long a case whose issue was read closed goes before
it is read again ([`CLOSED_RECHECK_INTERVAL`](#liaise.tick.CLOSED_RECHECK_INTERVAL)). Unless `dry_run`, the tick holds
the run lock in `state_dir` (raising [`RunLockHeld`](#liaise.tick.RunLockHeld) while another tick holds
it) and stamps its start and end in `store`.

* **Return type:**
  [`TickReport`](#liaise.tick.TickReport)

### liaise.tick.run_stamps(store, , lock_path=None)

The run stamps a tick keeps in `store`.

Pass `lock_path` (see [`run_lock_path()`](#liaise.tick.run_lock_path)) to tell a running tick from a killed
one. A store last written by 0.0.3 or earlier holds only `last_run`, the start of a
pass that had already finished, read here as a run that started and ended then.

* **Return type:**
  [`RunStamps`](#liaise.tick.RunStamps)

### liaise.tick.status_lines(subjects, store, , global_config, now=None, recent=5)

What `liaise status` prints: what the ledger in `store` says, read only.

The run stamps (`running`, `interrupted` or `finished`, the lock checked in
`state_dir`), the holds, the runs in flight with their heartbeat age, each subject’s
cases by state and dispatches today, the unrouted queue (its size and the `recent`
latest), the drafts waiting for the operator, the messages held in the outbox, and the
`recent` latest digest notes.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]
