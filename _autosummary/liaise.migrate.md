# liaise.migrate

Derive 0.1 subject files from a 0.0.x configuration: `liaise migrate-config`.

0.0.x configured one partner per file (`partners/<slug>.toml`, plus the shared
`config.toml`). 0.1 configures one subject per body of work (`subjects/<slug>.toml`,
see [`liaise.subjects`](liaise.subjects.md#module-liaise.subjects)). [`migrate_config()`](#liaise.migrate.migrate_config) groups the partners by repo, one
subject per repo, maps every field, and returns a [`MigrationPlan`](#liaise.migrate.MigrationPlan): the TOML each
subject file would hold, the files it came from, and what the operator should look at.

It writes nothing unless `apply=True`, and then only creates missing subject files: it
never overwrites one, and never touches `config.toml`, `partners/` or `briefs/`.

- **Chosen, not defaulted.** A value the operator set (in a partner file or in
  `config.toml`) is carried. A value 0.0.x only defaulted is left to 0.1’s default,
  which is the same constant.
- **Partners sharing a repo must agree** on the subject’s settings (verify, readiness,
  delivery, workspace…). The first partner’s value is kept, and every other value is
  listed as a conflict. Budgets keep the strictest value instead.
- **Briefs stay per person.** Each partner’s brief goes to `policy.briefs`, and the
  subject’s own `brief` is set only when every partner has the same one.
- **Bindings match by label only.** 0.0.x also took any issue a partner’s login opened,
  which claimed issues never meant for liaise. In 0.1, matching a login is opt-in, and a
  note on the subject names the binding that opts in.
- **Custom command templates are not carried**: 0.1 builds the `claude` command itself.

### Module Attributes

| [`BATCH_DELIVERY_PER`](#liaise.migrate.BATCH_DELIVERY_PER)   | 0.0.x's `batch` stays `batch`, and 0.0.x deployed each issue on its own for any other `deploy_per`, which 0.1 calls `issue`.   |
|-----------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------|
| [`ISSUE_DELIVERY_PER`](#liaise.migrate.ISSUE_DELIVERY_PER)   | 0.0.x's `batch` stays `batch`, and 0.0.x deployed each issue on its own for any other `deploy_per`, which 0.1 calls `issue`.   |
| [`GLOBAL_CONFIG_FILE`](#liaise.migrate.GLOBAL_CONFIG_FILE)   | The 0.0.x global config file, under the config root.                                                                           |
| [`PARTNERS_SUBDIR`](#liaise.migrate.PARTNERS_SUBDIR)      | The 0.0.x partner files' directory, under the config root.                                                                     |
| [`GITHUB_CHANNEL`](#liaise.migrate.GITHUB_CHANNEL)       | The channel every 0.0.x partner was on.                                                                                        |
| [`PARTNER_ROLE`](#liaise.migrate.PARTNER_ROLE)         | The role every migrated partner gets on its subject.                                                                           |
| [`POSTING_REPLY_MODE`](#liaise.migrate.POSTING_REPLY_MODE)   | 0.0.x posted directly for any `reply_mode` but draft; 0.1 names that mode.                                                     |
| [`BUDGET_LIMITS`](#liaise.migrate.BUDGET_LIMITS)        | The budget limits carried, each keeping the strictest partner's value.                                                         |

### Functions

| [`apply_plan`](#liaise.migrate.apply_plan)(plan)                        | Write each planned subject file that does not exist yet, and return `plan` as applied.                                           |
|------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------|
| [`dumps_toml`](#liaise.migrate.dumps_toml)(doc)                         | `doc` as TOML text, which [`tomllib.loads()`](https://docs.python.org/3/library/tomllib.html#tomllib.loads) reads back as `doc`. |
| [`migrate_config`](#liaise.migrate.migrate_config)([root, apply, registry]) | Plan the 0.1 subject files for the 0.0.x config under `root`; write them if `apply`.                                             |
| [`plan_migration`](#liaise.migrate.plan_migration)([root, registry])        | The dry-run [`MigrationPlan`](#liaise.migrate.MigrationPlan) for the 0.0.x config under `root`, writing nothing.   |

### Classes

| [`InlineTable`](#liaise.migrate.InlineTable)                                       | A table [`dumps_toml()`](#liaise.migrate.dumps_toml) writes on one line, as `k = { a = 1 }`, not under a header.   |
|----------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------|
| [`MigrationPlan`](#liaise.migrate.MigrationPlan)(subjects[, warnings, applied, ...]) | What `liaise migrate-config` would write, and what it wrote once applied.                                                           |
| [`PlannedSubject`](#liaise.migrate.PlannedSubject)(slug, path, toml_text, sources)    | One subject file [`migrate_config()`](#liaise.migrate.migrate_config) would write, and what went into it.              |

### liaise.migrate.BATCH_DELIVERY_PER *= 'batch'*

0.0.x’s `batch` stays `batch`, and 0.0.x deployed each
issue on its own for any other `deploy_per`, which 0.1 calls `issue`.

* **Type:**
  A 0.1 delivery’s `per`

### liaise.migrate.BUDGET_LIMITS *= ('timeout_minutes', 'max_turns', 'daily_dispatches')*

The budget limits carried, each keeping the strictest partner’s value.

### liaise.migrate.GITHUB_CHANNEL *= 'github'*

The channel every 0.0.x partner was on.

### liaise.migrate.GLOBAL_CONFIG_FILE *= 'config.toml'*

The 0.0.x global config file, under the config root.

### liaise.migrate.ISSUE_DELIVERY_PER *= 'issue'*

0.0.x’s `batch` stays `batch`, and 0.0.x deployed each
issue on its own for any other `deploy_per`, which 0.1 calls `issue`.

* **Type:**
  A 0.1 delivery’s `per`

### *class* liaise.migrate.InlineTable

Bases: [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

A table [`dumps_toml()`](#liaise.migrate.dumps_toml) writes on one line, as `k = { a = 1 }`, not under a header.

### *class* liaise.migrate.MigrationPlan(subjects, warnings=(), applied=False, written=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What `liaise migrate-config` would write, and what it wrote once applied.

#### lines()

The printable plan, subject by subject, ending with what was written.

For each subject: its target path, its sources, the TOML, then its conflicts,
warnings, binding problems and notes. Then the plan’s own warnings, then
`nothing written (dry run)` or `wrote N subject file(s)`.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.migrate.PARTNERS_SUBDIR *= 'partners'*

The 0.0.x partner files’ directory, under the config root.

### liaise.migrate.PARTNER_ROLE *= 'partner'*

The role every migrated partner gets on its subject.

### liaise.migrate.POSTING_REPLY_MODE *= 'direct'*

0.0.x posted directly for any `reply_mode` but draft; 0.1 names that mode.

### *class* liaise.migrate.PlannedSubject(slug, path, toml_text, sources, conflicts=(), warnings=(), binding_problems=(), notes=())

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One subject file [`migrate_config()`](#liaise.migrate.migrate_config) would write, and what went into it.

#### binding_problems *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), ...]* *= ()*

What `correspond.check_binding()` says would keep a binding from matching.

#### conflicts *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), ...]* *= ()*

Where the subject’s partners disagreed, and which value was kept.

#### notes *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), ...]* *= ()*

What 0.1 does differently from 0.0.x for this subject, and how to change that.

#### sources *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Path](https://docs.python.org/3/library/pathlib.html#pathlib.Path), ...]*

The 0.0.x files its values came from.

#### warnings *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), ...]* *= ()*

What the operator should check before relying on the file.

### liaise.migrate.apply_plan(plan)

Write each planned subject file that does not exist yet, and return `plan` as applied.

An existing file is never overwritten: its subject gets a warning instead. Each file
written is loaded back with [`load_subject()`](liaise.subjects.md#liaise.subjects.load_subject), and one that does
not load gets a warning naming the error. Nothing outside `subjects/` is touched.

* **Return type:**
  [`MigrationPlan`](#liaise.migrate.MigrationPlan)

### liaise.migrate.dumps_toml(doc)

`doc` as TOML text, which [`tomllib.loads()`](https://docs.python.org/3/library/tomllib.html#tomllib.loads) reads back as `doc`.

Writes strings (escaped), ints, floats, bools, arrays and tables. A nested mapping
becomes a `[section]` (dotted, as in `[policy.readiness]`), unless it is an
[`InlineTable`](#liaise.migrate.InlineTable) or sits in an array, which are written inline. A key that is not
bare, such as `github:pat`, is quoted. `None` values are left out, and so is a
section with nothing left in it.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> print(dumps_toml({
...     "bindings": ["github:example/app"],
...     "policy": {"people": InlineTable({"github:pat": "pat"}), "note": None},
... }), end="")
bindings = ["github:example/app"]

[policy]
people = { "github:pat" = "pat" }
```

### liaise.migrate.migrate_config(root=None, , apply=False, registry=None)

Plan the 0.1 subject files for the 0.0.x config under `root`; write them if `apply`.

`root` defaults to `~/.config/liaise`. A dry run (the default) writes nothing.
`apply=True` creates `subjects/<slug>.toml` for each subject whose file does not
exist yet (see [`apply_plan()`](#liaise.migrate.apply_plan)). `registry` is correspond’s channel registry,
used only to check bindings (its default when None; that check runs no adapter).
Raises [`ConfigError`](liaise.config.md#liaise.config.ConfigError) when the 0.0.x config does not load.

* **Return type:**
  [`MigrationPlan`](#liaise.migrate.MigrationPlan)

### liaise.migrate.plan_migration(root=None, , registry=None)

The dry-run [`MigrationPlan`](#liaise.migrate.MigrationPlan) for the 0.0.x config under `root`, writing nothing.

One subject per repo (compared case-insensitively, as GitHub does), in slug order.

* **Return type:**
  [`MigrationPlan`](#liaise.migrate.MigrationPlan)
