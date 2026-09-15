# liaise.holds

Holds: stops on work, by scope, set by the operator or by the tick itself.

A hold sits on one scope and stops the work that falls under it. The scopes:

```default
global              everything
processor           the processor, for every subject
effect:<kind>       one kind of effect of finished work, such as effect:deploy
subject:<slug>      one subject
person:<id>         one person's cases
repo:<owner/repo>   one repository
checkout:<path>     one checkout, in any spelling (compared resolved)
```

Its mode says what it stops (see [`BLOCKING_MODES`](#liaise.holds.BLOCKING_MODES)):

- `block`: no new starts. Running runs continue and are collected, and the effects of
  their outcomes wait on the case until unhold.
- `drain`: no new starts. In-flight work finishes, and its effects execute.
- `cancel`: no new starts. Running runs are cancelled gracefully, so they can resume,
  and effects wait.

Operator notifications always go out, whatever the mode.

The tick asks [`blocking_hold()`](#liaise.holds.blocking_hold) before starting a case (before budget and preflight),
before executing a finished run’s effects, and about each running run. When several
holds apply it gets the most specific one, the narrowest reason there is.

The holds the tick sets itself (`processor` on `config_error` or `auth_expired`,
`effect:deploy` on `effect_blocked`) come from [`auto_hold()`](#liaise.holds.auto_hold), recorded with
`set_by="auto:<error class>"`, which also says whether it placed a new hold.
[`release_auto_holds()`](#liaise.holds.release_auto_holds) lifts those, and never an operator’s hold.

Everything goes through a [`Ledger`](liaise.ledger.html.md#liaise.ledger.Ledger), so a dry-run ledger
(`Ledger(ChainMap({}, store))`) holds and lifts without touching the real one.

### Module Attributes

| [`SCOPE_KINDS`](#liaise.holds.SCOPE_KINDS)        | Every kind of scope.                                                                                                     |
|---------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------|
| [`BARE_SCOPE_KINDS`](#liaise.holds.BARE_SCOPE_KINDS)   | The scope kinds that take no value.                                                                                      |
| [`SCOPE_SPECIFICITY`](#liaise.holds.SCOPE_SPECIFICITY)  | the order [`blocking_hold()`](#liaise.holds.blocking_hold) prefers.                                      |
| [`BLOCKING_MODES`](#liaise.holds.BLOCKING_MODES)     | any hold stops a start, `drain` lets the effects of in-flight work execute, and only `cancel` stops a run already going. |
| [`DFLT_SET_BY`](#liaise.holds.DFLT_SET_BY)        | Who [`hold()`](#liaise.holds.hold) records as having set a hold, unless told otherwise.         |
| [`AUTO_SET_BY_PREFIX`](#liaise.holds.AUTO_SET_BY_PREFIX) | `auto:<error class>`.                                                                                                    |

### Functions

| [`auto_hold`](#liaise.holds.auto_hold)(ledger, scope, \*, error_class[, now])   | Block `scope` because the tick met `error_class`: the hold in force, and whether it is new.                                                                        |
|-----------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`blocking_hold`](#liaise.holds.blocking_hold)(ledger, scopes, \*, for_)            | The most specific hold on one of `scopes` that stops `for_`, or None.                                                                                              |
| [`canonical_scope`](#liaise.holds.canonical_scope)(scope)                             | `scope` as holds are stored and matched: validated, a checkout path resolved, and a repository lower-cased, since GitHub's names ignore case as its references do. |
| [`hold`](#liaise.holds.hold)(ledger, scope, \*[, mode, reason, ...])       | Put a `mode` hold on `scope`, replacing any hold already there, and return it.                                                                                     |
| [`parse_scope`](#liaise.holds.parse_scope)(scope)                                 | `(kind, value)` for a hold scope; the value is None for `global` and `processor`.                                                                                  |
| [`release_auto_holds`](#liaise.holds.release_auto_holds)(ledger, scope)                  | Lift the tick's own hold on `scope` and return what was lifted; an operator's stays.                                                                               |
| [`scopes_for`](#liaise.holds.scopes_for)(\*[, subject, person, repo, ...])       | The scopes a piece of work falls under, most specific first and `global` last.                                                                                     |
| [`unhold`](#liaise.holds.unhold)(ledger, scope)                              | Lift the hold on `scope`, whoever set it; True when there was one.                                                                                                 |

### Classes

| [`AutoHold`](#liaise.holds.AutoHold)(hold, created)   | What [`auto_hold()`](#liaise.holds.auto_hold) did: the hold now in force on the scope, and whether it is new.   |
|----------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------|

### liaise.holds.AUTO_SET_BY_PREFIX *= 'auto:'*

`auto:<error class>`.

* **Type:**
  How `set_by` starts on a hold the tick set itself

### *class* liaise.holds.AutoHold(hold, created)

Bases: [`NamedTuple`](https://docs.python.org/3/library/typing.html#typing.NamedTuple)

What [`auto_hold()`](#liaise.holds.auto_hold) did: the hold now in force on the scope, and whether it is new.

#### created *: [bool](https://docs.python.org/3/builtins/functions.html#bool)*

True when the scope had no hold before. False when an earlier automatic hold was
replaced, or an operator’s hold was kept. The tick tells the operator only when True.

#### hold *: [Hold](liaise.model.html.md#liaise.model.Hold)*

Alias for field number 0

### liaise.holds.BARE_SCOPE_KINDS *= ('global', 'processor')*

The scope kinds that take no value.

### liaise.holds.BLOCKING_MODES *= mappingproxy({'start': ('block', 'drain', 'cancel'), 'effect': ('block', 'cancel'), 'running': ('cancel',)})*

any hold stops
a start, `drain` lets the effects of in-flight work execute, and only `cancel`
stops a run already going.

* **Type:**
  For each check [`blocking_hold()`](#liaise.holds.blocking_hold) makes, the hold modes that stop it

### liaise.holds.DFLT_SET_BY *= 'operator'*

Who [`hold()`](#liaise.holds.hold) records as having set a hold, unless told otherwise.

### liaise.holds.SCOPE_KINDS *= ('global', 'subject', 'person', 'repo', 'checkout', 'processor', 'effect')*

Every kind of scope. `global` and `processor` stand alone; the rest are `kind:value`.

### liaise.holds.SCOPE_SPECIFICITY *= ('checkout', 'repo', 'person', 'subject', 'effect', 'processor', 'global')*

the order [`blocking_hold()`](#liaise.holds.blocking_hold) prefers.

* **Type:**
  Scope kinds from the most specific to the broadest

### liaise.holds.auto_hold(ledger, scope, , error_class, now=None)

Block `scope` because the tick met `error_class`: the hold in force, and whether it is new.

The hold is recorded with `set_by="auto:<error_class>"`, so
[`release_auto_holds()`](#liaise.holds.release_auto_holds) can lift it, and it replaces an earlier automatic hold. An
operator’s hold already on the scope is kept, unchanged, and returned instead:
replacing it would let a later release lift the operator’s hold.

* **Return type:**
  [`AutoHold`](#liaise.holds.AutoHold)

### liaise.holds.blocking_hold(ledger, scopes, , for_)

The most specific hold on one of `scopes` that stops `for_`, or None.

`for_` is the check, one of [`BLOCKING_MODES`](#liaise.holds.BLOCKING_MODES):

- `"start"`: may a new run start? Any hold stops it.
- `"effect"`: may a finished run’s effects execute? `block` and `cancel` keep
  them waiting, while `drain` lets them go.
- `"running"`: must a running run be cancelled? Only `cancel` says so.

Build `scopes` with [`scopes_for()`](#liaise.holds.scopes_for). Raises `ValueError` for an unknown check or
a scope outside the accepted forms.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`Hold`](liaise.model.html.md#liaise.model.Hold)]

### liaise.holds.canonical_scope(scope)

`scope` as holds are stored and matched: validated, a checkout path resolved, and a
repository lower-cased, since GitHub’s names ignore case as its references do.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> canonical_scope("repo:Example/App")
'repo:example/app'
```

Raises `ValueError` as [`parse_scope()`](#liaise.holds.parse_scope) does.

### liaise.holds.hold(ledger, scope, , mode='block', reason='', set_by='operator', now=None)

Put a `mode` hold on `scope`, replacing any hold already there, and return it.

Raises `ValueError`, writing nothing, for a scope outside the accepted forms or a
mode outside `HOLD_MODES`.

* **Return type:**
  [`Hold`](liaise.model.html.md#liaise.model.Hold)

### liaise.holds.parse_scope(scope)

`(kind, value)` for a hold scope; the value is None for `global` and `processor`.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]]

```pycon
>>> parse_scope("effect:deploy")
('effect', 'deploy')
>>> parse_scope("global")
('global', None)
```

Only the first `:` separates, so a value may hold more (a Windows checkout path).
Raises `ValueError` naming the accepted forms for anything else.

### liaise.holds.release_auto_holds(ledger, scope)

Lift the tick’s own hold on `scope` and return what was lifted; an operator’s stays.

For the tick to call once the cause has cleared, as when preflight passes again after
`auth_expired`.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Hold`](liaise.model.html.md#liaise.model.Hold)]

### liaise.holds.scopes_for(, subject=None, person=None, repo=None, checkout=None, effect=None, processor=True)

The scopes a piece of work falls under, most specific first and `global` last.

One scope per argument given, plus `processor` unless `processor=False` (for an
effect the processor has no part in), plus `global`.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

```pycon
>>> scopes_for(person="pat", effect="deploy")
('person:pat', 'effect:deploy', 'processor', 'global')
```

Raises `ValueError` for an empty value.

### liaise.holds.unhold(ledger, scope)

Lift the hold on `scope`, whoever set it; True when there was one.

Raises `ValueError` for a scope outside the accepted forms.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)
