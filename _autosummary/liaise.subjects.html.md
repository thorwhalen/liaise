# liaise.subjects

Subjects: the bodies of work liaise runs, each loaded from `subjects/<slug>.toml`.

A subject is one app or site. Its file says which conversations belong to it
(`bindings`, as correspond binding patterns), where the work happens, how it is
delivered, and its policy. The policy covers who is who (`people`), what each person
may do (`roles`, `permissions`) and at what authenticity grade (`grades`), whose
routing labels count as claims (`relays`, `claim_labels`), which brief a run reads
for each person (`briefs`), and how replies go out.

**One subject per polled conversation.** correspond keeps one listen cursor per
conversation, so two subjects polling the same one would starve each other:
[`load_subjects()`](#liaise.subjects.load_subjects) refuses that. Several bindings of one subject may share a
conversation (see [`poll_ref()`](#liaise.subjects.poll_ref)).

**An inert subject.** `active = false` declares a subject that no tick acts on. It is
loaded, validated, shown and used as gate context, but [`liaise.tick.run_once()`](liaise.tick.html.md#liaise.tick.run_once) polls
none of its bindings and starts, delivers, nudges and labels none of its cases, and
`liaise setup` refuses it. It is part of the file, not a hold, so `liaise unhold
global` does not lift it, and a reader of the file sees it.

Like [`liaise.config`](liaise.config.html.md#module-liaise.config), this module knows only the file’s shape and defaults.
Every real value lives under `~/.config/liaise/subjects/`, never in this package.
Every table has defaults, so a minimal subject file needs only:

```default
bindings = ["github:example/app?labels=partner:pat"]

[policy]
people = { "github:pat" = "pat" }
roles = { pat = "partner" }
```

### Module Attributes

| [`TAINTED_RUNS`](#liaise.subjects.TAINTED_RUNS)                  | `approve` (a run that read untrusted input needs the operator for any audience wider than them) or `send` (the subject waives that).                               |
|--------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`DFLT_DELAY_MINUTES`](#liaise.subjects.DFLT_DELAY_MINUTES)            | Minutes a `delay` verdict holds a message in the outbox before the tick sends it (`policy.delay_minutes`; liaise #38); 0 sends it at once.                         |
| [`RECOMMENDED_DELAY_MINUTES`](#liaise.subjects.RECOMMENDED_DELAY_MINUTES)     | The window a subject that turns the outbox on is advised to use (liaise ADR 0003).                                                                                 |
| [`DFLT_DELAY_STALE_MINUTES`](#liaise.subjects.DFLT_DELAY_STALE_MINUTES)      | Minutes past its release after which a held message goes to the operator instead of out (`policy.delay_stale_minutes`): nobody watched the window it relied on.    |
| [`DFLT_SUBJECTS_SUBDIR`](#liaise.subjects.DFLT_SUBJECTS_SUBDIR)          | Subject files live in this directory under the config root.                                                                                                        |
| [`REPLY_MODES`](#liaise.subjects.REPLY_MODES)                   | `direct` posts replies to the conversation; `draft` holds them for the operator.                                                                                   |
| [`DELIVERY_KINDS`](#liaise.subjects.DELIVERY_KINDS)                | `deploy` runs the delivery command; `pr_only` stops at a pull request.                                                                                             |
| [`DELIVERY_PERS`](#liaise.subjects.DELIVERY_PERS)                 | `batch` once per tick, for every case delivered in it; `issue` for each case, right after that case's outcomes.                                                    |
| [`WORKSPACE_KINDS`](#liaise.subjects.WORKSPACE_KINDS)               | Where a run works.                                                                                                                                                 |
| [`GRADES`](#liaise.subjects.GRADES)                        | Authenticity grades, weakest first, as correspond names them.                                                                                                      |
| [`REF_WILDCARDS`](#liaise.subjects.REF_WILDCARDS)                 | What makes a binding's conversation part a glob, which v0.1 cannot poll ("?" starts a binding's conditions, so it never gets that far).                            |
| [`CASE_INSENSITIVE_REF_CHANNELS`](#liaise.subjects.CASE_INSENSITIVE_REF_CHANNELS) | Channels whose conversation references ignore case, so their bindings load lower-cased (see [`normalize_binding()`](#liaise.subjects.normalize_binding)). |
| [`DFLT_WAITING_LABEL`](#liaise.subjects.DFLT_WAITING_LABEL)            | Each person's waiting label when a subject sets `policy.waiting_labels = true`.                                                                                    |

### Functions

| [`address_key`](#liaise.subjects.address_key)(address)                    | How two channel addresses compare: without regard to case.                                                                   |
|------------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------|
| [`check_bindings`](#liaise.subjects.check_bindings)(subject, \*[, registry]) | What would make one of `subject`'s bindings never match; empty when nothing would.                                           |
| [`load_subject`](#liaise.subjects.load_subject)(path)                      | Load and resolve one subject file into a [`Subject`](#liaise.subjects.Subject), its slug the file's stem. |
| [`load_subjects`](#liaise.subjects.load_subjects)([root])                   | Every subject under `<root>/subjects/*.toml`, keyed by slug, in slug order.                                                  |
| [`normalize_binding`](#liaise.subjects.normalize_binding)(binding)              | `binding` as a subject keeps it: on a GitHub channel, its channel and conversation lower-cased.                              |
| [`poll_ref`](#liaise.subjects.poll_ref)(binding)                       | The conversation `binding` is polled on, or None when v0.1 cannot poll it.                                                   |
| [`ref_key`](#liaise.subjects.ref_key)(ref)                            | How two polled conversations compare: without regard to case, as their cursors do.                                           |
| [`subject_for_ref`](#liaise.subjects.subject_for_ref)(subjects, ref)          | The subject whose bindings take in `ref`, the conversation a message outside a case goes to.                                 |

### Classes

| [`BudgetPolicy`](#liaise.subjects.BudgetPolicy)([concurrent, timeout_minutes, ...])   | Limits on a subject's runs.                                                                                                       |
|-----------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------|
| [`Delivery`](#liaise.subjects.Delivery)([kind, per, command])                     | How finished work reaches the partner.                                                                                            |
| [`Policy`](#liaise.subjects.Policy)(people, roles[, default_reply_mode, ...])   | Who is who on a subject, what each may do, and how liaise answers them.                                                           |
| [`ProcessorConfig`](#liaise.subjects.ProcessorConfig)([permission_mode])                 | How the subject's processor runs.                                                                                                 |
| [`ReadinessPolicy`](#liaise.subjects.ReadinessPolicy)([quiet_minutes, go_minutes, ...])  | When a case is ready to dispatch (see [`liaise.readiness`](liaise.readiness.html.md#module-liaise.readiness)). |
| [`Subject`](#liaise.subjects.Subject)(slug, bindings, policy[, ...])             | A resolved subject: `subjects/<slug>.toml` with every default applied.                                                            |
| [`Workspace`](#liaise.subjects.Workspace)([kind, path])                            | Where a subject's runs do their work.                                                                                             |

### *class* liaise.subjects.BudgetPolicy(concurrent=1, timeout_minutes=60, max_turns=200, daily_dispatches=6)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Limits on a subject’s runs. Mandatory, never unlimited.

### liaise.subjects.CASE_INSENSITIVE_REF_CHANNELS *= ('github',)*

Channels whose conversation references ignore case, so their bindings load lower-cased
(see [`normalize_binding()`](#liaise.subjects.normalize_binding)).

### liaise.subjects.DELIVERY_KINDS *= ('deploy', 'pr_only')*

`deploy` runs the delivery command; `pr_only` stops at a pull request.

### liaise.subjects.DELIVERY_PERS *= ('batch', 'issue')*

`batch` once per tick, for every case delivered in
it; `issue` for each case, right after that case’s outcomes.

* **Type:**
  When a `deploy` runs its command

### liaise.subjects.DFLT_DELAY_MINUTES *= None*

Minutes a `delay` verdict holds a message in the outbox before the tick sends it
(`policy.delay_minutes`; liaise #38); 0 sends it at once. Unset by default: the outbox
sends without a person, so a subject turns it on explicitly, and until then a `delay`
waits for the operator as a draft, as it did before the outbox existed.

### liaise.subjects.DFLT_DELAY_STALE_MINUTES *= 1440*

Minutes past its release after which a held message goes to the operator instead of out
(`policy.delay_stale_minutes`): nobody watched the window it relied on. 0 never lapses.

### liaise.subjects.DFLT_SUBJECTS_SUBDIR *= 'subjects'*

Subject files live in this directory under the config root.

### liaise.subjects.DFLT_WAITING_LABEL *= 'needs-{person}'*

Each person’s waiting label when a subject sets `policy.waiting_labels = true`.

### *class* liaise.subjects.Delivery(kind='deploy', per='batch', command='')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

How finished work reaches the partner.

`deploy` runs `command` in the workspace: once per tick for every case delivered
in it (`per = "batch"`), or for each case right after its outcomes
(`per = "issue"`). Nothing tells the partner it is live without a run that
succeeded. `pr_only` stops at a pull request and runs nothing.

### liaise.subjects.GRADES *= ('forged', 'claimed', 'platform', 'domain', 'bound', 'crypto')*

Authenticity grades, weakest first, as correspond names them.

### *class* liaise.subjects.Policy(people, roles, default_reply_mode='draft', reply_modes=<factory>, relays=(), claim_labels=<factory>, notify=<factory>, leak_terms=(), public_channels=('github', ), permissions=<factory>, grades=<factory>, readiness=<factory>, escalate=<factory>, budget=<factory>, deployed_nudge_days=3, briefs=<factory>, waiting_labels=<factory>, tainted_runs='approve', link_allowlist=(), canary_terms=(), mode='enforce', delay_minutes=None, delay_stale_minutes=1440)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Who is who on a subject, what each may do, and how liaise answers them.

> `people` maps a channel address (`github:pat`) to a person id and `roles` a
> person to a role. `permissions` maps a role to the permissions it grants, and
> `grades` a permission to the authenticity grades it accepts. `relays` are
> authors whose `claim_labels` (routing label to person) count as claims. See
> [`liaise.access`](liaise.access.html.md#module-liaise.access). `waiting_labels` (person to label) is the mirror of
> `claim_labels`: a label liaise writes on a case’s issues while the case waits on that
> person, where a claim label is one it reads (see [`liaise.projection`](liaise.projection.html.md#module-liaise.projection)).

> The outbound gate’s policy (liaise ADR 0002) reads four more: `tainted_runs`
> (`approve`, or `send` to waive the taint rule), `link_allowlist` (hosts a link
> may point at besides the channel’s own), `canary_terms` (terms planted in private
> context, never to be sent) and `mode` (`enforce`, or `shadow`, recorded on every
> verdict, counted by `liaise gate report`, and enforced alike until its sending
> semantics are decided, liaise #51). `delay_minutes` turns the delay

outbox on (liaise #38): how long a `delay` verdict (an irreversible send to an
organisation-wide or public place) waits, cancellable, before the tick sends it; 0 sends
it at once, and None (the default) keeps it for the operator as a draft. A held
message the tick reaches more than `delay_stale_minutes` after its release goes to the
operator as a draft instead (0: never).
`leak_terms` are scanned for as

> a label no reader is cleared for; `public_channels` is still read, and decides
> nothing: the audience correspond computes does. Both go after one release.

#### briefs *: [Mapping](https://docs.python.org/3/library/typing.html#typing.Mapping)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [str](https://docs.python.org/3/builtins/stdtypes.html#str)]*

Person id to the brief a run on their case reads (see [`Subject.brief_for()`](#liaise.subjects.Subject.brief_for)).

#### deployed_nudge_days *: [int](https://docs.python.org/3/builtins/functions.html#int)* *= 3*

Days a `deployed` case may stay quiet before the partner is nudged, once.

#### waiting_labels *: [Mapping](https://docs.python.org/3/library/typing.html#typing.Mapping)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [str](https://docs.python.org/3/builtins/stdtypes.html#str)]*

Person id to the label a case’s issues carry while the case waits on them.

### *class* liaise.subjects.ProcessorConfig(permission_mode='auto')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

How the subject’s processor runs.

### liaise.subjects.RECOMMENDED_DELAY_MINUTES *= 10*

The window a subject that turns the outbox on is advised to use (liaise ADR 0003).

### liaise.subjects.REF_WILDCARDS *= frozenset({'\*', '['})*

What makes a binding’s conversation part a glob, which v0.1 cannot poll (“?” starts
a binding’s conditions, so it never gets that far).

### liaise.subjects.REPLY_MODES *= ('direct', 'draft')*

`direct` posts replies to the conversation; `draft` holds them for the operator.

### *class* liaise.subjects.ReadinessPolicy(quiet_minutes=10, go_minutes=2, markers=<factory>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

When a case is ready to dispatch (see [`liaise.readiness`](liaise.readiness.html.md#module-liaise.readiness)).

### *class* liaise.subjects.Subject(slug, bindings, policy, display_name='', workspace=<factory>, brief='', verify='', delivery=<factory>, label_prefix='liaise:', processor=<factory>, source=None, active=True)

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

That is the first of [`notify_addresses_for()`](#liaise.subjects.Subject.notify_addresses_for), which says how `channels`
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

### liaise.subjects.TAINTED_RUNS *= ('approve', 'send')*

`approve` (a run that read untrusted input needs
the operator for any audience wider than them) or `send` (the subject waives that).

* **Type:**
  What `policy.tainted_runs` may say

### liaise.subjects.WORKSPACE_KINDS *= ('shared',)*

Where a run works. v0.1 has one checkout, shared by the subject’s runs.

### *class* liaise.subjects.Workspace(kind='shared', path='')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Where a subject’s runs do their work.

### liaise.subjects.address_key(address)

How two channel addresses compare: without regard to case.

GitHub logins are case-insensitive, so `github:Pat` is `github:pat`.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> address_key("github:Pat") == address_key("github:pat")
True
```

### liaise.subjects.check_bindings(subject, , registry=None)

What would make one of `subject`’s bindings never match; empty when nothing would.

Runs `correspond.check_binding()` on each binding, which reports a pattern with
no channel, an unknown channel, or a condition on a field that channel’s messages
never carry. `registry` is correspond’s channel registry (its default when None).
Each problem names the subject’s file and the binding.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.subjects.load_subject(path)

Load and resolve one subject file into a [`Subject`](#liaise.subjects.Subject), its slug the file’s stem.

Each binding is kept as [`normalize_binding()`](#liaise.subjects.normalize_binding) gives it. Raises
[`ConfigError`](liaise.config.html.md#liaise.config.ConfigError) naming the file and the fix for a missing file,
invalid TOML, a missing `bindings`, `policy.people` or `policy.roles`, an
`active` that is not true or false, and any value outside its vocabulary (workspace
and delivery kinds, `delivery.per`, reply modes, permissions, grades, roles).

* **Return type:**
  [`Subject`](#liaise.subjects.Subject)

### liaise.subjects.load_subjects(root=None)

Every subject under `<root>/subjects/*.toml`, keyed by slug, in slug order.

`root` defaults to `~/.config/liaise`. No `subjects` directory means no subjects.
Raises [`ConfigError`](liaise.config.html.md#liaise.config.ConfigError) for a file that does not load, and, naming
both files, for two subjects whose bindings are polled on the same conversation.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Subject`](#liaise.subjects.Subject)]

### liaise.subjects.normalize_binding(binding)

`binding` as a subject keeps it: on a GitHub channel, its channel and conversation lower-cased.

GitHub references ignore case, but correspond’s `binding_matches` compares a binding’s
conversation case-sensitively with the lower-cased reference its GitHub adapter gives a
message, so `github:Example/App` would match nothing. The `?conditions` stay as
written, and a binding on any other channel is returned unchanged.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> normalize_binding("github:Example/App?labels=partner:Pat")
'github:example/app?labels=partner:Pat'
>>> normalize_binding("webinbox:Example-Site")
'webinbox:Example-Site'
```

### liaise.subjects.poll_ref(binding)

The conversation `binding` is polled on, or None when v0.1 cannot poll it.

That is the binding without its `?conditions`. A conversation part holding a
wildcard is a glob, not a conversation, so it gives None.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

```pycon
>>> poll_ref("github:example/app?labels=partner:pat")
'github:example/app'
>>> poll_ref("github:example/*") is None
True
```

### liaise.subjects.ref_key(ref)

How two polled conversations compare: without regard to case, as their cursors do.

correspond keys a cursor on the reference its adapter normalizes. GitHub’s lower-cases
it, and a web inbox’s site names are lower-case, so `github:Example/App` is polled
on the cursor of `github:example/app`.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> ref_key("github:Example/App") == ref_key("github:example/app")
True
```

### liaise.subjects.subject_for_ref(subjects, ref)

The subject whose bindings take in `ref`, the conversation a message outside a case goes to.

A binding takes in the conversation it polls and, on GitHub, every issue of a repository
it binds, so `github:example/app#12` is the subject’s that binds
`github:example/app`. Conversations compare as [`ref_key()`](#liaise.subjects.ref_key) says. A binding’s
`?conditions` are not consulted, since they sort what comes in, not where a message
may go, and a wildcard binding polls nothing, so it takes in nothing (see
[`poll_ref()`](#liaise.subjects.poll_ref)). When two subjects take `ref` in, the one whose binding names it most
closely wins: an issue’s own binding over its repository’s.

No caller may choose another subject: the subject’s policy is what the gate judges a
message by, so a message goes only where its subject binds.

* **Return type:**
  [`Subject`](#liaise.subjects.Subject)

```pycon
>>> heron = Subject("heron", ("github:example/heron?labels=partner:pat",),
...     Policy(people={}, roles={}))
>>> subject_for_ref({"heron": heron}, "github:Example/Heron#12").slug
'heron'
```

Raises `ValueError` naming the subjects there are when none takes `ref` in, and
naming each when several name it equally closely.
