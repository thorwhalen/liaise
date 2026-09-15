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
liaise case send-draft CASE_ID [INDEX] [--edit] [--dry-run]
liaise case reject-draft CASE_ID [INDEX] --reason TEXT [--dry-run]
liaise subject list
liaise subject show SLUG
liaise setup SUBJECT
liaise migrate-config [--apply]
liaise schedule install | uninstall | status
```

Every command takes `--root`, the config root (`~/.config/liaise` by default), and
returns the text it prints. The seams (the channel registry, the processor, the labeler,
the ledger store, the notifier, the sessions directory, the clock and the editor) are keyword
arguments with working defaults, hidden from the command line by `_dispatch_config`:
tests fill them with fakes, and the command line never shows them.

An expected failure, such as a configuration that does not load, an unknown subject or a
bad hold scope, is one line on stderr and a nonzero exit (`cw.CommandError`), not a
traceback.

**Breaking change from 0.0.x.** `partner list`, `partner show` and `poll` are gone.
`subject list`, `subject show` and `run --once --dry-run` replace them, and
`migrate-config` derives the subject files from a 0.0.x configuration.

### Module Attributes

| [`DFLT_LOOP_SECONDS`](#liaise.cli.DFLT_LOOP_SECONDS)   | Seconds between two ticks of `liaise run` without `--once`.                                    |
|----------------------------------------------------------------------|------------------------------------------------------------------------------------------------|
| [`STOPPED`](#liaise.cli.STOPPED)             | What `liaise run` without `--once` returns once it is interrupted.                             |
| [`NONE_SHOWN`](#liaise.cli.NONE_SHOWN)          | How `liaise subject show` prints an empty or unset value.                                      |
| [`TICK_RUNNING`](#liaise.cli.TICK_RUNNING)        | What a case command that writes says, changing nothing, while a tick holds the run lock.       |
| [`EDITOR_ENV_VARS`](#liaise.cli.EDITOR_ENV_VARS)     | The environment variables that name the operator's editor, the first one set winning.          |
| [`DFLT_EDITOR`](#liaise.cli.DFLT_EDITOR)         | The editor `liaise case send-draft --edit` opens when no variable names one.                   |
| [`DRAFT_FILE_NAME`](#liaise.cli.DRAFT_FILE_NAME)     | The file `--edit` puts the draft in, inside a temporary directory of its own.                  |
| [`CONFIRM_PROMPT`](#liaise.cli.CONFIRM_PROMPT)      | What `liaise case send-draft` asks at the terminal, and the answers that send.                 |
| [`DIVERTED_EXIT_CODE`](#liaise.cli.DIVERTED_EXIT_CODE)  | the message is held for the operator, as `liaise vet` is planned to say (discussion 32, §5.8). |
| [`NO_TERMINAL`](#liaise.cli.NO_TERMINAL)         | Why `liaise case send-draft` sends nothing without a terminal to ask at.                       |

### Functions

| [`case_list`](#liaise.cli.case_list)(\*[, state, root, store])                | Every case in the ledger, a line each: its id, its state and its conversations.              |
|-----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| [`case_reject_draft`](#liaise.cli.case_reject_draft)(case_id, \*index[, reason, ...]) | Decline CASE_ID's draft INDEX, or its only one, recording `--reason`.                        |
| [`case_send_draft`](#liaise.cli.case_send_draft)(case_id, \*index[, edit, ...])     | Send a draft you approved: CASE_ID's draft INDEX, or its only one, through the gate.         |
| [`case_set_state`](#liaise.cli.case_set_state)(case_id, state, \*[, reason, ...])  | Move CASE_ID to STATE, as you: how a case in needs-owner, or deployed, moves on.             |
| [`case_show`](#liaise.cli.case_show)(case_id, \*[, root, store])              | CASE_ID as the ledger holds it: what a notification from liaise leaves out.                  |
| [`confirm_at_terminal`](#liaise.cli.confirm_at_terminal)(preview)                       | Show `preview` and ask, at the operator's terminal, whether to send it; True for yes.        |
| [`edit_in_editor`](#liaise.cli.edit_in_editor)(text)                               | `text` as the operator leaves it in their editor: `$VISUAL`, `$EDITOR`, else vi.             |
| [`hold`](#liaise.cli.hold)(scope, \*[, mode, reason, root, store])       | Stop work in SCOPE until `liaise unhold`.                                                    |
| [`migrate_config`](#liaise.cli.migrate_config)(\*[, root, apply])                  | Derive 0.1 subject files from a 0.0.x configuration, and print the plan.                     |
| [`run`](#liaise.cli.run)(\*[, root, once, dry_run, subject, ...])       | One tick: take in what arrived, collect finished runs, start ready cases, deploy, label.     |
| [`schedule_install`](#liaise.cli.schedule_install)(\*[, root, ...])                  | Install the scheduled `liaise run --once` job (launchd on macOS, systemd on Linux).          |
| [`schedule_status_cmd`](#liaise.cli.schedule_status_cmd)()                              | Whether the scheduled job is installed.                                                      |
| [`schedule_uninstall`](#liaise.cli.schedule_uninstall)()                               | Remove the scheduled job.                                                                    |
| [`setup`](#liaise.cli.setup)(subject, \*[, root, labeler])                | Create SUBJECT's labels in each GitHub repository it binds.                                  |
| [`status`](#liaise.cli.status)(\*[, root, store, now])                     | What the ledger says, changing nothing: the last run, holds, runs, cases, what waits on you. |
| [`subject_list`](#liaise.cli.subject_list)(\*[, root])                           | Every configured subject with its bindings, flagging any binding that could never match.     |
| [`subject_show`](#liaise.cli.subject_show)(slug, \*[, root])                     | The subject SLUG as liaise reads it, every default applied, and its binding problems.        |
| [`unhold`](#liaise.cli.unhold)(scope, \*[, root, store])                   | Lift the hold on SCOPE, whoever set it.                                                      |

### liaise.cli.CONFIRM_PROMPT *= 'send it? [y/N] '*

What `liaise case send-draft` asks at the terminal, and the answers that send.

### liaise.cli.DFLT_EDITOR *= 'vi'*

The editor `liaise case send-draft --edit` opens when no variable names one.

### liaise.cli.DFLT_LOOP_SECONDS *= 60*

Seconds between two ticks of `liaise run` without `--once`. The scheduled job
passes `--once` and leaves the interval to the scheduler.

### liaise.cli.DIVERTED_EXIT_CODE *= 2*

the message is held
for the operator, as `liaise vet` is planned to say (discussion 32, §5.8).

* **Type:**
  The exit code of a draft command whose message the gate diverted

### liaise.cli.DRAFT_FILE_NAME *= 'draft.md'*

The file `--edit` puts the draft in, inside a temporary directory of its own.

### liaise.cli.EDITOR_ENV_VARS *= ('VISUAL', 'EDITOR')*

The environment variables that name the operator’s editor, the first one set winning.

### liaise.cli.NONE_SHOWN *= '(none)'*

How `liaise subject show` prints an empty or unset value.

### liaise.cli.NO_TERMINAL *= 'liaise case send-draft sends a draft only once you confirm it at a terminal, and there is no terminal here, so nothing was sent: run it in your own shell (--dry-run asks nothing)'*

Why `liaise case send-draft` sends nothing without a terminal to ask at.

### liaise.cli.STOPPED *= 'stopped'*

What `liaise run` without `--once` returns once it is interrupted.

### liaise.cli.TICK_RUNNING *= 'a liaise tick is running, so {case_id} was not {done}; try again shortly ({busy})'*

What a case command that writes says, changing nothing, while a tick holds the run lock.

### liaise.cli.case_list(, state=None, root=None, store=None)

Every case in the ledger, a line each: its id, its state and its conversations.

`--state` lists only the cases in that state, such as `needs-owner`, what waits
on you. It changes nothing.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cli.case_reject_draft(case_id, \*index, reason='', dry_run=False, root=None, store=None, now=None)

Decline CASE_ID’s draft INDEX, or its only one, recording `--reason`.

Nothing is sent. The draft leaves the case, and your refusal is recorded on the case with
its reason and the draft’s text. The case’s state stays as it is: move it on with
`liaise case set-state`. It holds the run lock, as `set-state` does. `--dry-run`
changes nothing.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.cli.case_send_draft(case_id, \*index, edit=False, dry_run=False, root=None, registry=None, store=None, now=None, editor=None, confirm=None)

Send a draft you approved: CASE_ID’s draft INDEX, or its only one, through the gate.

`liaise case show` numbers the drafts. The gate judges the text again, with your
approval recorded: draft reply mode lets it through, and the leak scan, deslop and the
mention judge it as they judge any message. `--edit` opens the text in `$VISUAL` or
`$EDITOR` first, and the gate judges what you saved.

It then shows you where the message goes, the gate’s verdict and the message exactly as
it would be sent, and sends it only once you answer `y` at a terminal. Without a
terminal, as in an agent’s shell or a processor run, it sends nothing.

Once sent, the draft leaves the case and the send is recorded as yours. When no draft
is left, a case in needs-owner moves on as a sent message moves it: an ask, a reply or
a proposal, to needs-partner. A message the gate diverts is not sent: the draft stays
on the case with the reason, and the command exits 2. A message its channel refuses
stays the same way, and exits 1. `--dry-run` judges and plans, asks nothing, and
records nothing.

It holds the run lock, as `set-state` does, and refuses while a tick runs. It also
refuses while a hold keeps the case’s messages waiting, while a run of the case is in
flight, and for a delivery message whose delivery a hold kept from running.

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

### liaise.cli.confirm_at_terminal(preview)

Show `preview` and ask, at the operator’s terminal, whether to send it; True for yes.

A draft is released by a person at a terminal, not by whatever can run a command, so
this raises `ValueError` when standard input is not a terminal: a processor run, or
an agent’s shell. It is a check on the ordinary way of running the command, not a
sandbox: the hook of discussion 32, §5.8, is the defence for commands an agent writes.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### liaise.cli.edit_in_editor(text)

`text` as the operator leaves it in their editor: `$VISUAL`, `$EDITOR`, else vi.

The text goes in a file in a temporary directory only its owner can read, and the
directory is removed once the editor exits, with any backup the editor left there. An
editor that returns before the operator has saved, such as a GUI editor started
without its wait flag (`code --wait`), hands the text back unchanged, and the
confirmation says so. On Windows the command runs through the shell, which a `.cmd`
editor needs. Raises `ValueError` when the editor cannot be started or exits
nonzero, so nothing is sent.

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
