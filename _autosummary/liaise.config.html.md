# liaise.config

The global config, and the 0.0.x partner files `liaise migrate-config` reads.

[`load_global_config()`](#liaise.config.load_global_config) reads `~/.config/liaise/config.toml`: the owner, the state
directory and notifications. A 0.1 subject is a `subjects/<slug>.toml` file (see
[`liaise.subjects`](liaise.subjects.html.md#module-liaise.subjects)). [`load_config()`](#liaise.config.load_config) also reads the 0.0.x `partners/*.toml`,
which only [`liaise.migrate`](liaise.migrate.html.md#module-liaise.migrate) still uses. The defaults here are the ones both formats
share.

Everything partner-specific (identities, repos, briefs, commands, hosts) lives under
`~/.config/liaise/`, never in this package’s code, tests, docs, fixtures or examples.
This module only knows the *shape* of that configuration and how to resolve it into
frozen dataclasses with defaults applied; it never hardcodes a real partner.

### Module Attributes

| [`DFLT_DISPATCH_COMMAND`](#liaise.config.DFLT_DISPATCH_COMMAND)   | `-p`/`--print` takes the prompt as its next argument, not a file to open on its own — a bare path here IS the prompt (an agent seeing only a path has no instruction to read it), so the templates spell that out explicitly.   |
|--------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

### Functions

| [`load_config`](#liaise.config.load_config)([root])        | Load a 0.0.x configuration, the global config and every partner, as frozen dataclasses.                           |
|-----------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------|
| [`load_global_config`](#liaise.config.load_global_config)([root]) | Load `<root>/config.toml` into a [`GlobalConfig`](#liaise.config.GlobalConfig), defaults applied. |

### Classes

| [`Budget`](#liaise.config.Budget)([timeout_minutes, max_turns, ...])     | Per-dispatch and per-day limits.                                                                                         |
|------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------|
| [`Config`](#liaise.config.Config)(global_, partners)                     | Everything [`load_config()`](#liaise.config.load_config) resolved: the global config and every partner. |
| [`DispatchConfig`](#liaise.config.DispatchConfig)([command, cwd, ...])           | Command templates used to run (and resume) the coding agent.                                                             |
| [`EscalateConfig`](#liaise.config.EscalateConfig)([money_usd, max_scope])        | Thresholds beyond which the agent must route a decision to the owner.                                                    |
| [`GlobalConfig`](#liaise.config.GlobalConfig)(owner_login, state_dir[, ...])   | `~/.config/liaise/config.toml`: the owner, the state directory, and notifications.                                       |
| [`Markers`](#liaise.config.Markers)([go, wait])                           | Literal, case-insensitive substrings a partner can drop in a comment.                                                    |
| [`NotifyConfig`](#liaise.config.NotifyConfig)([ntfy_topic_env])                | Where to send owner notifications.                                                                                       |
| [`PartnerConfig`](#liaise.config.PartnerConfig)(slug, display_name, ...[, ...]) | A resolved partner: `~/.config/liaise/partners/<slug>.toml` plus defaults.                                               |

### Exceptions

| [`ConfigError`](#liaise.config.ConfigError)   | Raised when configuration is missing or malformed.   |
|----------------------------------------------------------------|------------------------------------------------------|

### *class* liaise.config.Budget(timeout_minutes=60, max_turns=200, daily_dispatches=6)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Per-dispatch and per-day limits. Mandatory, never unlimited.

### *class* liaise.config.Config(global_, partners)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Everything [`load_config()`](#liaise.config.load_config) resolved: the global config and every partner.

#### partner(slug)

Return the resolved partner config for `slug`, or raise [`ConfigError`](#liaise.config.ConfigError).

* **Return type:**
  [`PartnerConfig`](#liaise.config.PartnerConfig)

### *exception* liaise.config.ConfigError

Bases: [`Exception`](https://docs.python.org/3/builtins/exceptions.html#Exception)

Raised when configuration is missing or malformed.

Always names the path involved and, for a missing file, the minimal content
that would fix it — an agent (or a human) reading the error should not have
to go spelunking in the design docs to recover.

### liaise.config.DFLT_DISPATCH_COMMAND *= 'claude -p "Read and follow the instructions in {prompt_file}" --permission-mode {permission_mode} --output-format json'*

`-p`/`--print` takes the prompt as its next argument, not a file to open on
its own — a bare path here IS the prompt (an agent seeing only a path has no
instruction to read it), so the templates spell that out explicitly.
Both templates take `{permission_mode}` from the one `dispatch.permission_mode`
setting (#22): the resume template used to omit it, so a resumed dispatch ran
under a different permission mode than the run it continued.

### *class* liaise.config.DispatchConfig(command='claude -p "Read and follow the instructions in {prompt_file}" --permission-mode {permission_mode} --output-format json', cwd='.', resume_command='claude --resume {session_id} -p "Read and follow the instructions in {prompt_file}" --permission-mode {permission_mode} --output-format json', permission_mode='auto')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Command templates used to run (and resume) the coding agent.

Both templates are formatted with `prompt_file`, `session_id` and
`permission_mode` — the last from this one field, so a fresh and a
resumed dispatch run under the same permission mode. For an overridden
`command` that hardcodes its mode instead, the loader resumes under that
mode (see `_dispatch_from`).

### *class* liaise.config.EscalateConfig(money_usd=50.0, max_scope='about a day of work')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Thresholds beyond which the agent must route a decision to the owner.

### *class* liaise.config.GlobalConfig(owner_login, state_dir, notify=<factory>, quiet_minutes=10, go_minutes=2, markers=<factory>, label_prefix='liaise:', budget=<factory>, deploy_per='batch', deployed_nudge_days=3)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

`~/.config/liaise/config.toml`: the owner, the state directory, and notifications.

The other fields are the 0.0.x defaults a partner file inherits, which only
[`liaise.migrate`](liaise.migrate.html.md#module-liaise.migrate) still reads. A 0.1 subject file has defaults of its own.

### *class* liaise.config.Markers(go='#startwork#', wait='#wait#')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Literal, case-insensitive substrings a partner can drop in a comment.

### *class* liaise.config.NotifyConfig(ntfy_topic_env='LIAISE_NTFY_TOPIC')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Where to send owner notifications. See [`liaise.notify`](liaise.html.md#liaise.notify).

### *class* liaise.config.PartnerConfig(slug, display_name, github_logins, repo, brief, label, notify_login, reply_mode='draft', dispatch=<factory>, verify='', deploy='', escalate=<factory>, quiet_minutes=10, go_minutes=2, markers=<factory>, label_prefix='liaise:', budget=<factory>, deploy_per='batch', deployed_nudge_days=3)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A resolved partner: `~/.config/liaise/partners/<slug>.toml` plus defaults.

#### notify_login *: [str](https://docs.python.org/3/builtins/stdtypes.html#str)*

The GitHub login `@mentioned` at the start of every partner-facing
comment — an issue filed through the app is authored by the app’s own
credentials, so GitHub sends the partner no email otherwise (#20).
Computed by the loader (default: the first `github_logins` entry); a
partner identified by label only (no `github_logins`) must set it
explicitly.

### liaise.config.load_config(root=None)

Load a 0.0.x configuration, the global config and every partner, as frozen dataclasses.

Only [`liaise.migrate`](liaise.migrate.html.md#module-liaise.migrate) still reads partner files. 0.1 reads the global config alone
([`load_global_config()`](#liaise.config.load_global_config)) and its subjects ([`liaise.subjects.load_subjects()`](liaise.subjects.html.md#liaise.subjects.load_subjects)).

`root` defaults to `~/.config/liaise`. A missing global config raises
[`ConfigError`](#liaise.config.ConfigError) naming the path and the minimal content needed. Every
partner under `partners/*.toml` is resolved with the global defaults
applied, then overridden by whatever the partner file sets explicitly.

* **Return type:**
  [`Config`](#liaise.config.Config)

### liaise.config.load_global_config(root=None)

Load `<root>/config.toml` into a [`GlobalConfig`](#liaise.config.GlobalConfig), defaults applied.

`root` defaults to `~/.config/liaise`. A missing file raises [`ConfigError`](#liaise.config.ConfigError)
naming the path and the minimal content it needs, as does a missing `owner_login`
or `state_dir`. Nothing under `partners/` is read (see [`load_config()`](#liaise.config.load_config)).

* **Return type:**
  [`GlobalConfig`](#liaise.config.GlobalConfig)
