# liaise

liaise: the loop between a non-technical partner and a coding agent, over conversations.

A subject (`subjects/<slug>.toml`, [`load_subjects()`](#liaise.load_subjects)) binds conversations, such as
GitHub issues or a web inbox, and says who may ask for what. Each tick ([`run_once()`](#liaise.run_once))
takes in what arrived as cases in the [`Ledger`](#liaise.Ledger), starts ready cases as detached
processor runs, and carries out their outcomes through the outbound gate
([`run_gate()`](#liaise.run_gate)). `liaise run --once --dry-run` prints one tick’s plan and changes
nothing.

### Functions

| [`authorize`](#liaise.authorize)(message, subject, permission, \*[, ...])   | Whether `message` may exercise `permission` on `subject`, by the label-as-claim rule.                                                       |
|-------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------|
| [`default_ledger_store`](#liaise.default_ledger_store)(state_dir)                      | The default ledger store: one JSON file per key in `<state_dir>/ledger`.                                                                    |
| [`evaluate`](#liaise.evaluate)(outbound, \*, audience, disclosure, ...)    | The [`Verdict`](#liaise.Verdict) for `outbound`, through every rule of the table.                               |
| [`hold`](#liaise.hold)(ledger, scope, \*[, mode, reason, ...])         | Put a `mode` hold on `scope`, replacing any hold already there, and return it.                                                              |
| [`intake`](#liaise.intake)(subject, ledger, \*[, registry, ...])         | Take in what arrived on `subject`'s bindings since the ledger's cursors.                                                                    |
| [`load_global_config`](#liaise.load_global_config)([root])                           | Load `<root>/config.toml` into a [`GlobalConfig`](#liaise.GlobalConfig), defaults applied.                           |
| [`load_subjects`](#liaise.load_subjects)([root])                                | Every subject under `<root>/subjects/*.toml`, keyed by slug, in slug order.                                                                 |
| [`migrate_config`](#liaise.migrate_config)([root, apply, registry])              | Plan the 0.1 subject files for the 0.0.x config under `root`; write them if `apply`.                                                        |
| [`notify`](#liaise.notify)(title, body, \*[, priority, ...])             | POST `body` to the ntfy topic named by the `topic_env` environment variable.                                                                |
| [`outbound_policy`](#liaise.outbound_policy)(outbound, ctx, \*[, ...])            | Hold back what the outbound policy (discussion §5.4) does not let go now.                                                                   |
| [`parse_outcomes`](#liaise.parse_outcomes)(structured_output)                    | The outcomes of a run's structured output, checked against `OUTCOME_SCHEMA`.                                                                |
| [`plan_outcomes`](#liaise.plan_outcomes)(case, outcomes, subject, \*, now)      | The actions that carry out `outcomes` on `case`, one group per outcome, in order.                                                           |
| [`resolve_person`](#liaise.resolve_person)(address, subject)                     | The person id `address` belongs to on `subject`, or None.                                                                                   |
| [`run_gate`](#liaise.run_gate)(outbound, ctx, \*[, outbound_filters])      | Run `outbound` through every one of `outbound_filters`, in order, and decide.                                                               |
| [`run_once`](#liaise.run_once)(subjects, store, \*, global_config)         | One tick over `subjects` (slug to [`Subject`](liaise.subjects.md#liaise.subjects.Subject)), on the ledger `store`. |
| [`status_lines`](#liaise.status_lines)(subjects, store, \*, global_config)     | What `liaise status` prints: what the ledger in `store` says, read only.                                                                    |
| [`unhold`](#liaise.unhold)(ledger, scope)                                | Lift the hold on `scope`, whoever set it; True when there was one.                                                                          |

### Classes

| [`Approval`](#liaise.Approval)(by, at[, payload_hash, ...])             | The operator's release of a held message, bound to the message and audience they saw.                                         |
|----------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------|
| [`Case`](#liaise.Case)(id, subject, conversations, reporter, ...)   | One piece of work on a subject, from its first message to its delivery.                                                       |
| [`ClaudeHeadless`](#liaise.ClaudeHeadless)(\*[, claude_bin, runs_dir, ...])   | The default [`Processor`](#liaise.Processor): the `claude` CLI, headless, stream-JSON, detached.    |
| [`EchoProcessor`](#liaise.EchoProcessor)(\*[, results, default, health])     | A [`Processor`](#liaise.Processor) that runs nothing: it records jobs and returns scripted results. |
| [`FakeGitHub`](#liaise.FakeGitHub)([issues])                              | In-memory [`GitHub`](#liaise.GitHub), for tests.                                                 |
| [`GateContext`](#liaise.GateContext)(\*, subject, now[, case, ...])        | What the filters may consult about one message.                                                                               |
| [`GateDecision`](#liaise.GateDecision)(send, diverted[, notes, ...])        | What [`run_gate()`](#liaise.run_gate) decided: `send` a message, or why it is `diverted`.          |
| [`GhCli`](#liaise.GhCli)(\*[, gh_bin])                               | The default [`GitHub`](#liaise.GitHub): every call shells out to the `gh` CLI.                   |
| [`GitHub`](#liaise.GitHub)(\*args, \*\*kwargs)                        | What `liaise` needs from GitHub.                                                                                              |
| [`GlobalConfig`](#liaise.GlobalConfig)(owner_login, state_dir[, ...])       | `~/.config/liaise/config.toml`: the owner, the state directory, and notifications.                                            |
| [`Health`](#liaise.Health)(ok[, defer_until, error])                  | Whether a processor can take work now, and if not, until when or why.                                                         |
| [`Hold`](#liaise.Hold)(scope, mode[, reason, set_by, set_at])       | A stop on work in `scope` (`global`, `subject:<slug>`, `repo:<o/r>`, ...).                                                    |
| [`IntakeReport`](#liaise.IntakeReport)(subject[, events, new_cases, ...])   | What one [`intake()`](#liaise.intake) of a subject took in.                                      |
| [`Job`](#liaise.Job)(run_id, case_id, subject, prompt, cwd, ...)   | Everything a [`Processor`](#liaise.Processor) needs to run one case once.                           |
| [`Ledger`](#liaise.Ledger)(store)                                     | What liaise has seen, its cases, runs and holds, the unrouted queue and the cursors.                                          |
| [`LedgerEntry`](#liaise.LedgerEntry)(at, kind[, actor, grade, ...])        | One thing that happened on a case: appended, never changed.                                                                   |
| [`Outbound`](#liaise.Outbound)(\*, ref, channel, recipient, ...[, ...]) | A message liaise would send: `text` for `recipient` (a person id) at `ref`.                                                   |
| [`Outcome`](#liaise.Outcome)(kind[, text, questions, reason])          | One outcome a processor run reports: `kind` from `OUTCOME_KINDS`.                                                             |
| [`Processor`](#liaise.Processor)(\*args, \*\*kwargs)                     | What the tick needs to run a case's work: the processor seam (`processor=`).                                                  |
| [`Provenance`](#liaise.Provenance)([tainted, evidence])                   | What the run that wrote the message read: tainted, clean, or unknown.                                                         |
| [`RunRecord`](#liaise.RunRecord)(run_id, case_id, subject, mode, ...)    | A processor run started on a case: how it was started, and where it is now.                                                   |
| [`RunResult`](#liaise.RunResult)(run_id[, outcomes, usage, ...])         | What a finished run returned: its outcomes, what it cost, and how it ended.                                                   |
| [`Subject`](#liaise.Subject)(slug, bindings, policy[, ...])            | A resolved subject: `subjects/<slug>.toml` with every default applied.                                                        |
| [`TickReport`](#liaise.TickReport)([plan_lines, dispatched, ...])         | What one [`run_once()`](#liaise.run_once) did or, in a dry run, would do.                          |
| [`Verdict`](#liaise.Verdict)(\*, flow, route, reasons, axes, ...)      | What the policy decided about one message, and why (discussion §5.1).                                                         |

### Exceptions

| [`ConfigError`](#liaise.ConfigError)   | Raised when configuration is missing or malformed.          |
|----------------------------------------------------------------|-------------------------------------------------------------|
| [`GitHubError`](#liaise.GitHubError)   | Raised when the `gh` CLI fails — its stderr is the message. |

### *class* liaise.Approval(by, at, payload_hash=None, audience_hash=None, verdict_id=None, justification='', rules_overridden=())

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

### *class* liaise.Case(id, subject, conversations, reporter, state, created_at, updated_at, session_id=None, entries=(), drafts=(), outbox=(), defer_until=None)

Bases: `_Record`

One piece of work on a subject, from its first message to its delivery.

`id` is `<subject>-<n>`. `conversations` are the encoded refs
(`github:example/app#12`) whose messages belong to it; `reporter` is the
person who opened it; `state` is one of `CASE_STATES`. `entries` is the
append-only history and `drafts` the outbound messages diverted to the operator.
`outbox` holds the messages the gate gave `delay`: each waits, cancellable, until
its `release_at`, when the tick judges it again and sends it (liaise #38; see
[`liaise.outbox`](liaise.outbox.md#module-liaise.outbox)). `defer_until`, when set, is the earliest time the case may be dispatched again
(after a quota reset, a rate limit, or a busy workspace).

#### with_entry(entry)

This case with `entry` appended, and `updated_at` moved forward to it.

* **Return type:**
  [`Case`](liaise.model.md#liaise.model.Case)

#### with_state(state, , at, actor=None, reason='')

This case in `state`, with a `transition` entry recording the change.

Raises `ValueError` for a state outside `CASE_STATES`.

* **Return type:**
  [`Case`](liaise.model.md#liaise.model.Case)

### *class* liaise.ClaudeHeadless(, claude_bin='claude', runs_dir=None, grace_s=15.0, kill_grace_s=60.0, auth_check=('auth', 'status'), auth_timeout_s=15.0)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

The default [`Processor`](#liaise.Processor): the `claude` CLI, headless, stream-JSON, detached.

`runs_dir` is where each run’s directory goes; the tick passes
`<state_dir>/runs`, and nothing but [`preflight()`](#liaise.ClaudeHeadless.preflight) works without it.
`claude_bin` is the command, found on `PATH` or given as a path. `grace_s` is
how long a graceful cancel waits after its interrupt before it may terminate, and
`kill_grace_s` how long a run that was sent SIGTERM may go on before a cancel kills
it (POSIX). `auth_check` is the arguments that ask `claude` whether its login
still works (`DFLT_AUTH_CHECK`), which [`preflight()`](#liaise.ClaudeHeadless.preflight) gives
`auth_timeout_s` seconds to answer; None skips that check, for a `claude` without
the command.

A run is spawned in its own process group (a new session on POSIX), so it outlives
the tick that started it and a cancel reaches everything it started. Its environment
is `child_env()`.

#### cancel(run, , mode='graceful', now=None)

Ask `run` to stop, and return its refreshed record (usually still running).

On POSIX, `graceful` sends SIGINT to the run’s process group the first time, and
SIGTERM on a later call once `grace_s` has passed since that first request;
`now` sends SIGTERM. A run still alive `kill_grace_s` after its SIGTERM is sent
SIGKILL by the next call, whatever the mode. On Windows every call terminates the
run and its children outright, so there is nothing to escalate to. When the cancel
was first requested, the last signal, and when that signal was first sent are kept
in `record.json`, so the next tick’s call continues the same cancel. `now` is
the cancel’s clock (the current UTC time when None). A finished run is returned as
it is. Only a process verified as the run’s (see [`status()`](#liaise.ClaudeHeadless.status)) is signalled: a
live pid whose start cannot be read may be another process’s, so it is sent nothing,
and only the request is recorded.

* **Return type:**
  [`RunRecord`](liaise.model.md#liaise.model.RunRecord)

#### collect(run, , timed_out=False, persist=True)

`run`’s result, or None while it is running. Never raises on a bad stream.

The outcomes are the last `result` event’s `structured_output.outcomes`; if
any of them is malformed the run has no outcomes. The error class comes from
[`liaise.errors.classify()`](liaise.errors.md#liaise.errors.classify), given `stderr.log` and `timed_out` (which the
tick sets for a run it cancelled for passing its wall clock). `persist` is as
for [`status()`](#liaise.ClaudeHeadless.status). A run whose id names another case’s record is `config_error`,
and that record’s files are not read.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`RunResult`](liaise.model.md#liaise.model.RunResult)]

#### preflight(job)

Whether `job` could start now. It starts no run.

`config_error` without `runs_dir`, a runnable `claude_bin`, or `job.cwd`.
Then `auth_expired` when `claude auth status` (`auth_check`) exits non-zero,
so a login that has expired costs no run. A check that cannot be run, or does not
answer within `auth_timeout_s`, tells nothing: preflight passes, and the run’s
own classification has the last word.

* **Return type:**
  [`Health`](liaise.model.md#liaise.model.Health)

#### resume(session_id, job)

Like [`start()`](#liaise.ClaudeHeadless.start), but continuing `session_id` (`--resume`).

* **Return type:**
  [`RunRecord`](liaise.model.md#liaise.model.RunRecord)

#### run_dir(run_id)

The directory holding `run_id`’s files. Raises `ValueError` without
`runs_dir`, or for a `run_id` that is not one path segment.

* **Return type:**
  [`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)

#### start(job)

Spawn `job` in a new session (`--session-id`) and return its record at once.

Idempotent: when `job.run_id` already has a record of `job.case_id`, that record
is returned and nothing is spawned. A record another case’s run wrote under that id
is neither returned nor touched: the run returned is finished at once, spawned
nothing, and [`collect()`](#liaise.ClaudeHeadless.collect) classifies it as `config_error`. Never raises for a
command that cannot be spawned: the run is recorded as finished with the reason in
its `stderr.log`, which [`collect()`](#liaise.ClaudeHeadless.collect) classifies as `config_error`.

* **Return type:**
  [`RunRecord`](liaise.model.md#liaise.model.RunRecord)

#### status(run, , persist=True)

`run` refreshed from its files and process, and saved to its `record.json`.

`heartbeat_at` is the stream’s mtime. The run is finished once its stream holds
a `result` event or its process is gone; `ended_at` is then the stream’s mtime.
A run another process started (an earlier tick) is known by its pid, which counts
as its process only while that process started when the run was spawned (see
`_process_is_the_run()`): a pid a later process was given, after a reboot say,
is gone, and a live pid whose start cannot be read is taken to be the run. It reads
files and, for such a pid, asks `ps` (bounded by a timeout), so it never waits on
the run. With `persist=False`, as in a dry run, `record.json` is left as it was.
A run whose id names another case’s record is finished, and nothing is written.

* **Return type:**
  [`RunRecord`](liaise.model.md#liaise.model.RunRecord)

### *exception* liaise.ConfigError

Bases: [`Exception`](https://docs.python.org/3/builtins/exceptions.html#Exception)

Raised when configuration is missing or malformed.

Always names the path involved and, for a missing file, the minimal content
that would fix it — an agent (or a human) reading the error should not have
to go spelunking in the design docs to recover.

### *class* liaise.EchoProcessor(, results=None, default=None, health=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A [`Processor`](#liaise.Processor) that runs nothing: it records jobs and returns scripted results.

`results` maps a case id to the [`RunResult`](liaise.model.md#liaise.model.RunResult) its runs return,
and `default` is returned for any other case; without either, a run reports one
`note`. `health` is what `preflight()` returns (ok by default). A run is
finished as soon as it starts. It writes nothing, so `persist` changes nothing. `jobs`, `preflights` and `cancels` record the
calls, for tests to assert on.

### *class* liaise.FakeGitHub(issues=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

In-memory [`GitHub`](#liaise.GitHub), for tests. No network, no real repo, no token.

Every other test in this package should use this rather than [`GhCli`](#liaise.GhCli).

#### SELF_AUTHOR *= 'liaise-bot'*

The fixed identity every comment posted through this fake carries —
stands in for “whatever GitHub identity `gh` is authenticated as” in
tests, so `ensure_last_comment_mentions()` has something to match.

#### labels_created(repo)

Test helper: labels created (via `create_label()`) for `repo`.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

#### seed(issue)

Add or replace an issue, for test setup.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### *class* liaise.GateContext(, subject, now, case=None, approval=None, audience=None, provenance=None, mode=None, fingerprint_key=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What the filters may consult about one message.

`subject` is the subject whose policy applies, `now` the time of the decision, and
`case` the case the message belongs to (None outside any). `audience` is
correspond’s record of who can read the destination, computed right before the gate
runs (None: unknown, so public). `provenance` is what the run that wrote the message
read (None: unknown, so tainted). `mode` overrides the subject’s `policy.mode`.
`approval` is the operator’s release of this message, None for every message sent
without one. `fingerprint_key` is the key findings are fingerprinted with (None: the
one in the configured state directory).

### *class* liaise.GateDecision(send, diverted, notes=(), diverted_by=None, flow='send', concerns=(), settled=(), verdict=None, consulted=<factory>, approval=None, payload_hash=None, audience_hash=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What [`run_gate()`](#liaise.run_gate) decided: `send` a message, or why it is `diverted`.

Exactly one of `send` (the message as the filters left it) and `diverted` (every
standing concern’s text, most restrictive first) is set. `flow` is the decision’s,
`concerns` what still holds the message back and `settled` what the approval
released it past. `notes` holds every filter’s notes, in order. `diverted_by` names
the filter of the most restrictive concern, as an operator notification may say it: the
reason can quote what a filter raised. `verdict` and `consulted` are the policy’s
verdict and what it consulted. `approval` is the one on the context, and
`payload_hash` and `audience_hash` what it had to match.

#### *property* audience_words *: [str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None)*

The audience the policy judged, in words; None when the policy did not run.

#### *property* bound *: [bool](https://docs.python.org/3/builtins/functions.html#bool)*

Whether the approval binds to this message, this audience and this verdict.

#### *property* overridable *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), ...]*

The rules of the standing concerns an approval could settle, each once.

#### record()

What a ledger `gate` entry records of the decision (discussion §5.7).

The flow and every concern with its findings (kinds, positions and fingerprints,
never the value), what the approval settled, the policy’s verdict (its audience
snapshot, the readers’ tiers and clearances, the mode), the labels, seals and
provenance consulted, and the approval with whether it bound.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

#### summary()

What a held draft keeps of the decision: the flow, the audience in words, the reasons.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### *class* liaise.GhCli(, gh_bin='gh')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

The default [`GitHub`](#liaise.GitHub): every call shells out to the `gh` CLI.

Auth belongs to whatever machine `gh` is configured on. This class never
reads, stores or passes a token.

### *class* liaise.GitHub(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

What `liaise` needs from GitHub. Implemented by [`GhCli`](#liaise.GhCli) and [`FakeGitHub`](#liaise.FakeGitHub).

#### add_labels(repo, number, labels)

Add one or more labels to an issue. No-op for a label already present.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### create_label(repo, name, , color='ededed', description='')

Create a label if it does not already exist. Idempotent.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### ensure_last_comment_mentions(repo, number, mention)

Repair this identity’s own last comment on `number` to carry `mention`.

If the last comment posted by liaise’s own GitHub identity on this
issue does not already contain `mention`, prepends it (body content
otherwise untouched) and returns True. Returns False when the last
such comment already contains `mention`, or when this identity has
posted no comment on the issue at all. A rule the dispatched agent
forgets must still hold (#20).

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

#### get_issue(repo, number)

Read one issue, with its comments.

* **Return type:**
  [`Issue`](liaise.github.md#liaise.github.Issue)

#### list_issues(repo, , label=None, author=None, state='open')

List issues in `repo`, optionally filtered by label and/or author.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Issue`](liaise.github.md#liaise.github.Issue)]

#### post_comment(repo, number, body)

Post a comment on an issue.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### remove_labels(repo, number, labels)

Remove one or more labels from an issue. No-op for a label already absent.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### *exception* liaise.GitHubError

Bases: [`Exception`](https://docs.python.org/3/builtins/exceptions.html#Exception)

Raised when the `gh` CLI fails — its stderr is the message.

### *class* liaise.GlobalConfig(owner_login, state_dir, notify=<factory>, quiet_minutes=10, go_minutes=2, markers=<factory>, label_prefix='liaise:', budget=<factory>, deploy_per='batch', deployed_nudge_days=3)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

`~/.config/liaise/config.toml`: the owner, the state directory, and notifications.

The other fields are the 0.0.x defaults a partner file inherits, which only
[`liaise.migrate`](liaise.migrate.md#module-liaise.migrate) still reads. A 0.1 subject file has defaults of its own.

### *class* liaise.Health(ok, defer_until=None, error=None)

Bases: `_Record`

Whether a processor can take work now, and if not, until when or why.

### *class* liaise.Hold(scope, mode, reason='', set_by=None, set_at=None)

Bases: `_Record`

A stop on work in `scope` (`global`, `subject:<slug>`, `repo:<o/r>`, …).

### *class* liaise.IntakeReport(subject, events=(), new_cases=(), updated_cases=(), unrouted=(), problems=(), dry_run=False)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What one [`intake()`](#liaise.intake) of a subject took in.

`new_cases` and `updated_cases` are the cases as intake left them (a case opened
this time is only in `new_cases`). `unrouted` holds the fields each unrouted
message was queued with; `problems` what stopped a binding or a label from being
read as it should.

#### plan_lines()

One line per event taken in, per case, and per problem, under a summary line.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### *class* liaise.Job(run_id, case_id, subject, prompt, cwd, permission_mode, timeout_minutes, json_schema, session_id=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Everything a [`Processor`](#liaise.Processor) needs to run one case once.

`run_id` names the run, and its directory, so it is one path segment. `prompt`
is the whole prompt (see [`liaise.prompt.compose_case_prompt()`](liaise.prompt.md#liaise.prompt.compose_case_prompt)), `cwd` is where
the work happens, and `json_schema` is the structured result the run must end
with. `timeout_minutes` is the wall clock the tick enforces; a processor does not.
`session_id` is the case’s stored session, if it has one: the tick hands it to
[`Processor.resume()`](#liaise.Processor.resume), while `start` always opens a new session.

### *class* liaise.Ledger(store)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What liaise has seen, its cases, runs and holds, the unrouted queue and the cursors.

`store` is any `MutableMapping` of JSON-ready values: a `dict` in tests,
[`default_ledger_store()`](#liaise.default_ledger_store) for real, `ChainMap({}, store)` for a dry run. The
ledger keeps no state of its own, so two ledgers over one store agree.

#### add_conversation(case_id, encoded_ref)

Attach `encoded_ref` to the case, indexing it. Idempotent.

* **Return type:**
  [`Case`](liaise.model.md#liaise.model.Case)

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
  [`Case`](liaise.model.md#liaise.model.Case)

#### case_for_conversation(encoded_ref)

The case `encoded_ref` belongs to, or None.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Case`](liaise.model.md#liaise.model.Case)]

#### cases(, subject=None, state=None)

Every case, or those of `subject` and/or in `state`, in no set order.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Case`](liaise.model.md#liaise.model.Case)]

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
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Case`](liaise.model.md#liaise.model.Case)]

#### get_hold(scope)

The hold on `scope`, or None.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Hold`](liaise.model.md#liaise.model.Hold)]

#### get_issue_check(case_id)

The tick’s reads of the case’s issue state; an empty `IssueCheck` before any.

* **Return type:**
  [`IssueCheck`](liaise.model.md#liaise.model.IssueCheck)

#### get_message(message_id)

The message outside a case with `message_id`, or None.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`OutboundMessage`](liaise.model.md#liaise.model.OutboundMessage)]

#### get_run(run_id)

The record of the run `run_id`, or None.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`RunRecord`](liaise.model.md#liaise.model.RunRecord)]

#### holds()

Every hold, in no set order.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`Hold`](liaise.model.md#liaise.model.Hold)]

#### increment_daily(subject, day)

Count one more dispatch for `subject` on `day`, returning the new count.

* **Return type:**
  [`int`](https://docs.python.org/3/builtins/functions.html#int)

#### mark_daily_cap_notified(subject, day, , at)

Record that the operator was told, `at`, that `subject` reached its cap on `day`.

[`daily_cap_notified()`](#liaise.Ledger.daily_cap_notified) then says so, so they are told once per subject per day.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### mark_seen(delivery_id, , channel, kind, at)

Record the event with `delivery_id` as taken in, so [`seen()`](#liaise.Ledger.seen) dedupes it.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### messages(, subject=None, state=None)

Every message outside a case, or those of `subject`, or in `state`; in no set order.

Raises `ValueError` for a state outside [`MESSAGE_STATES`](liaise.model.md#liaise.model.MESSAGE_STATES).

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`OutboundMessage`](liaise.model.md#liaise.model.OutboundMessage)]

#### new_case(subject, conversation, , reporter, at)

Open a case on `conversation` (an encoded ref), numbered `<subject>-<n>`.

The case starts in `intake`. Raises `ValueError`, handing out no number, when
the conversation already belongs to a case: its messages go to that case.

* **Return type:**
  [`Case`](liaise.model.md#liaise.model.Case)

#### new_message_id(subject)

A fresh `<subject>-m<hex>` id for a message outside a case, unused in the ledger.

The hex part is random, not counted, so two agents sending at once need no lock.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

#### runs(, status=None)

Every run record, or those with `status`, in no set order.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`RunRecord`](liaise.model.md#liaise.model.RunRecord)]

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

#### save_message(message)

Write `message`, replacing what the ledger held under its id.

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
  [`Case`](liaise.model.md#liaise.model.Case)

#### unrouted()

Every queued unrouted message, as a fresh dict, in no set order.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]]

### *class* liaise.LedgerEntry(at, kind, actor=None, grade=None, permission=None, delivery_id=None, text=None, detail=<factory>)

Bases: `_Record`

One thing that happened on a case: appended, never changed.

`kind` is one of `ENTRY_KINDS`. `actor` is a person id (for a
`message`, the person it is attributed to); `grade` and `permission` are
what access was judged on; `delivery_id` is the channel event it came from.
`detail` holds whatever else the kind needs, such as a transition’s `from`,
`to` and `reason`.

### *class* liaise.Outbound(, ref, channel, recipient, purpose, text, title=None, case_id=None, cc=(), bcc=(), attachments=(), project=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A message liaise would send: `text` for `recipient` (a person id) at `ref`.

`ref` is the encoded conversation or address it goes to (`github:example/app#12`,
or `github:example/app` to open an issue there), and `channel` is that ref’s
channel. `purpose` is the outcome kind it carries out (`ask`, `reply`,
`propose`, `deliver`). `title` is the title of the issue it opens, when it opens
one. `case_id` is the case the message belongs to, or None for a message an agent
sends outside any case (`liaise message send`). `cc` and `bcc` are further
recipients (addresses), on channels that have them; `attachments` are the names of
attached files, and `project` the project the message is about, when one is named.
The policy judges every one of these, and the payload hash covers all but `project`.

### *class* liaise.Outcome(kind, text='', questions=(), reason='')

Bases: `_Record`

One outcome a processor run reports: `kind` from `OUTCOME_KINDS`.

Not validated here: `liaise.outcomes` validates a run’s outcomes as a whole.

### *class* liaise.Processor(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

What the tick needs to run a case’s work: the processor seam (`processor=`).

#### cancel(run, , mode='graceful')

Ask `run` to stop (see `CANCEL_MODES`); its session stays resumable.

* **Return type:**
  [`RunRecord`](liaise.model.md#liaise.model.RunRecord)

#### collect(run, , timed_out=False, persist=True)

`run`’s result once it has finished, or None while it is still going.

`persist` is as for [`status()`](#liaise.Processor.status).

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`RunResult`](liaise.model.md#liaise.model.RunResult)]

#### preflight(job)

Whether `job` could start now, and if not why or until when. Starts nothing.

* **Return type:**
  [`Health`](liaise.model.md#liaise.model.Health)

#### resume(session_id, job)

Start `job` continuing `session_id`, in the same permission mode as `start`.

* **Return type:**
  [`RunRecord`](liaise.model.md#liaise.model.RunRecord)

#### start(job)

Start `job` in a new session; for a `run_id` already started, its record.

* **Return type:**
  [`RunRecord`](liaise.model.md#liaise.model.RunRecord)

#### status(run, , persist=True)

`run` with its heartbeat, status and end time refreshed. Never blocks.

With `persist=False`, as in a dry run, the processor writes nothing of its own.

* **Return type:**
  [`RunRecord`](liaise.model.md#liaise.model.RunRecord)

### *class* liaise.Provenance(tainted=None, evidence=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What the run that wrote the message read: tainted, clean, or unknown.

`tainted` is `None` when nobody can say (the hook path), which counts as tainted
(decision 10). `evidence` says why: the messages read and the grades and roles that
the subject does not trust, in words.

#### *classmethod* clean(\*evidence)

A run that read only what the subject trusts.

* **Return type:**
  [`Provenance`](liaise.policy.md#liaise.policy.Provenance)

#### *classmethod* of(value)

`value` as a [`Provenance`](#liaise.Provenance): a record, its dict, a bool, or None.

* **Return type:**
  [`Provenance`](liaise.policy.md#liaise.policy.Provenance)

#### *classmethod* tainted_by(\*evidence)

A run that read something the subject does not trust for `request_work`.

* **Return type:**
  [`Provenance`](liaise.policy.md#liaise.policy.Provenance)

#### to_dict()

JSON-ready.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

#### *classmethod* unknown(\*evidence)

A run nobody can vouch for.

* **Return type:**
  [`Provenance`](liaise.policy.md#liaise.policy.Provenance)

### *class* liaise.RunRecord(run_id, case_id, subject, mode, status, started_at, pid=None, heartbeat_at=None, ended_at=None, session_id=None, stream_path=None, cancel_sent_at=None)

Bases: `_Record`

A processor run started on a case: how it was started, and where it is now.

`cancel_sent_at` is when the tick first cancelled the run for passing its wall clock:
the lost-run deadline counts from it (see [`liaise.tick`](liaise.tick.md#module-liaise.tick)).

### *class* liaise.RunResult(run_id, outcomes=(), usage=<factory>, cost_usd=None, rate_limit=None, error=None, session_id=None, summary='')

Bases: `_Record`

What a finished run returned: its outcomes, what it cost, and how it ended.

### *class* liaise.Subject(slug, bindings, policy, display_name='', workspace=<factory>, brief='', verify='', delivery=<factory>, label_prefix='liaise:', processor=<factory>, source=None, active=True)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A resolved subject: `subjects/<slug>.toml` with every default applied.

#### accepts(permission, grade)

Whether `permission` may be used at authenticity `grade` on this subject.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

#### active *: [bool](https://docs.python.org/3/builtins/functions.html#bool)* *= True*

loaded, shown and used as gate context, but
no tick polls, starts, delivers, nudges or labels anything of it.

* **Type:**
  False for a subject declared but inert

#### brief_for(person)

The brief a run on `person`’s case reads: theirs, else the subject’s, else None.

`policy.briefs[person]` when set, else `brief`. An empty path counts as unset.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

#### notify_address_for(person, , channels=None)

The best address to notify `person` at, or None when the policy has none.

That is the first of [`notify_addresses_for()`](#liaise.Subject.notify_addresses_for), which says how `channels`
filters.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

#### notify_addresses_for(person, , channels=None)

Every address `person` can be notified at on this subject, best first.

`policy.notify[person]` comes first when set, then each `policy.people`
address that maps to `person`, in file order, each once. A string with no
channel part (the `github` of `github:pat`) is not an address, so it is left
out. `channels`, one channel or several, keeps only the addresses on them, so a
GitHub mention is never handed a web-inbox user id.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

#### permissions_for(role)

The permissions `role` grants on this subject (none for an unknown role).

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

#### reply_mode_for(person)

`direct` or `draft`: the person’s override, else the subject’s default.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

#### source *: [str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None)* *= None*

The file this subject was loaded from, when it was.

### *class* liaise.TickReport(plan_lines=(), dispatched=(), collected=(), sent=(), diverted=(), problems=(), dry_run=False, held=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What one [`run_once()`](#liaise.run_once) did or, in a dry run, would do.

`plan_lines` has a line per step, event, case and decision, for `--dry-run` to
print. `dispatched` and `collected` are run ids, `sent` the messages as they went
out (mention added), `diverted` those that stayed with the operator as drafts, and
`held` those the tick put in a case’s outbox (liaise #38).

### *class* liaise.Verdict(, flow, route, reasons, axes, least_cleared, findings, payload_hash, audience_hash, audience, readers, as_of, mode)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What the policy decided about one message, and why (discussion §5.1).

`flow` is one of `FLOWS` and `route` its `ROUTES` entry. `reasons`
are every rule that fired, most restrictive first. `axes` are the values the decision
was made on: `audience` (the scope), `sensitivity` (the highest finding severity,
0 when nothing was found), `relationship` (the most restrictive standing among the
explicit recipients, a tier or `stranger`), `irreversible` (the audience is not
retractable) and `tainted` (true, false, or `None` for unknown). `least_cleared`
is the reader the content ceiling came from. `payload_hash` and `audience_hash`
are what an approval binds to; `audience` is the snapshot the hash was taken over
and `readers` the standing (tier, clearance) of every reader consulted, so the
ledger entry explains itself (§5.7); `as_of` is the `now` the verdict was made
at, and `mode` the policy’s. `route` is `draft` for `delay` until the outbox
exists (`OutboundPolicy.outbox`).

#### *property* rules *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), ...]*

The names of the rules that fired, most restrictive first, each once.

#### to_dict()

JSON-ready: what the ledger records for a gated message.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### liaise.authorize(message, subject, permission, \*, resolver=<function resolve_person>)

Whether `message` may exercise `permission` on `subject`, by the label-as-claim rule.

See the module docstring for the rule. `resolver` maps the author’s address to a
person id (default [`resolve_person()`](#liaise.resolve_person)). Raises `ValueError` for a permission
outside [`PERMISSIONS`](liaise.model.md#liaise.model.PERMISSIONS).

* **Return type:**
  [`AccessDecision`](liaise.access.md#liaise.access.AccessDecision)

### liaise.default_ledger_store(state_dir)

The default ledger store: one JSON file per key in `<state_dir>/ledger`.

Creates the directory, since `dol.Jsons` will not create one on write.

* **Return type:**
  [`MutableMapping`](https://docs.python.org/3/library/collections.abc.html#collections.abc.MutableMapping)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### liaise.evaluate(outbound, \*, audience, disclosure, findings, provenance, policy=None, now, identities=None, \_rules=(Rule(name='secrets', predicate=<function secrets>, flow='refuse'), Rule(name='seals', predicate=<function seals>, flow='refuse'), Rule(name='exfiltration', predicate=<function exfiltration>, flow='refuse'), Rule(name='personal, public', predicate=<function personal_public>, flow='refuse'), Rule(name='no write-down', predicate=<function no_write_down>, flow='revise'), Rule(name='co-ownership', predicate=<function co_ownership>, flow='revise'), Rule(name='personal, private', predicate=<function personal_private>, flow='approve'), Rule(name='tier', predicate=<function tier>, flow='approve'), Rule(name='stranger', predicate=<function stranger>, flow='approve'), Rule(name='disclosure stance', predicate=<function disclosure_stance>, flow='approve'), Rule(name='taint', predicate=<function taint>, flow='approve'), Rule(name='reply mode', predicate=<function reply_mode>, flow='approve'), Rule(name='irreversibility', predicate=<function irreversibility>, flow='delay'), Rule(name='unknown audience', predicate=<function unknown_audience>, flow='send')))

The [`Verdict`](#liaise.Verdict) for `outbound`, through every rule of the table.

Pure: no I/O, no clock (`now` is given), and the same verdict for the same inputs.
See the module docstring for what each input is. `_rules` is for the mutation
checks of the test suite only (the table is not a seam); it must hold rules of the
table by name.

* **Return type:**
  [`Verdict`](liaise.policy.md#liaise.policy.Verdict)

```pycon
>>> from datetime import datetime, timezone
>>> now = datetime(2026, 9, 15, tzinfo=timezone.utc)
>>> ada = {"people": {"ada": {"tier": "open", "clearance": "amber"}}}
>>> email = {"ref": "email:ada", "scope": "named", "readers": ["email:ada"]}
>>> message = {"ref": "email:ada", "channel": "email", "recipient": "ada", "text": "hi", "case_id": "s-1"}
>>> verdict = evaluate(message, audience=email, disclosure=ada, findings=(), provenance=False,
...                    now=now, identities={"email:ada": "ada"})
>>> verdict.flow, verdict.route, verdict.rules
('send', 'send', ())
>>> evaluate(message, audience=None, disclosure=ada, findings=(), provenance=False, now=now).rules
('irreversibility', 'unknown audience')
```

### liaise.hold(ledger, scope, , mode='block', reason='', set_by='operator', now=None)

Put a `mode` hold on `scope`, replacing any hold already there, and return it.

Raises `ValueError`, writing nothing, for a scope outside the accepted forms or a
mode outside `HOLD_MODES`.

* **Return type:**
  [`Hold`](liaise.model.md#liaise.model.Hold)

### liaise.intake(subject, ledger, \*, registry=None, resolver=<function resolve_person>, now=None, dry_run=False)

Take in what arrived on `subject`’s bindings since the ledger’s cursors.

**Polling.** Each conversation the bindings name is polled once, however many
bindings name it, with the `?conditions` stripped
(`github:example/app?labels=partner:pat` polls `github:example/app`), through
`correspond.listen` over the ledger’s cursors and `registry` (correspond’s own
when None). Each message it yields meets every binding. A binding with a wildcard in
its conversation part cannot be polled in v0.1: it is reported in `problems` and
skipped. A channel that fails is a problem too, and the other conversations are
still polled.

**Adoption, before a first poll.** correspond’s GitHub adapter looks back only a day
the first time it polls a repository, so an older open issue would never be heard.
When the ledger holds no cursor for a repository on one of `ADOPTING_CHANNELS`,
intake first reads its open issues (`correspond.read`, the
`ADOPTION_READ_LIMIT` most recent) and takes in each opening post a binding
matches, exactly as the poll would: the same routing, authorization and legacy
adoption, marked seen under the delivery id listen gives that post, so no poll takes
it in again. It then reads each such issue’s comments (`correspond.read` on
`github:owner/repo#N`) and takes them in the same way, each under the delivery id a
poll would give it, so readiness sees the partner’s markers and activity from before
the adoption. When a read fails, that is a problem and the repository is not polled, so its cursor
stays unset and the next intake tries the adoption again.

**Routing.** An event with no message is passed over, as is a delivery this intake
already dealt with (a poll hearing an issue adopted moments before). So is one whose
delivery id the ledger has already seen (`duplicate`). Otherwise:

- Its conversation belongs to one of this subject’s cases: the message becomes a
  `message` entry on it. A message by the channel’s own account (`is_self`) is
  recorded with the actor `self`, and one by a `policy.relays` author with its
  address as the actor. Each carries its `detail["role"]` (`self` or `relay`)
  and no permission: neither is partner activity, and neither is unrouted. Anyone
  else must be authorized to `report`, or the message is queued as unrouted with
  the reason, and no entry.
- A binding matches it (`correspond.routing.binding_matches`): when its sender may
  `report`, a case opens on its conversation with that person as reporter and the
  message as its first entry, in `intake`. Otherwise it is unrouted with the reason.
  A closed issue’s opening opens no case.
- Neither: it is not this subject’s (`ignored`), and it is not marked seen.

**Legacy adoption.** An issue opening that already carries a
`<label_prefix><state>` label opens its case in that state rather than `intake`,
recorded as a transition. One carrying several state labels opens in
`needs-owner`, with a problem saying why. Either way the case also gets a `run`
entry, `{"event": "adopted", "adopted": True}`, stamped `now`: one adopted in
`needs-partner` waits for its partner to write after it (see [`liaise.tick`](liaise.tick.md#module-liaise.tick)).
The 0.0.x session id (the `sessions__<repo>__<n>` key) is not carried over: that is
out of scope here.

**Dry run.** Cursors are not committed, and nothing reaches the ledger’s store,
adoptions included: unless the store is already a `ChainMap` overlay (as the tick
passes it), intake works on one of its own. Since no cursor is committed, every dry
run before the first real one adopts again.

Cursors are kept per conversation, not per subject, so two subjects polling one
conversation would starve each other: [`load_subjects()`](liaise.subjects.md#liaise.subjects.load_subjects) refuses
such a configuration.

`resolver` maps a sender’s address to a person (see [`liaise.access`](liaise.access.md#module-liaise.access)). `now`
stamps the inbox records and adoption transitions (default: the current UTC time).

* **Return type:**
  [`IntakeReport`](#liaise.IntakeReport)

### liaise.load_global_config(root=None)

Load `<root>/config.toml` into a [`GlobalConfig`](#liaise.GlobalConfig), defaults applied.

`root` defaults to `~/.config/liaise`. A missing file raises [`ConfigError`](#liaise.ConfigError)
naming the path and the minimal content it needs, as does a missing `owner_login`
or `state_dir`. Nothing under `partners/` is read (see `load_config()`).

* **Return type:**
  [`GlobalConfig`](liaise.config.md#liaise.config.GlobalConfig)

### liaise.load_subjects(root=None)

Every subject under `<root>/subjects/*.toml`, keyed by slug, in slug order.

`root` defaults to `~/.config/liaise`. No `subjects` directory means no subjects.
Raises [`ConfigError`](liaise.config.md#liaise.config.ConfigError) for a file that does not load, and, naming
both files, for two subjects whose bindings are polled on the same conversation.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Subject`](liaise.subjects.md#liaise.subjects.Subject)]

### liaise.migrate_config(root=None, , apply=False, registry=None)

Plan the 0.1 subject files for the 0.0.x config under `root`; write them if `apply`.

`root` defaults to `~/.config/liaise`. A dry run (the default) writes nothing.
`apply=True` creates `subjects/<slug>.toml` for each subject whose file does not
exist yet (see `apply_plan()`). `registry` is correspond’s channel registry,
used only to check bindings (its default when None; that check runs no adapter).
Raises [`ConfigError`](liaise.config.md#liaise.config.ConfigError) when the 0.0.x config does not load.

* **Return type:**
  [`MigrationPlan`](liaise.migrate.md#liaise.migrate.MigrationPlan)

### liaise.notify(title, body, , priority='default', topic_env='LIAISE_NTFY_TOPIC', base_url='https://ntfy.sh')

POST `body` to the ntfy topic named by the `topic_env` environment variable.

Returns whether it actually sent. No-ops (returns False) when `topic_env`
is unset. Never raises — a notification failure must not take down a run
that otherwise succeeded. Build `body` with `notice_body()`.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### liaise.outbound_policy(outbound, ctx, \*, disclosure=<function acquaint_disclosure>, detectors=(<function secret_detector.<locals>.detect_secrets>, <function detect_canaries>, <function detect_vocabulary>, <function chain.<locals>.chained>, <function chain.<locals>.chained>, <function detect_third_parties>), resolver=<function resolve_person>)

Hold back what the outbound policy (discussion §5.4) does not let go now.

The audience is the context’s (unknown, so public, when it has none), the disclosure
comes through `disclosure` (acquaint’s, or every reader at `need-to-know` without
it), and the provenance is the context’s (unknown, so tainted, when it has none). See
[`liaise.outbound.judge()`](liaise.outbound.md#liaise.outbound.judge). A `send` verdict passes, noting the audience; any
other diverts at its flow, one concern per rule that fired. It never redacts: what it
found is for the operator to fix, and its reasons say where, never what.

* **Return type:**
  `Union`[[`Pass`](liaise.gate.md#liaise.gate.Pass), [`Divert`](liaise.gate.md#liaise.gate.Divert)]

### liaise.parse_outcomes(structured_output)

The outcomes of a run’s structured output, checked against `OUTCOME_SCHEMA`.

Beyond the schema, each kind must carry the field `REQUIRED_FIELD_BY_KIND`
names: an `ask` its questions, a `reply` its text, and so on. A `decline` is
kept as reported ([`plan_outcomes()`](#liaise.plan_outcomes) normalizes it). Raises `ValueError`
listing every problem found, not only the first.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`Outcome`](liaise.model.md#liaise.model.Outcome), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

### liaise.plan_outcomes(case, outcomes, subject, , now, sending_channels=('github',), address_channels=('email',))

The actions that carry out `outcomes` on `case`, one group per outcome, in order.

The module docstring lists what each kind plans; `decline` is planned as
`escalate` (see `normalize()`). Messages are for `case.reporter`, and go to
the case’s first conversation on one of `sending_channels`. Failing that, they go
to the reporter’s first notify address on one of `address_channels` (see
[`notify_address_for()`](liaise.subjects.md#liaise.subjects.Subject.notify_address_for)). Failing both, each becomes a
`StoreDraft` held for `no channel to reach <person>`, with a
`NotifyOperator` that names the case, not the person. `now` stamps the drafts.

Raises `ValueError` for an outcome kind outside the vocabulary.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[`Union`[[`Send`](liaise.outcomes.md#liaise.outcomes.Send), [`Transition`](liaise.outcomes.md#liaise.outcomes.Transition), [`NotifyOperator`](liaise.outcomes.md#liaise.outcomes.NotifyOperator), [`StoreDraft`](liaise.outcomes.md#liaise.outcomes.StoreDraft), [`Deliver`](liaise.outcomes.md#liaise.outcomes.Deliver), [`Defer`](liaise.outcomes.md#liaise.outcomes.Defer), [`DigestNote`](liaise.outcomes.md#liaise.outcomes.DigestNote)]]

### liaise.resolve_person(address, subject)

The person id `address` belongs to on `subject`, or None.

`policy.people` first, matched without regard to case. Otherwise, when acquaint
imports, `acquaint.resolve`, trusted only when it is `ok` with exactly one match.
A missing or broken acquaint, or any error it raises, resolves to None.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.run_gate(outbound, ctx, \*, outbound_filters=(<function outside_a_case>, <function outbound_policy>, <function writing_card>, <function deslop>, <function notify_recipient>))

Run `outbound` through every one of `outbound_filters`, in order, and decide.

Each `Pass` hands its message, possibly rewritten, to the next filter; each
`Divert` adds its concerns. An approval on the context settles what it binds to
and names (see the module docstring). The decision’s flow is the most restrictive
concern left, and only `send` sends. The gate fails closed: a filter that raises, or
returns anything but a `Pass` (of an [`Outbound`](#liaise.Outbound)) or a `Divert`, adds an
`approve` concern naming it.

* **Return type:**
  [`GateDecision`](liaise.gate.md#liaise.gate.GateDecision)

### liaise.run_once(subjects, store, \*, global_config, registry=None, processor=None, resolver=<function resolve_person>, workspace=<function workspace_for>, labeler=None, notify_fn=None, sessions_dir=None, now=None, dry_run=False, only=None, outbound_filters=(<function outside_a_case>, <function outbound_policy>, <function writing_card>, <function deslop>, <function notify_recipient>), triage=None, lost_run_deadline=datetime.timedelta(seconds=600), closed_recheck_interval=datetime.timedelta(seconds=3600), sends=None)

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
- `triage`: a `Triage` that groups and orders each subject’s ready cases
  before they start; None keeps the tick’s own order, oldest first;
- `sends`: the idempotency records every send’s key is kept in, by default
  [`liaise.release.default_send_store()`](liaise.release.md#liaise.release.default_send_store) (`<state_dir>/sends`), so a message
  > that may have gone out is never posted again.

`only` is a slug or slugs to run alone; an unknown one raises
[`ConfigError`](liaise.config.md#liaise.config.ConfigError). An inactive subject (`active = false`) is not
ticked: `only` may name one in a dry run alone, and outside one that raises
[`ConfigError`](liaise.config.md#liaise.config.ConfigError) too. `now` is the tick’s clock (the current UTC
time when None). `lost_run_deadline` is how long after the tick cancelled a run for
its wall clock a run that will not stop is waited on (`LOST_RUN_DEADLINE`), and
`closed_recheck_interval` how long a case whose issue was read closed goes before
it is read again (`CLOSED_RECHECK_INTERVAL`). Unless `dry_run`, the tick holds
the run lock in `state_dir` (raising `RunLockHeld` while another tick holds
it) and stamps its start and end in `store`.

* **Return type:**
  [`TickReport`](liaise.tick.md#liaise.tick.TickReport)

### liaise.status_lines(subjects, store, , global_config, now=None, recent=5)

What `liaise status` prints: what the ledger in `store` says, read only.

The run stamps (`running`, `interrupted` or `finished`, the lock checked in
`state_dir`), the holds, the runs in flight with their heartbeat age, each subject’s
cases by state and dispatches today, the unrouted queue (its size and the `recent`
latest), the drafts waiting for the operator, the messages held in the outbox, and the
`recent` latest digest notes.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.unhold(ledger, scope)

Lift the hold on `scope`, whoever set it; True when there was one.

Raises `ValueError` for a scope outside the accepted forms.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### Modules

| [`access`](liaise.access.md#module-liaise.access)         | Identity and access: who a message is from, and whether they may do what it asks.                                    |
|--------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------|
| [`cases`](liaise.cases.md#module-liaise.cases)           | Cases as the operator sees and moves them: `liaise case list`, `show`, `set-state`, `send-draft` and `reject-draft`. |
| [`cli`](liaise.cli.md#module-liaise.cli)               | The `liaise` command line (liaise 0.1).                                                                              |
| [`config`](liaise.config.md#module-liaise.config)         | The global config, and the 0.0.x partner files `liaise migrate-config` reads.                                        |
| [`detect`](liaise.detect.md#module-liaise.detect)         | Detectors for outbound messages: what a message holds, reported without the value.                                   |
| [`errors`](liaise.errors.md#module-liaise.errors)         | Processor error taxonomy (design §3.6): classify how a run ended, and what the tick does.                            |
| [`gate`](liaise.gate.md#module-liaise.gate)             | The outbound gate: the checks every message passes before liaise sends it, and the verdict they reach.               |
| [`github`](liaise.github.md#module-liaise.github)         | The GitHub seam: one protocol, two implementations.                                                                  |
| [`holds`](liaise.holds.md#module-liaise.holds)           | Holds: stops on work, by scope, set by the operator or by the tick itself.                                           |
| [`ledger`](liaise.ledger.md#module-liaise.ledger)         | The ledger: liaise's own record of what it has seen, opened, decided and started.                                    |
| [`messages`](liaise.messages.md#module-liaise.messages)     | Messages outside a case: what an agent says to a person on its own initiative, through the gate.                     |
| [`migrate`](liaise.migrate.md#module-liaise.migrate)       | Derive 0.1 subject files from a 0.0.x configuration: `liaise migrate-config`.                                        |
| [`model`](liaise.model.md#module-liaise.model)           | The liaise 0.1 data model: cases, ledger entries, outcomes, holds and runs.                                          |
| [`outbound`](liaise.outbound.md#module-liaise.outbound)     | What the outbound gate's policy filter gathers before the policy decides.                                            |
| [`outbox`](liaise.outbox.md#module-liaise.outbox)         | The delay outbox: messages the gate gave `delay`, held for a cancellable window (liaise #38).                        |
| [`outcomes`](liaise.outcomes.md#module-liaise.outcomes)     | Outcomes: what a processor run reports, checked, then planned into actions.                                          |
| [`policy`](liaise.policy.md#module-liaise.policy)         | Policy and verdict for outbound messages: from findings and an audience to a flow.                                   |
| [`processor`](liaise.processor.md#module-liaise.processor)   | Processors (design §3.6): what runs a case's work, detached, and how that run ended.                                 |
| [`projection`](liaise.projection.md#module-liaise.projection) | Label projection: a case's state, shown on each of its GitHub issues as one label.                                   |
| [`prompt`](liaise.prompt.md#module-liaise.prompt)         | The prompt composer: the whole prompt a processor run on one case starts from.                                       |
| [`readiness`](liaise.readiness.md#module-liaise.readiness)   | Readiness: whether a case is ready to dispatch, read off its own ledger entries.                                     |
| [`release`](liaise.release.md#module-liaise.release)       | Releasing a message: through the gate, then through correspond, as one step.                                         |
| [`report`](liaise.report.md#module-liaise.report)         | `liaise gate report`: what the outbound gate did, in counts, and whether to enforce (liaise #39).                    |
| [`schedule`](liaise.schedule.md#module-liaise.schedule)     | Scheduling `liaise run --once` (A.7): a launchd agent on macOS, a systemd user timer on Linux.                       |
| [`subjects`](liaise.subjects.md#module-liaise.subjects)     | Subjects: the bodies of work liaise runs, each loaded from `subjects/<slug>.toml`.                                   |
| [`testing`](liaise.testing.md#module-liaise.testing)       | Fakes shipped with liaise: for its tests, and for the one-command smoke test.                                        |
| [`tick`](liaise.tick.md#module-liaise.tick)             | The tick: one pass of liaise 0.1's loop (design §3.1).                                                               |
| [`workspace`](liaise.workspace.md#module-liaise.workspace)   | The checkout a subject's runs share: one run at a time, and never beside a live session.                             |
