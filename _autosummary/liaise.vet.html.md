# liaise.vet

Vetting a draft outside any case: the gate’s verdict, and nothing sent (discussion 32, §5.8).

[`vet()`](#liaise.vet.vet) puts a draft through the same gate every message liaise sends passes
([`liaise.gate.run_gate()`](liaise.gate.html.md#liaise.gate.run_gate)), with a case-less context (liaise #28), and returns the
verdict as a JSON-ready record: the flow, the route, the reasons, the audience in words and
the tiers consulted. It sends nothing and records nothing, so it is safe to run at any time
and to expose over MCP later. `liaise vet` prints it, with the exit code
[`EXIT_CODES`](#liaise.vet.EXIT_CODES) gives the route: 0 send, 2 draft-to-operator, 3 block.
Whoever vets a draft then posts it themselves, at once, so the exit code is the route of
an immediate write ([`immediate_route()`](#liaise.vet.immediate_route)): a `delay` exits 2.

**The same gate, less what does not apply.** Discussion 32 rejects a separate gate for
`vet` (§11). [`VET_FILTERS`](#liaise.vet.VET_FILTERS) are [`DFLT_OUTBOUND_FILTERS`](liaise.gate.html.md#liaise.gate.DFLT_OUTBOUND_FILTERS) in their
order, less two whose job is liaise’s own sending: [`outside_a_case()`](liaise.gate.html.md#liaise.gate.outside_a_case)
holds every message liaise itself would send outside a case for the operator (its sender
chose the readers), and [`notify_recipient()`](liaise.gate.html.md#liaise.gate.notify_recipient) adds the `@mention` liaise
needs to reach someone on GitHub. Here the sender is whoever asks, and liaise sends
nothing; the hold on a sender-chosen destination is the taint rule’s job, since the
provenance of a vetted draft is unknown unless the caller says otherwise (decision 10).
`outbound_filters=` takes another tuple.

**The subject** is the one whose bindings take the reference in
([`liaise.subjects.subject_for_ref()`](liaise.subjects.html.md#liaise.subjects.subject_for_ref)); when none does, [`unbound_subject()`](liaise.subjects.html.md#liaise.subjects.unbound_subject), whose
policy has every default: nobody known, the taint rule in force, `direct` reply mode.

**Where nothing can be held.** A `delay` routes to `send` in liaise’s own sending
(discussion §5.5), because its outbox holds it. `liaise vet`’s exit code,
[`before_send()`](#liaise.vet.before_send) (correspond’s check) and the Claude Code hook stand in front of a write
that happens at once, with no outbox, so for them a `delay` degrades to
draft-to-operator, as §5.5 degrades it where the outbox does not exist:
[`immediate_route()`](#liaise.vet.immediate_route).

### Module Attributes

| [`VET_FILTERS`](#liaise.vet.VET_FILTERS)        | every one, in order, but the two that presuppose liaise as the sender (see the module docstring).   |
|---------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| [`EXIT_CODES`](#liaise.vet.EXIT_CODES)         | The exit code of `liaise vet` for each route.                                                       |
| [`VET_PURPOSE`](#liaise.vet.VET_PURPOSE)        | the writing card and deslop read it.                                                                |
| [`UNKNOWN_PROVENANCE`](#liaise.vet.UNKNOWN_PROVENANCE) | Why a vetted draft counts as tainted when the caller did not say (decision 10).                     |

### Functions

| [`before_send`](#liaise.vet.before_send)(ref, draft, audience, \*\*context)   | correspond's `before_send` check: [`vet()`](#liaise.vet.vet), raising `Refused` or `NeedsApproval`.     |
|---------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------|
| [`immediate_route`](#liaise.vet.immediate_route)(flow)                            | The route of `flow` for a write that happens at once: a `delay` is draft-to-operator.                                               |
| [`verdict_record`](#liaise.vet.verdict_record)(decision, \*, subject, ref)       | What `vet` answers about `decision`: JSON-ready, and never the text or a value found.                                               |
| [`vet`](#liaise.vet.vet)(text, \*, ref[, to, cc, bcc, title, ...])    | The gate's verdict on `text` sent to `ref` for `to`, as [`verdict_record()`](#liaise.vet.verdict_record) gives it. |

### liaise.vet.EXIT_CODES *: [Mapping](https://docs.python.org/3/library/collections.abc.html#collections.abc.Mapping)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [int](https://docs.python.org/3/builtins/functions.html#int)]* *= {'block': 3, 'draft': 2, 'send': 0}*

The exit code of `liaise vet` for each route.

### liaise.vet.UNKNOWN_PROVENANCE *= 'nobody said what the author of this draft read (--untainted says it read nothing untrusted)'*

Why a vetted draft counts as tainted when the caller did not say (decision 10).

### liaise.vet.VET_FILTERS *: [tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[Callable](https://docs.python.org/3/library/typing.html#typing.Callable)[[[Outbound](liaise.gate.html.md#liaise.gate.Outbound), [GateContext](liaise.gate.html.md#liaise.gate.GateContext)], [Pass](liaise.gate.html.md#liaise.gate.Pass) | [Divert](liaise.gate.html.md#liaise.gate.Divert)], ...]* *= (<function outbound_policy>, <function writing_card>, <function deslop>)*

every one, in order, but the two that presuppose
liaise as the sender (see the module docstring). Derived by exclusion, so a filter added
to the gate reaches `vet` too.

* **Type:**
  The gate’s filters as `vet` runs them

### liaise.vet.VET_PURPOSE *= 'reply'*

the writing card and deslop read it.

* **Type:**
  The purpose a vetted draft is judged for

### liaise.vet.before_send(ref, draft, audience, \*\*context)

correspond’s `before_send` check: [`vet()`](#liaise.vet.vet), raising `Refused` or `NeedsApproval`.

Set it once in correspond’s config:

```default
before_send = "liaise.vet:before_send"
```

`ref` is correspond’s conversation reference, `draft` its draft (its text, title and
copies are vetted) and `audience` the audience correspond computed, which the gate
judges as given. The subject is the one binding the reference, else
[`unbound_subject()`](liaise.subjects.html.md#liaise.subjects.unbound_subject), and the provenance is unknown. A block raises
`correspond.errors.Refused` and anything for the operator, a `delay` included (the
write happens at once: [`immediate_route()`](#liaise.vet.immediate_route)), `NeedsApproval`, each with the reasons
joined; a send returns None. The details carry the flow, the rules and the hashes,
never the text. A draft that cannot be vetted (a configuration that does not load) is
`NeedsApproval` with the reason.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### liaise.vet.immediate_route(flow)

The route of `flow` for a write that happens at once: a `delay` is draft-to-operator.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> immediate_route("delay"), immediate_route("send"), immediate_route("refuse")
('draft', 'send', 'block')
```

### liaise.vet.verdict_record(decision, , subject, ref)

What `vet` answers about `decision`: JSON-ready, and never the text or a value found.

`flow` and `route` are the decision’s (`route` as liaise’s own sending would
take it, where the outbox holds a `delay`). `exit_code` is the route of a write
that happens at once ([`immediate_route()`](#liaise.vet.immediate_route), [`EXIT_CODES`](#liaise.vet.EXIT_CODES)): whoever vets a
draft posts it themselves, so a `delay` exits 2. `reasons` every standing concern’s text, most restrictive first,
`rules` the rules they came from, `audience` the audience in words, `readers` the
tier and clearance of each reader consulted, `notes` every filter’s notes, and
`record` what a ledger `gate` entry would keep (`GateDecision.record()`).

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)

### liaise.vet.vet(text, \*, ref, to=(), cc=(), bcc=(), title=None, project=None, tainted=None, root=None, subjects=None, registry=None, audience=None, now=None, fingerprint_key=None, outbound_filters=(<function outbound_policy>, <function writing_card>, <function deslop>))

The gate’s verdict on `text` sent to `ref` for `to`, as [`verdict_record()`](#liaise.vet.verdict_record) gives it. Sends nothing.

`to` is the person (or people) the draft is for: the first is its recipient and the
rest count as copies, beside `cc` and `bcc`, so each is a reader the policy judges.
`project` names the project it is about. `tainted` is what the author read: None
(unknown, which counts as tainted), True, or False (`--untainted`: nothing untrusted).
`root` is liaise’s config root, whose subjects and state directory are read (none is
needed); `subjects` replaces the subjects read from it. `audience` is correspond’s
record of who can read `ref`; None asks correspond now, on `registry`.
`fingerprint_key` is as [`GateContext`](liaise.gate.html.md#liaise.gate.GateContext) has it; by default the key
in the state directory, and none is created.

Raises `ValueError` for a reference that is not one, and
[`ConfigError`](liaise.config.html.md#liaise.config.ConfigError) for a configuration that does not load.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)
