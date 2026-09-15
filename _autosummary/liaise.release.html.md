# liaise.release

Releasing a message: through the gate, then through correspond, as one step.

Every message liaise sends goes through [`gate_and_send()`](#liaise.release.gate_and_send). That covers what the tick
sends for a run’s outcomes, the tick’s own notices, and a draft the operator releases
with `liaise case send-draft`. It runs [`liaise.gate.run_gate()`](liaise.gate.html.md#liaise.gate.run_gate), and only a message
the gate passed reaches `correspond.send`, as the filters left it. It records nothing:
what a [`SendAttempt`](#liaise.release.SendAttempt) means for a case, a draft or a notification is for its caller
to keep.

One path for every sender is what makes the gate a gate. A filter added to it applies to
all of them at once, and none of them has a way to send around it.

### Module Attributes

| [`DFLT_REFUSAL`](#liaise.release.DFLT_REFUSAL)   | Why a send failed when the channel said no without saying why.   |
|-----------------------------------------------------------------|------------------------------------------------------------------|

### Functions

| [`error_text`](#liaise.release.error_text)(error)                                 | How liaise names an exception it recovered from: its class, then its message.         |
|----------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------|
| [`gate_and_send`](#liaise.release.gate_and_send)(outbound, ctx, \*[, registry, ...]) | Put `outbound` through the gate and, only when it passes, send it through correspond. |

### Classes

| [`SendAttempt`](#liaise.release.SendAttempt)(decision[, result, failure, ...])   | What [`gate_and_send()`](#liaise.release.gate_and_send) did with one message.   |
|--------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------|

### liaise.release.DFLT_REFUSAL *= 'the channel refused it'*

Why a send failed when the channel said no without saying why.

### *class* liaise.release.SendAttempt(decision, result=None, failure=None, failure_kind=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What [`gate_and_send()`](#liaise.release.gate_and_send) did with one message.

`decision` is the gate’s. Once the gate has passed the message, `result` is
correspond’s `SendResult`, or None when sending raised. `failure` says why the
channel did not take the message, and is None once it did. `failure_kind` names that
failure: correspond’s `error_kind`, or the class of what was raised. A message the
gate diverted has none of the three.

#### *property* outbound *: [Outbound](liaise.gate.html.md#liaise.gate.Outbound) | [None](https://docs.python.org/3/builtins/constants.html#None)*

The message as the gate’s filters left it, or None when the gate diverted it.

#### *property* sent *: [bool](https://docs.python.org/3/builtins/functions.html#bool)*

Whether the channel took the message; in a dry run, whether it would have.

### liaise.release.error_text(error)

How liaise names an exception it recovered from: its class, then its message.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> error_text(ValueError("no such channel"))
'ValueError: no such channel'
```

### liaise.release.gate_and_send(outbound, ctx, \*, registry=None, dry_run=False, outbound_filters=(<function reply_mode>, <function leak_scan>, <function writing_card>, <function deslop>, <function notify_recipient>))

Put `outbound` through the gate and, only when it passes, send it through correspond.

The gate is [`liaise.gate.run_gate()`](liaise.gate.html.md#liaise.gate.run_gate) with `outbound_filters`, and a message it
diverts is not sent. A passed message goes to `correspond.send` as the filters left
it, on `registry` (correspond’s own when None). `dry_run` asks correspond for its
plan and sends nothing. A channel that refuses the message, or raises, becomes a
`failure` on the attempt rather than an exception, so the caller still has the message
to keep.

* **Return type:**
  [`SendAttempt`](#liaise.release.SendAttempt)
