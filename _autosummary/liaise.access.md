# liaise.access

Identity and access: who a message is from, and whether they may do what it asks.

**Resolving a sender.** [`resolve_person()`](#liaise.access.resolve_person) maps a channel address (`github:pat`)
to a person id. It looks in the subject’s `policy.people` first, then in acquaint’s
people records when acquaint is installed (`liaise[people]`). Any failure there
resolves to nobody; it never raises.

**Label-as-claim.** A routing label is only a claim, since anyone who can label an
issue can write one. [`authorize()`](#liaise.access.authorize) attributes a message:

1. to its author, by handle, when the author resolves to a person with a role on the
   subject, whatever labels the message carries;
2. otherwise to the person a `policy.claim_labels` label names, but only when the
   author is one of `policy.relays` (an app filing issues for its users), and at
   the relay’s own authenticity grade;
3. otherwise to no one. A claim label from any other author is refused as
   `label claim by an untrusted author`.

The attributed person’s role must grant the permission, at a grade `policy.grades`
accepts for it.

**Addresses compare without regard to case** (see [`liaise.subjects.address_key()`](liaise.subjects.md#liaise.subjects.address_key)),
in `policy.people` and `policy.relays` alike: GitHub logins are case-insensitive, so
`github:Pat` is the person `github:pat` is.

### Module Attributes

| [`UNTRUSTED_CLAIM`](#liaise.access.UNTRUSTED_CLAIM)   | Why [`authorize()`](#liaise.access.authorize) refused a message.           |
|--------------------------------------------------------------------|-----------------------------------------------------------------------------------------------|
| [`ALLOWED`](#liaise.access.ALLOWED)           | The reason of a decision that allows.                                                         |
| [`VIA_HANDLE`](#liaise.access.VIA_HANDLE)        | How an [`AccessDecision`](#liaise.access.AccessDecision) attributed its person. |
| [`Resolver`](#liaise.access.Resolver)          | the identity-resolver seam.                                                                   |

### Functions

| [`authorize`](#liaise.access.authorize)(message, subject, permission, \*[, ...])   | Whether `message` may exercise `permission` on `subject`, by the label-as-claim rule.     |
|-------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| [`is_relay`](#liaise.access.is_relay)(address, subject)                           | Whether `address` is one of `subject`'s `policy.relays`, compared without regard to case. |
| [`resolve_person`](#liaise.access.resolve_person)(address, subject)                     | The person id `address` belongs to on `subject`, or None.                                 |

### Classes

| [`AccessDecision`](#liaise.access.AccessDecision)(person, role, grade, ...)   | What [`authorize()`](#liaise.access.authorize) decided about one message, and why.   |
|---------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------|

### liaise.access.ALLOWED *= 'allowed'*

The reason of a decision that allows.

### *class* liaise.access.AccessDecision(person, role, grade, permission, allowed, reason, via)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What [`authorize()`](#liaise.access.authorize) decided about one message, and why.

`person` is who the message was attributed to (for a refusal, its resolved author,
if any). `via` says how: `handle`, `relay-label`, or `none` when the
decision attributes it to no one.

### liaise.access.Resolver

the identity-resolver seam.

* **Type:**
  `(address, subject) -> person id or None`

alias of `Callable`[[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Subject`](liaise.subjects.md#liaise.subjects.Subject)], [`str`](https://docs.python.org/3/builtins/stdtypes.html#str) | [`None`](https://docs.python.org/3/builtins/constants.html#None)]

### liaise.access.UNTRUSTED_CLAIM *= 'label claim by an untrusted author'*

Why [`authorize()`](#liaise.access.authorize) refused a message. A role or grade refusal names the role,
grade and permission instead.

### liaise.access.VIA_HANDLE *= 'handle'*

How an [`AccessDecision`](#liaise.access.AccessDecision) attributed its person.

### liaise.access.authorize(message, subject, permission, \*, resolver=<function resolve_person>)

Whether `message` may exercise `permission` on `subject`, by the label-as-claim rule.

See the module docstring for the rule. `resolver` maps the author’s address to a
person id (default [`resolve_person()`](#liaise.access.resolve_person)). Raises `ValueError` for a permission
outside [`PERMISSIONS`](liaise.model.md#liaise.model.PERMISSIONS).

* **Return type:**
  [`AccessDecision`](#liaise.access.AccessDecision)

### liaise.access.is_relay(address, subject)

Whether `address` is one of `subject`’s `policy.relays`, compared without regard to case.

A relay’s routing labels count as claims (see [`authorize()`](#liaise.access.authorize)), and its comments on
a case are recorded as the relay’s own, not as any person’s.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

### liaise.access.resolve_person(address, subject)

The person id `address` belongs to on `subject`, or None.

`policy.people` first, matched without regard to case. Otherwise, when acquaint
imports, `acquaint.resolve`, trusted only when it is `ok` with exactly one match.
A missing or broken acquaint, or any error it raises, resolves to None.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]
