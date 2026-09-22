# liaise.outcomes

Outcomes: what a processor run reports, checked, then planned into actions.

A run reports its result as structured output in the shape of [`OUTCOME_SCHEMA`](#liaise.outcomes.OUTCOME_SCHEMA),
which the processor passes to `claude --json-schema`: a `summary`, and one or more
outcomes, each a `kind` from the closed vocabulary [`OUTCOME_KINDS`](liaise.model.html.md#liaise.model.OUTCOME_KINDS)
with its `text`, `questions` and `reason`.

- [`parse_outcomes()`](#liaise.outcomes.parse_outcomes) checks that output and returns its
  [`Outcome`](liaise.model.html.md#liaise.model.Outcome) records, or raises `ValueError` listing every problem.
- [`normalize()`](#liaise.outcomes.normalize) makes each `decline` an `escalate`.
- [`plan_outcomes()`](#liaise.outcomes.plan_outcomes) turns a case’s outcomes into actions, in outcome order:
  - `ask`: [`Send`](#liaise.outcomes.Send) the text and the numbered questions, then
    [`Transition`](#liaise.outcomes.Transition) to `needs-partner`;
  - `reply`: [`Send`](#liaise.outcomes.Send);
  - `escalate`, and `decline`: [`StoreDraft`](#liaise.outcomes.StoreDraft), [`NotifyOperator`](#liaise.outcomes.NotifyOperator) at high
    priority, then [`Transition`](#liaise.outcomes.Transition) to `needs-owner`;
  - `propose`: [`Send`](#liaise.outcomes.Send), then [`Transition`](#liaise.outcomes.Transition) to `needs-partner`;
  - `deliver`: [`Deliver`](#liaise.outcomes.Deliver) as the subject’s `delivery` says, [`Send`](#liaise.outcomes.Send) the
    “try it” text, then [`Transition`](#liaise.outcomes.Transition) to `deployed`;
  - `defer`: [`Defer`](#liaise.outcomes.Defer);
  - `note`: [`DigestNote`](#liaise.outcomes.DigestNote).

Planning is pure: it reads the case and the subject and does nothing. The tick executes
the actions in order, and passes each [`Send`](#liaise.outcomes.Send) through [`liaise.gate.run_gate()`](liaise.gate.html.md#liaise.gate.run_gate)
before sending it.

**Where a Send goes.** To the case’s reporter, at the case’s first conversation on a
channel liaise can post into (GitHub, in v0.1). A case with none, such as one reported
through a web inbox, is answered at the reporter’s notify address when that is on a
channel that can write to a person (email). Failing both, the message becomes a draft
for the operator, held for `no channel to reach <person>`, and the operator is notified.

**What the operator is told.** A [`NotifyOperator`](#liaise.outcomes.NotifyOperator)’s title and body come from
`liaise.notify.notice_title()` and `liaise.notify.notice_body()`: the case and the
event, never the reporter, the draft or the reason the agent wrote, which stay on the case
for `liaise case show`.

### Module Attributes

| [`DFLT_SENDING_CHANNELS`](#liaise.outcomes.DFLT_SENDING_CHANNELS)   | Channels whose conversations liaise posts into.                                                                                |
|--------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------|
| [`DFLT_ADDRESS_CHANNELS`](#liaise.outcomes.DFLT_ADDRESS_CHANNELS)   | Channels that can write to a person's address (`email:<address>`), not only into a conversation.                               |
| [`DFLT_OPERATOR_PRIORITY`](#liaise.outcomes.DFLT_OPERATOR_PRIORITY)  | The ntfy priority of an operator notification that needs the operator to act.                                                  |
| [`HELD_REASON_PREFIX`](#liaise.outcomes.HELD_REASON_PREFIX)      | How the reason of a draft kept because a hold kept its effects waiting begins, before the hold's scope: `held: effect:deploy`. |
| [`REQUIRED_FIELD_BY_KIND`](#liaise.outcomes.REQUIRED_FIELD_BY_KIND)  | The field each outcome kind cannot do without.                                                                                 |
| [`OUTCOME_SCHEMA`](#liaise.outcomes.OUTCOME_SCHEMA)          | The JSON Schema of a run's structured result, passed to `claude --json-schema`.                                                |
| [`Action`](#liaise.outcomes.Action)                  | What [`plan_outcomes()`](#liaise.outcomes.plan_outcomes) returns a list of.                                       |

### Functions

| [`make_draft`](#liaise.outcomes.make_draft)(\*, at, outcome, recipient, ref, ...)   | One item of a case's `drafts`: a message held for the operator.                                                             |
|-----------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------|
| [`normalize`](#liaise.outcomes.normalize)(outcomes)                                | `outcomes` with each `decline` made an `escalate`, its reason `decline: <reason>`.                                          |
| [`parse_outcomes`](#liaise.outcomes.parse_outcomes)(structured_output)                  | The outcomes of a run's structured output, checked against [`OUTCOME_SCHEMA`](#liaise.outcomes.OUTCOME_SCHEMA). |
| [`plan_outcomes`](#liaise.outcomes.plan_outcomes)(case, outcomes, subject, \*, now)    | The actions that carry out `outcomes` on `case`, one group per outcome, in order.                                           |

### Classes

| [`Defer`](#liaise.outcomes.Defer)(case_id, reason)                           | Put the case aside for `reason`.                                                                                          |
|---------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------|
| [`Deliver`](#liaise.outcomes.Deliver)(case_id, kind, per, command)             | Deliver the case's work as the subject's `delivery` says.                                                                 |
| [`DigestNote`](#liaise.outcomes.DigestNote)(case_id, text)                        | Add `text` to the operator's digest.                                                                                      |
| [`NotifyOperator`](#liaise.outcomes.NotifyOperator)(title, body[, priority])          | Tell the operator, as `liaise.notify.notify()` does.                                                                      |
| [`Send`](#liaise.outcomes.Send)(\*, ref, channel, recipient, purpose, text) | Send `text` to `recipient` at `ref`.                                                                                      |
| [`StoreDraft`](#liaise.outcomes.StoreDraft)(case_id, draft)                       | Keep `draft` (see [`make_draft()`](#liaise.outcomes.make_draft)) in the case's `drafts` for the operator. |
| [`Transition`](#liaise.outcomes.Transition)(case_id, state, reason)               | Move the case to `state`, recording `reason`.                                                                             |

### liaise.outcomes.Action

What [`plan_outcomes()`](#liaise.outcomes.plan_outcomes) returns a list of.

alias of [`Send`](#liaise.outcomes.Send) | [`Transition`](#liaise.outcomes.Transition) | [`NotifyOperator`](#liaise.outcomes.NotifyOperator) | [`StoreDraft`](#liaise.outcomes.StoreDraft) | [`Deliver`](#liaise.outcomes.Deliver) | [`Defer`](#liaise.outcomes.Defer) | [`DigestNote`](#liaise.outcomes.DigestNote)

### liaise.outcomes.DFLT_ADDRESS_CHANNELS *= ('email',)*

Channels that can write to a person’s address (`email:<address>`), not only into a
conversation. GitHub is not one: a `github:<login>` handle cannot be messaged.

### liaise.outcomes.DFLT_OPERATOR_PRIORITY *= 'high'*

The ntfy priority of an operator notification that needs the operator to act.

### liaise.outcomes.DFLT_SENDING_CHANNELS *= ('github',)*

Channels whose conversations liaise posts into. A web inbox has no writer.

### *class* liaise.outcomes.Defer(case_id, reason)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Put the case aside for `reason`.

### *class* liaise.outcomes.Deliver(case_id, kind, per, command)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Deliver the case’s work as the subject’s `delivery` says.

### *class* liaise.outcomes.DigestNote(case_id, text)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Add `text` to the operator’s digest.

### liaise.outcomes.HELD_REASON_PREFIX *= 'held: '*

How the reason of a draft kept because a hold kept its effects waiting begins, before
the hold’s scope: `held: effect:deploy`.

### *class* liaise.outcomes.NotifyOperator(title, body, priority='default')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Tell the operator, as `liaise.notify.notify()` does.

### liaise.outcomes.OUTCOME_SCHEMA *: [dict](https://docs.python.org/3/builtins/stdtypes.html#dict)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [Any](https://docs.python.org/3/library/typing.html#typing.Any)]* *= {'additionalProperties': False, 'properties': {'outcomes': {'items': {'additionalProperties': False, 'properties': {'kind': {'description': 'What to do. Each kind needs one field: ask needs questions; reply needs text; escalate needs reason; propose needs text; deliver needs text; decline needs reason; defer needs reason; note needs text.', 'enum': ['ask', 'reply', 'escalate', 'propose', 'deliver', 'decline', 'defer', 'note'], 'type': 'string'}, 'questions': {'description': 'For ask: each question, with the default you will take if it goes unanswered.', 'items': {'type': 'string'}, 'type': 'array'}, 'reason': {'description': 'Why, for the operator: what an escalate, decline or defer is waiting on.', 'type': 'string'}, 'text': {'description': "The message to the partner. For escalate and decline, a draft the operator may send; for note, the line for the operator's digest.", 'type': 'string'}}, 'required': ['kind'], 'type': 'object'}, 'minItems': 1, 'type': 'array'}, 'summary': {'description': 'What this run did, in a sentence or two, for the operator.', 'type': 'string'}}, 'required': ['outcomes'], 'type': 'object'}*

The JSON Schema of a run’s structured result, passed to `claude --json-schema`.

### liaise.outcomes.REQUIRED_FIELD_BY_KIND *= mappingproxy({'ask': 'questions', 'reply': 'text', 'escalate': 'reason', 'propose': 'text', 'deliver': 'text', 'decline': 'reason', 'defer': 'reason', 'note': 'text'})*

The field each outcome kind cannot do without. [`parse_outcomes()`](#liaise.outcomes.parse_outcomes) enforces it
and [`OUTCOME_SCHEMA`](#liaise.outcomes.OUTCOME_SCHEMA) states it.

### *class* liaise.outcomes.Send(, ref, channel, recipient, purpose, text, title=None, case_id=None, cc=(), bcc=(), attachments=(), project=None)

Bases: [`Outbound`](liaise.gate.html.md#liaise.gate.Outbound)

Send `text` to `recipient` at `ref`. Every Send is the Outbound the gate checks.

### *class* liaise.outcomes.StoreDraft(case_id, draft)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Keep `draft` (see [`make_draft()`](#liaise.outcomes.make_draft)) in the case’s `drafts` for the operator.

### *class* liaise.outcomes.Transition(case_id, state, reason)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Move the case to `state`, recording `reason`.

### liaise.outcomes.make_draft(, at, outcome, recipient, ref, text, reason, notes=(), title=None, gate=None, send_key=None)

One item of a case’s `drafts`: a message held for the operator.

This is the one shape every draft has, JSON-ready:

- `at`: when it was held, as ISO-8601;
- `outcome`: the outcome kind it carries out (`ask`, `escalate`, …);
- `recipient`: the person id it is for;
- `ref`: the encoded conversation or address it would go to, or None when nothing
  can reach the recipient;
- `text`: the message, as it would be sent;
- `reason`: why it was held (an escalation’s reason, a gate divert, no channel);
- `notes`: the gate’s notes on it, in order;
- `title`: the title of the issue it would open, present only when it opens one;
- `gate`: what the gate decided, present only when the gate held it: its flow, the
  audience in words and the reasons ([`liaise.gate.GateDecision.summary()`](liaise.gate.html.md#liaise.gate.GateDecision.summary));
- `send_key`: the idempotency key its release sends with, present only when it has
  one: the key of the send it failed as, so a release never posts it twice
  ([`liaise.release.draft_send_key()`](liaise.release.html.md#liaise.release.draft_send_key)).

```pycon
>>> from datetime import datetime, timezone
>>> make_draft(at=datetime(2026, 9, 11, tzinfo=timezone.utc), outcome="reply",
...     recipient="pat", ref="github:example/app#12", text="Fixed.",
...     reason="draft reply mode")
{'at': '2026-09-11T00:00:00+00:00', 'outcome': 'reply', 'recipient': 'pat',
 'ref': 'github:example/app#12', 'text': 'Fixed.', 'reason': 'draft reply mode',
 'notes': []}
```

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### liaise.outcomes.normalize(outcomes)

`outcomes` with each `decline` made an `escalate`, its reason `decline: <reason>`.

A refusal reaches the operator before it reaches the partner. Every other outcome
is returned as it was.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`Outcome`](liaise.model.html.md#liaise.model.Outcome), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

### liaise.outcomes.parse_outcomes(structured_output)

The outcomes of a run’s structured output, checked against [`OUTCOME_SCHEMA`](#liaise.outcomes.OUTCOME_SCHEMA).

Beyond the schema, each kind must carry the field [`REQUIRED_FIELD_BY_KIND`](#liaise.outcomes.REQUIRED_FIELD_BY_KIND)
names: an `ask` its questions, a `reply` its text, and so on. A `decline` is
kept as reported ([`plan_outcomes()`](#liaise.outcomes.plan_outcomes) normalizes it). Raises `ValueError`
listing every problem found, not only the first.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`Outcome`](liaise.model.html.md#liaise.model.Outcome), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

### liaise.outcomes.plan_outcomes(case, outcomes, subject, , now, sending_channels=('github',), address_channels=('email',))

The actions that carry out `outcomes` on `case`, one group per outcome, in order.

The module docstring lists what each kind plans; `decline` is planned as
`escalate` (see [`normalize()`](#liaise.outcomes.normalize)). Messages are for `case.reporter`, and go to
the case’s first conversation on one of `sending_channels`. Failing that, they go
to the reporter’s first notify address on one of `address_channels` (see
[`notify_address_for()`](liaise.subjects.html.md#liaise.subjects.Subject.notify_address_for)). Failing both, each becomes a
[`StoreDraft`](#liaise.outcomes.StoreDraft) held for `no channel to reach <person>`, with a
[`NotifyOperator`](#liaise.outcomes.NotifyOperator) that names the case, not the person. `now` stamps the drafts.

Raises `ValueError` for an outcome kind outside the vocabulary.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[`Union`[[`Send`](#liaise.outcomes.Send), [`Transition`](#liaise.outcomes.Transition), [`NotifyOperator`](#liaise.outcomes.NotifyOperator), [`StoreDraft`](#liaise.outcomes.StoreDraft), [`Deliver`](#liaise.outcomes.Deliver), [`Defer`](#liaise.outcomes.Defer), [`DigestNote`](#liaise.outcomes.DigestNote)]]
