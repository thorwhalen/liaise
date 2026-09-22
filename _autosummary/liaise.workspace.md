# liaise.workspace

The checkout a subject’s runs share: one run at a time, and never beside a live session.

v0.1 has one kind of workspace (seam 5 in the design): the subject’s own checkout,
shared by its runs. Two checks keep a run from trampling other work there:

- **The lock.** [`SharedCheckout.acquire()`](#liaise.workspace.SharedCheckout.acquire) writes
  `<lock_dir>/<sha1(resolved path)[:16]>.lock`, the JSON `{pid, run_id, path, at}`.
  A different run is refused while the holder’s pid is alive, and a lock whose pid is
  dead is reclaimed. Like the run lock of 0.0.x it is advisory, which is enough to keep
  one machine’s runs from colliding.
- **The collision check.** [`SharedCheckout.conflict()`](#liaise.workspace.SharedCheckout.conflict) reads Claude Code’s session
  records (`~/.claude/sessions/*.json` by default) and reports a live session, not one
  of liaise’s own runs, whose `cwd` is the checkout or inside it: someone working
  there by hand.

Paths are compared resolved (`~` expanded, symlinks followed) and on path-part
boundaries, so `.../app` never matches `.../app2`.

It also says whether a pid still names a given process ([`pid_matches_start()`](#liaise.workspace.pid_matches_start)): a pid
is reused once its process is gone, after a reboot say, so a live pid alone proves nothing.

The named replacement is a worktree per run, which would come in at [`workspace_for()`](#liaise.workspace.workspace_for).

### Module Attributes

| [`DFLT_SESSIONS_DIR`](#liaise.workspace.DFLT_SESSIONS_DIR)       | Where Claude Code keeps one JSON record per session.                                                                                                                                                |
|--------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`DFLT_LOCKS_SUBDIR`](#liaise.workspace.DFLT_LOCKS_SUBDIR)       | The workspace locks' directory under `state_dir`.                                                                                                                                                   |
| [`DFLT_LOCK_DIGEST_LENGTH`](#liaise.workspace.DFLT_LOCK_DIGEST_LENGTH) | How many hex digits of the resolved path's sha1 a lock file's name keeps.                                                                                                                           |
| [`PID_START_TOLERANCE`](#liaise.workspace.PID_START_TOLERANCE)     | How far a process's start may lie from the start recorded for it, either side, for its pid to be taken for that process (see [`pid_matches_start()`](#liaise.workspace.pid_matches_start)). |
| [`PS_COMMAND`](#liaise.workspace.PS_COMMAND)              | The command that reports a process's elapsed time on POSIX, and how many seconds it is given to answer.                                                                                             |

### Functions

| [`pid_is_alive`](#liaise.workspace.pid_is_alive)(pid)                             | Whether `pid` names a live process.                                                            |
|------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------|
| [`pid_matches_start`](#liaise.workspace.pid_matches_start)(pid, started_at, \*[, ...]) | Whether `pid` is the live process that started at `started_at`; None when that cannot be told. |
| [`process_started_at`](#liaise.workspace.process_started_at)(pid)                       | When the process `pid` started, in UTC; None when that cannot be determined.                   |
| [`resolve_checkout`](#liaise.workspace.resolve_checkout)(path)                        | `path` in the one form checkouts are compared in: `~` expanded, absolute, resolved.            |
| [`workspace_for`](#liaise.workspace.workspace_for)(subject, \*, lock_dir[, ...])   | The shared checkout `subject` works in, or None when its file names no workspace path.         |

### Classes

| [`SharedCheckout`](#liaise.workspace.SharedCheckout)(path, \*, lock_dir[, ...])   | One checkout shared by a subject's runs: its lock, and the live sessions inside it.   |
|----------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------|

### liaise.workspace.DFLT_LOCKS_SUBDIR *= 'locks'*

The workspace locks’ directory under `state_dir`.

### liaise.workspace.DFLT_LOCK_DIGEST_LENGTH *= 16*

How many hex digits of the resolved path’s sha1 a lock file’s name keeps.

### liaise.workspace.DFLT_SESSIONS_DIR *= '~/.claude/sessions'*

Where Claude Code keeps one JSON record per session.

### liaise.workspace.PID_START_TOLERANCE *= datetime.timedelta(seconds=120)*

How far a process’s start may lie from the start recorded for it, either side, for its
pid to be taken for that process (see [`pid_matches_start()`](#liaise.workspace.pid_matches_start)). A process that started
further away is another one, which was given the pid after the first was gone.

### liaise.workspace.PS_COMMAND *= 'ps'*

The command that reports a process’s elapsed time on POSIX, and how many seconds it is
given to answer.

### *class* liaise.workspace.SharedCheckout(path, , lock_dir, sessions_dir=None, own_pids=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One checkout shared by a subject’s runs: its lock, and the live sessions inside it.

`path` is resolved once (see [`resolve_checkout()`](#liaise.workspace.resolve_checkout)). `lock_dir` holds one lock
file per checkout. `sessions_dir` is where Claude Code keeps its session records
([`DFLT_SESSIONS_DIR`](#liaise.workspace.DFLT_SESSIONS_DIR) when None). `own_pids` are the pids of liaise’s own
runs, whose sessions are never a conflict.

#### acquire(, run_id, pid, now=None)

Take the checkout’s lock for `run_id`, held while `pid` lives; False when refused.

Refused only while a different run holds the lock and its pid is alive. A lock
left by a dead pid, or one that cannot be read, is reclaimed. The same run
acquiring again succeeds and rewrites the lock, which is how a lock taken before
a run starts moves to the run’s own pid once it has one.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

#### conflict()

`live session <name> in <cwd>` when another live session works in the checkout.

A session record conflicts when its `pid` is alive and not in `own_pids`, and
its `cwd` is the checkout or a path inside it. The first such record, in file
name order, is reported by its `name`, else its `sessionId`. None when there
is none. A missing directory, an unreadable or malformed record, or a `cwd` that
is not absolute never conflicts and never raises.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

#### holder()

The lock’s record, `{pid, run_id, path, at}`, or None when there is no readable lock.

The holder’s pid may be dead: [`acquire()`](#liaise.workspace.SharedCheckout.acquire) reclaims such a lock.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]]

#### *property* lock_path *: [Path](https://docs.python.org/3/library/pathlib.html#pathlib.Path)*

`<lock_dir>/<sha1(resolved path)[:16]>.lock`.

* **Type:**
  This checkout’s lock file

#### release(, run_id)

Remove the lock if `run_id` holds it; True when it was removed.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### liaise.workspace.pid_is_alive(pid)

Whether `pid` names a live process. Never sends it a real signal.

Anything but a positive int in pid range is no live process, so a malformed session
record or lock never raises here. On POSIX this is `os.kill(pid, 0)`, which delivers
nothing. On Windows `os.kill` would terminate the process instead, so the check
opens a query-only handle, and then asks for its exit code. Opening the handle is not
enough: Windows keeps an exited process openable while anyone still holds a handle to
it, such as the `Popen` that started it. Such a process is alive only if its exit
code is still `STILL_ACTIVE`. `liaise.run._pid_is_alive`, retired with that module,
lacked this check.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### liaise.workspace.pid_matches_start(pid, started_at, , tolerance=datetime.timedelta(seconds=120))

Whether `pid` is the live process that started at `started_at`; None when that cannot be told.

True when the process is alive ([`pid_is_alive()`](#liaise.workspace.pid_is_alive)) and started
([`process_started_at()`](#liaise.workspace.process_started_at)) within `tolerance` of `started_at`, either side. False
when it is gone, or started further away: its pid now names another process, given it
after the first ended or a reboot. None when it is alive but its start cannot be read,
so it may be either. Only a True pid may be signalled as the process that started then.
`started_at` is timezone-aware.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`bool`](https://docs.python.org/3/builtins/functions.html#bool)]

### liaise.workspace.process_started_at(pid)

When the process `pid` started, in UTC; None when that cannot be determined.

None, never an exception, for anything but a pid in range, a process that is gone, or a
system that does not say. On POSIX it is now less the elapsed time `ps -o etime=`
reports, to the second, which reads the same on macOS and Linux and involves no time
zone; `ps` is found on `PATH`, else on the system’s default path, and given
`PS_TIMEOUT_S` to answer. On Windows it is the creation time `GetProcessTimes`
reads through a query-only handle.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`datetime`](https://docs.python.org/3/library/datetime.html#datetime.datetime)]

### liaise.workspace.resolve_checkout(path)

`path` in the one form checkouts are compared in: `~` expanded, absolute, resolved.

[`liaise.holds`](liaise.holds.md#module-liaise.holds) spells `checkout:<path>` scopes with it too, so a hold set on
one spelling of a checkout applies to every other.

* **Return type:**
  [`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path)

### liaise.workspace.workspace_for(subject, , lock_dir, sessions_dir=None, own_pids=())

The shared checkout `subject` works in, or None when its file names no workspace path.

`lock_dir`, `sessions_dir` and `own_pids` go to [`SharedCheckout`](#liaise.workspace.SharedCheckout).

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`SharedCheckout`](#liaise.workspace.SharedCheckout)]
