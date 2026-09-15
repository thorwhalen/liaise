# liaise.cli

The `liaise` command line (liaise 0.1).

One SSOT command tree, `_dispatch_funcs`, of plain functions dispatched with `cw`:

```default
liaise run [--once] [--dry-run] [--subject SLUG]
liaise status
liaise hold SCOPE [--mode MODE] [--reason TEXT]
liaise unhold SCOPE
liaise case list [--state STATE]
liaise case show CASE_ID
liaise case set-state CASE_ID STATE [--reason TEXT] [--dry-run]
liaise subject list
liaise subject show SLUG
liaise setup SUBJECT
liaise migrate-config [--apply]
liaise schedule install | uninstall | status
```

Every command takes `--root`, the config root (`~/.config/liaise` by default), and
returns the text it prints. The seams (the channel registry, the processor, the labeler,
the ledger store, the notifier, the sessions directory and the clock) are keyword
arguments with working defaults, hidden from the command line by `_dispatch_config`:
tests fill them with fakes, and the command line never shows them.

An expected failure, such as a configuration that does not load, an unknown subject or a
bad hold scope, is one line on stderr and a nonzero exit (`cw.CommandError`), not a
traceback.

**Breaking change from 0.0.x.** `partner list`, `partner show` and `poll` are gone.
`subject list`, `subject show` and `run --once --dry-run` replace them, and
`migrate-config` derives the subject files from a 0.0.x configuration.

### Module Attributes

| [`DFLT_LOOP_SECONDS`](#liaise.cli.DFLT_LOOP_SECONDS)   | Seconds between two ticks of `liaise run` without `--once`.                           |
|----------------------------------------------------------------------|---------------------------------------------------------------------------------------|
| [`STOPPED`](#liaise.cli.STOPPED)             | What `liaise run` without `--once` returns once it is interrupted.                    |
| [`NONE_SHOWN`](#liaise.cli.NONE_SHOWN)          | How `liaise subject show` prints an empty or unset value.                             |
| [`TICK_RUNNING`](#liaise.cli.TICK_RUNNING)        | What `liaise case set-state` says, changing nothing, while a tick holds the run lock. |

### Functions

| [`case_list`](#liaise.cli.case_list)(\*[, state, root, store])               | Every case in the ledger, a line each: its id, its state and its conversations.              |
|----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| [`case_set_state`](#liaise.cli.case_set_state)(case_id, state, \*[, reason, ...]) | Move CASE_ID to STATE, as you: how a case in needs-owner, or deployed, moves on.             |
| [`case_show`](#liaise.cli.case_show)(case_id, \*[, root, store])             | CASE_ID as the ledger holds it: what a notification from liaise leaves out.                  |
| [`hold`](#liaise.cli.hold)(scope, \*[, mode, reason, root, store])      | Stop work in SCOPE until `liaise unhold`.                                                    |
| [`migrate_config`](#liaise.cli.migrate_config)(\*[, root, apply])                 | Derive 0.1 subject files from a 0.0.x configuration, and print the plan.                     |
| [`run`](#liaise.cli.run)(\*[, root, once, dry_run, subject, ...])      | One tick: take in what arrived, collect finished runs, start ready cases, deploy, label.     |
| [`schedule_install`](#liaise.cli.schedule_install)(\*[, root, ...])                 | Install the scheduled `liaise run --once` job (launchd on macOS, systemd on Linux).          |
| [`schedule_status_cmd`](#liaise.cli.schedule_status_cmd)()                             | Whether the scheduled job is installed.                                                      |
| [`schedule_uninstall`](#liaise.cli.schedule_uninstall)()                              | Remove the scheduled job.                                                                    |
| [`setup`](#liaise.cli.setup)(subject, \*[, root, labeler])               | Create SUBJECT's labels in each GitHub repository it binds.                                  |
| [`status`](#liaise.cli.status)(\*[, root, store, now])                    | What the ledger says, changing nothing: the last run, holds, runs, cases, what waits on you. |
| [`subject_list`](#liaise.cli.subject_list)(\*[, root])                          | Every configured subject with its bindings, flagging any binding that could never match.     |
| [`subject_show`](#liaise.cli.subject_show)(slug, \*[, root])                    | The subject SLUG as liaise reads it, every default applied, and its binding problems.        |
| [`unhold`](#liaise.cli.unhold)(scope, \*[, root, store])                  | Lift the hold on SCOPE, whoever set it.                                                      |

### liaise.cli.DFLT_LOOP_SECONDS *= 60*

Seconds between two ticks of `liaise run` without `--once`. The scheduled job
passes `--once` and leaves the interval to the scheduler.

### liaise.cli.NONE_SHOWN *= '(none)'*

How `liaise subject show` prints an empty or unset value.

### liaise.cli.STOPPED *= 'stopped'*

What `liaise run` without `--once` returns once it is interrupted.

### liaise.cli.TICK_RUNNING *= 'a liaise tick is running, so {case_id} was not moved; try again shortly ({busy})'*

What `liaise case set-state` says, changing nothing, while a tick holds the run lock.

### liaise.cli.case_list(, state=None, root=None, store=None)

Every case in the ledger, a line each: its id, its state and its conversations.

`--state` lists only the cases in that state, such as `needs-owner`, what waits
on you. It changes nothing.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cli.case_set_state(case_id, state, , reason='', dry_run=False, root=None, store=None, now=None)

Move CASE_ID to STATE, as you: how a case in needs-owner, or deployed, moves on.

STATE is a case state other than `working`, which only a run makes true. `intake`
has the tick start the case again once it is ready, resuming its session. The move is
recorded on the case, with `--reason`. The case’s GitHub label follows on the next
tick: a label is a projection of the ledger, so relabelling the issue by hand is
overwritten. `--dry-run` says what would change, and changes nothing.

The move holds the run lock, so a tick cannot start meanwhile and overwrite it. It does
not wait for one: while a tick is running, it refuses in one line and changes nothing.
A dry run takes no lock.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cli.case_show(case_id, , root=None, store=None)

CASE_ID as the ledger holds it: what a notification from liaise leaves out.

Its state, the reason of its last escalation, its last failed deploy with the command’s
output, each draft waiting for you with its text, and its latest entries. A
notification names the case and points here: nothing a case holds goes to the
notification service. It changes nothing.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cli.hold(scope, , mode='block', reason='', root=None, store=None)

Stop work in SCOPE until `liaise unhold`.

SCOPE is `global`, `processor`, `effect:<kind>`, `subject:<slug>`,
`person:<id>`, `repo:<owner/repo>` or `checkout:<path>`. `--mode block` (the
default) starts nothing new and keeps a finished run’s messages as drafts; `drain`
starts nothing new and lets work already running finish and send; `cancel` also
stops running runs, which stay resumable.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cli.migrate_config(, root=None, apply=False)

Derive 0.1 subject files from a 0.0.x configuration, and print the plan.

Writes nothing without `--apply`, which creates each missing
`subjects/<slug>.toml` and never overwrites one. Nothing else under the config root
is touched.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cli.run(, root=None, once=False, dry_run=False, subject=None, registry=None, processor=None, labeler=None, store=None, notify_fn=None, sessions_dir=None, now=None, resolver=None, workspace=None, triage=None)

One tick: take in what arrived, collect finished runs, start ready cases, deploy, label.

Prints the tick’s plan, a line per event, case, run and decision. `--dry-run` prints
the same plan and changes nothing: nothing is sent, labelled, started, cancelled,
deployed, locked or written. `--subject` ticks one subject alone. Without `--once`
or `--dry-run`, it ticks every minute, printing each plan, until interrupted; the
scheduled job (`liaise schedule install`) passes `--once`.

`resolver`, `workspace` and `triage` are the tick’s seams of those names (see
[`liaise.tick.run_once()`](liaise.tick.html.md#liaise.tick.run_once)); None keeps the tick’s own default.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cli.schedule_install(, root=None, interval_minutes=2, extra_env_vars=None)

Install the scheduled `liaise run --once` job (launchd on macOS, systemd on Linux).

`extra_env_vars`: names of additional environment variables (beyond `PATH`, `HOME`,
and the configured ntfy topic variable) the job needs snapshotted into its
environment, such as one a deploy command reads.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cli.schedule_status_cmd()

Whether the scheduled job is installed.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cli.schedule_uninstall()

Remove the scheduled job. Idempotent.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cli.setup(subject, , root=None, labeler=None)

Create SUBJECT’s labels in each GitHub repository it binds. Idempotent.

Those are its claim labels and one `<label_prefix><state>` label per case state.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cli.status(, root=None, store=None, now=None)

What the ledger says, changing nothing: the last run, holds, runs, cases, what waits on you.

That is the run stamps (`running`, `interrupted` or `finished`), the holds, the
runs in flight, each subject’s cases by state and dispatches today, the unrouted
queue, the drafts waiting for the operator, and the latest digest notes.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cli.subject_list(, root=None)

Every configured subject with its bindings, flagging any binding that could never match.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cli.subject_show(slug, , root=None)

The subject SLUG as liaise reads it, every default applied, and its binding problems.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cli.unhold(scope, , root=None, store=None)

Lift the hold on SCOPE, whoever set it.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)
