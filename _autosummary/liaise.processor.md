# liaise.processor

Processors (design §3.6): what runs a case’s work, detached, and how that run ended.

A [`Processor`](#liaise.processor.Processor) has six verbs, each with its default in [`ClaudeHeadless`](#liaise.processor.ClaudeHeadless):

- `preflight(job)`: whether work could start now: the command, the checkout, and whether
  the login still works. It never starts any.
- `start(job)` and `resume(session_id, job)`: spawn a detached run and return its
  [`RunRecord`](liaise.model.md#liaise.model.RunRecord) at once. Both are idempotent on `job.run_id`.
- `status(run)`: never blocks. It refreshes the heartbeat, the status and the end time.
- `cancel(run, mode=...)`: interrupt, then terminate. The session stays resumable. Only a
  process verified as the run’s, by when it started, is ever signalled.
- `collect(run)`: `None` while the run is going, then its
  [`RunResult`](liaise.model.md#liaise.model.RunResult), classified by [`liaise.errors.classify()`](liaise.errors.md#liaise.errors.classify).

In a dry run the tick passes `persist=False` to `status` and `collect`, which then
write nothing.

[`ClaudeHeadless`](#liaise.processor.ClaudeHeadless) keeps each run’s files in `<runs_dir>/<run_id>/`:

```default
prompt.md       the prompt; the command line only points at it
stream.jsonl    claude's stream-JSON stdout, whose mtime is the heartbeat
stderr.log      claude's stderr
record.json     the RunRecord, plus the cancel bookkeeping
```

The files hold everything, so a run one tick started is checked, cancelled and collected
by the next. [`EchoProcessor`](#liaise.processor.EchoProcessor) runs nothing and returns scripted results, for tests.

### Module Attributes

| [`FRESH`](#liaise.processor.FRESH)               | A run is a new session (`fresh`) or continues a stored one (`resume`).                                                                                                  |
|----------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`RESUME`](#liaise.processor.RESUME)              | A run is a new session (`fresh`) or continues a stored one (`resume`).                                                                                                  |
| [`RUN_STATUSES`](#liaise.processor.RUN_STATUSES)        | A run's `status` in its RunRecord.                                                                                                                                      |
| [`CANCEL_MODES`](#liaise.processor.CANCEL_MODES)        | `graceful` interrupts first and terminates after the grace period; `now` terminates.                                                                                    |
| [`DFLT_GRACE_S`](#liaise.processor.DFLT_GRACE_S)        | Seconds between a graceful cancel's interrupt and the terminate that may follow it.                                                                                     |
| [`KILL_GRACE_S`](#liaise.processor.KILL_GRACE_S)        | Seconds a run may go on after a cancel sent it SIGTERM before a later cancel kills it with SIGKILL (POSIX only: on Windows every cancel already ends the run outright). |
| [`SCRUBBED_ENV_VARS`](#liaise.processor.SCRUBBED_ENV_VARS)   | with either set, claude bills that key instead of the subscription the operator logged in with.                                                                         |
| [`CHILD_ENV_OVERRIDES`](#liaise.processor.CHILD_ENV_OVERRIDES) | claude retries transient API errors, a bounded number of times, before it gives up and reports them.                                                                    |
| [`DFLT_AUTH_CHECK`](#liaise.processor.DFLT_AUTH_CHECK)     | `claude auth status`, which exits non-zero once the login is gone.                                                                                                      |
| [`DFLT_AUTH_TIMEOUT_S`](#liaise.processor.DFLT_AUTH_TIMEOUT_S) | Seconds [`ClaudeHeadless.preflight()`](#liaise.processor.ClaudeHeadless.preflight) gives that check to answer.                                                         |
| [`PROMPT_POINTER`](#liaise.processor.PROMPT_POINTER)      | The only instruction on the command line; the prompt itself stays in its file.                                                                                          |

### Functions

| [`child_env`](#liaise.processor.child_env)(environ)   | The environment a run gets: `environ` without [`SCRUBBED_ENV_VARS`](#liaise.processor.SCRUBBED_ENV_VARS), plus [`CHILD_ENV_OVERRIDES`](#liaise.processor.CHILD_ENV_OVERRIDES).   |
|-----------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

### Classes

| [`ClaudeHeadless`](#liaise.processor.ClaudeHeadless)(\*[, claude_bin, runs_dir, ...])   | The default [`Processor`](#liaise.processor.Processor): the `claude` CLI, headless, stream-JSON, detached.    |
|----------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------|
| [`EchoProcessor`](#liaise.processor.EchoProcessor)(\*[, results, default, health])     | A [`Processor`](#liaise.processor.Processor) that runs nothing: it records jobs and returns scripted results. |
| [`Job`](#liaise.processor.Job)(run_id, case_id, subject, prompt, cwd, ...)   | Everything a [`Processor`](#liaise.processor.Processor) needs to run one case once.                           |
| [`Processor`](#liaise.processor.Processor)(\*args, \*\*kwargs)                     | What the tick needs to run a case's work: the processor seam (`processor=`).                                                  |

### liaise.processor.CANCEL_MODES *= ('graceful', 'now')*

`graceful` interrupts first and terminates after the grace period; `now` terminates.

### liaise.processor.CHILD_ENV_OVERRIDES *= mappingproxy({'CLAUDE_CODE_MAX_RETRIES': '3'})*

claude retries transient API errors, a bounded number
of times, before it gives up and reports them.

* **Type:**
  Set in every run’s environment

### *class* liaise.processor.ClaudeHeadless(, claude_bin='claude', runs_dir=None, grace_s=15.0, kill_grace_s=60.0, auth_check=('auth', 'status'), auth_timeout_s=15.0)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

The default [`Processor`](#liaise.processor.Processor): the `claude` CLI, headless, stream-JSON, detached.

`runs_dir` is where each run’s directory goes; the tick passes
`<state_dir>/runs`, and nothing but [`preflight()`](#liaise.processor.ClaudeHeadless.preflight) works without it.
`claude_bin` is the command, found on `PATH` or given as a path. `grace_s` is
how long a graceful cancel waits after its interrupt before it may terminate, and
`kill_grace_s` how long a run that was sent SIGTERM may go on before a cancel kills
it (POSIX). `auth_check` is the arguments that ask `claude` whether its login
still works ([`DFLT_AUTH_CHECK`](#liaise.processor.DFLT_AUTH_CHECK)), which [`preflight()`](#liaise.processor.ClaudeHeadless.preflight) gives
`auth_timeout_s` seconds to answer; None skips that check, for a `claude` without
the command.

A run is spawned in its own process group (a new session on POSIX), so it outlives
the tick that started it and a cancel reaches everything it started. Its environment
is [`child_env()`](#liaise.processor.child_env).

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
it is. Only a process verified as the run’s (see [`status()`](#liaise.processor.ClaudeHeadless.status)) is signalled: a
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
for [`status()`](#liaise.processor.ClaudeHeadless.status). A run whose id names another case’s record is `config_error`,
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

Like [`start()`](#liaise.processor.ClaudeHeadless.start), but continuing `session_id` (`--resume`).

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
nothing, and [`collect()`](#liaise.processor.ClaudeHeadless.collect) classifies it as `config_error`. Never raises for a
command that cannot be spawned: the run is recorded as finished with the reason in
its `stderr.log`, which [`collect()`](#liaise.processor.ClaudeHeadless.collect) classifies as `config_error`.

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

### liaise.processor.DFLT_AUTH_CHECK *= ('auth', 'status')*

`claude auth
status`, which exits non-zero once the login is gone.

* **Type:**
  The arguments that ask `claude` whether its login still works

### liaise.processor.DFLT_AUTH_TIMEOUT_S *= 15.0*

Seconds [`ClaudeHeadless.preflight()`](#liaise.processor.ClaudeHeadless.preflight) gives that check to answer.

### liaise.processor.DFLT_GRACE_S *= 15.0*

Seconds between a graceful cancel’s interrupt and the terminate that may follow it.

### *class* liaise.processor.EchoProcessor(, results=None, default=None, health=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A [`Processor`](#liaise.processor.Processor) that runs nothing: it records jobs and returns scripted results.

`results` maps a case id to the [`RunResult`](liaise.model.md#liaise.model.RunResult) its runs return,
and `default` is returned for any other case; without either, a run reports one
`note`. `health` is what `preflight()` returns (ok by default). A run is
finished as soon as it starts. It writes nothing, so `persist` changes nothing. `jobs`, `preflights` and `cancels` record the
calls, for tests to assert on.

### liaise.processor.FRESH *= 'fresh'*

A run is a new session (`fresh`) or continues a stored one (`resume`).

### *class* liaise.processor.Job(run_id, case_id, subject, prompt, cwd, permission_mode, timeout_minutes, json_schema, session_id=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Everything a [`Processor`](#liaise.processor.Processor) needs to run one case once.

`run_id` names the run, and its directory, so it is one path segment. `prompt`
is the whole prompt (see [`liaise.prompt.compose_case_prompt()`](liaise.prompt.md#liaise.prompt.compose_case_prompt)), `cwd` is where
the work happens, and `json_schema` is the structured result the run must end
with. `timeout_minutes` is the wall clock the tick enforces; a processor does not.
`session_id` is the case’s stored session, if it has one: the tick hands it to
[`Processor.resume()`](#liaise.processor.Processor.resume), while `start` always opens a new session.

### liaise.processor.KILL_GRACE_S *= 60.0*

Seconds a run may go on after a cancel sent it SIGTERM before a later cancel kills it
with SIGKILL (POSIX only: on Windows every cancel already ends the run outright).

### liaise.processor.PROMPT_POINTER *= 'Read and follow the instructions in {prompt_path}'*

The only instruction on the command line; the prompt itself stays in its file.

### *class* liaise.processor.Processor(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

What the tick needs to run a case’s work: the processor seam (`processor=`).

#### cancel(run, , mode='graceful')

Ask `run` to stop (see [`CANCEL_MODES`](#liaise.processor.CANCEL_MODES)); its session stays resumable.

* **Return type:**
  [`RunRecord`](liaise.model.md#liaise.model.RunRecord)

#### collect(run, , timed_out=False, persist=True)

`run`’s result once it has finished, or None while it is still going.

`persist` is as for [`status()`](#liaise.processor.Processor.status).

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

### liaise.processor.RESUME *= 'resume'*

A run is a new session (`fresh`) or continues a stored one (`resume`).

### liaise.processor.RUN_STATUSES *= ('running', 'finished')*

A run’s `status` in its RunRecord.

### liaise.processor.SCRUBBED_ENV_VARS *= ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')*

with either set, claude bills that key instead of
the subscription the operator logged in with.

* **Type:**
  Variables a run must not inherit

### liaise.processor.child_env(environ)

The environment a run gets: `environ` without [`SCRUBBED_ENV_VARS`](#liaise.processor.SCRUBBED_ENV_VARS), plus
[`CHILD_ENV_OVERRIDES`](#liaise.processor.CHILD_ENV_OVERRIDES).

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

```pycon
>>> env = child_env({"PATH": "/bin", "ANTHROPIC_API_KEY": "k"})
>>> sorted(env)
['CLAUDE_CODE_MAX_RETRIES', 'PATH']
```
