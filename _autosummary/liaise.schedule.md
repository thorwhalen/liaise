# liaise.schedule

Scheduling `liaise run --once` (A.7): a launchd agent on macOS, a systemd
user timer on Linux.

launchd and systemd both hand a job a nearly-empty environment, so the
installed job needs an environment **snapshot** taken from the installing
shell — `PATH` (so it can find `gh` and the dispatch command, e.g. `claude`),
plus the ntfy topic variable and anything else a partner’s dispatch command
needs. Re-run install after moving any of those.

### Module Attributes

| [`LAUNCHD_DETACH_SETTING`](#liaise.schedule.LAUNCHD_DETACH_SETTING)   | What a job file carries so the detached runs a tick starts outlive that tick.                                |
|---------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------|
| [`OUTDATED_SCHEDULE_NOTE`](#liaise.schedule.OUTDATED_SCHEDULE_NOTE)   | How [`schedule_status()`](#liaise.schedule.schedule_status) says the installed job is such a job. |

### Functions

| [`install_schedule`](#liaise.schedule.install_schedule)(\*[, root, ...])                  | Install (or replace) the scheduled job.                                    |
|-----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------|
| [`job_environment`](#liaise.schedule.job_environment)(\*[, extra_env_vars, ...])         | The environment to pin into the installed job.                             |
| [`schedule_status`](#liaise.schedule.schedule_status)(\*[, system, launchd_dir, ...])    | Whether the scheduled job is installed, where, and whether it is outdated. |
| [`uninstall_schedule`](#liaise.schedule.uninstall_schedule)(\*[, system, launchd_dir, ...]) | Remove the scheduled job.                                                  |

### liaise.schedule.LAUNCHD_DETACH_SETTING *= re.compile('<key>AbandonProcessGroup</key>\\\\s\*<true/>')*

What a job file carries so the detached runs a tick starts outlive that tick. A job
installed by 0.0.x has neither, and must be installed again.

### liaise.schedule.OUTDATED_SCHEDULE_NOTE *= 'outdated: re-run liaise schedule install'*

How [`schedule_status()`](#liaise.schedule.schedule_status) says the installed job is such a job.

### liaise.schedule.install_schedule(, root=None, interval_minutes=2, system=None, launchd_dir=None, launchd_log_dir=None, systemd_dir=None, label='com.liaise.run', unit='liaise-run', load=True, ntfy_topic_env='LIAISE_NTFY_TOPIC', extra_env_vars=())

Install (or replace) the scheduled job. Returns the path(s) written.

`system` overrides `platform.system()` (a seam for tests); `launchd_dir`
/ `launchd_log_dir` / `systemd_dir` override the real locations under
`$HOME` (also for tests); `load=False` writes the files without calling
`launchctl`/`systemctl` — the actual scheduler state is left untouched.
`ntfy_topic_env` should be this installation’s actual configured
`notify.ntfy_topic_env` (M-4) — a custom variable name that never reaches
here means every scheduled-run notification silently disappears.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.schedule.job_environment(, extra_env_vars=(), ntfy_topic_env='LIAISE_NTFY_TOPIC')

The environment to pin into the installed job.

`PATH` carries every directory a dispatch command is likely to need `gh`
or `claude` from, plus the standard system locations. `HOME` is included
because both schedulers otherwise omit it. The ntfy topic variable and any
`extra_env_vars` are copied over from *this* shell if set — silently
omitted otherwise, since a job that can’t notify still runs.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.schedule.schedule_status(, system=None, launchd_dir=None, systemd_dir=None, label='com.liaise.run', unit='liaise-run')

Whether the scheduled job is installed, where, and whether it is outdated.

`installed (outdated: re-run liaise schedule install)` for a job that would kill the
detached runs its tick starts: a plist without `AbandonProcessGroup`, or a service
unit without `KillMode=process`, as a job installed by 0.0.x is. The files are only
read; the scheduler itself is never asked.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.schedule.uninstall_schedule(, system=None, launchd_dir=None, systemd_dir=None, label='com.liaise.run', unit='liaise-run', load=True)

Remove the scheduled job. Idempotent — a no-op if nothing was installed.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)
