# liaise.report

`liaise gate report`: what the outbound gate did, in counts, and whether to enforce (liaise #39).

The report reads the `gate` entries of every case and every message outside a case, and
counts; it never prints a message’s text, a finding’s value or a fingerprint, so it can be
pasted anywhere. What it counts (discussion 32 §5.7 and §7):

- **judged**: messages the gate judged on their own, with no approval on the context
  (a first judgement: the tick’s send, divert or hold, and `liaise message send`);
- **sent as judged**: judged messages that went out, and delays the outbox released when
  their window passed (a `hold` is not counted as **held**: no person releases it);
- **released**: an operator’s approval that bound, settled a rule’s concern and let the
  message go out (the outbox’s own approvals, by [`OUTBOX_ACTOR`](liaise.gate.md#liaise.gate.OUTBOX_ACTOR), and
  the release of a draft no rule held back, such as a failed send’s, are left out); a
  release that did not edit the message is a **false divert**: the operator judged it fine
  as written;
- **rejected**: a draft the operator declined, joined to the judgement that held it (the
  latest earlier judged entry of the same case or message with the same text, held for
  approval or refusal). A draft edited before it was rejected, or a text that recurs, can
  miss or mis-join: a draft id on both entries is the fix, not in this version;
- **per rule**: how often it fired, how often its findings were released as false positives
  (an operator approval settled it and the message went out unedited), and how often they
  were confirmed (a draft it held was
  rejected), with the precision `confirmed / (confirmed + released)`;
- **rates**: the override rate (releases over judged messages) and the false-divert rate
  (false diverts over the messages that should have gone as written: those sent as judged
  plus the false diverts);
- **shadow**: the judged messages of subjects in `policy.mode = "shadow"`. Shadow mode
  enforces like `enforce` (liaise #51: nothing sends that the policy holds back); beside
  each verdict the gate records what liaise 0.1 would have decided ([`liaise.legacy`](liaise.legacy.md#module-liaise.legacy)).
  **Compared** counts the shadow messages that carry that counterfactual; \*\*shadow
  agreement\*\* is the share of them where the policy and 0.1 are in the same flow class
  ([`liaise.legacy.flow_class()`](liaise.legacy.md#liaise.legacy.flow_class): sent, or held back), and a **missed finding** is one
  0.1 would have sent where the policy found severity [`MISSED_SEVERITY`](#liaise.report.MISSED_SEVERITY) or above.
  Entries recorded before the counterfactual was are shadow but not compared.
- **hook overrides**: writes the Claude Code hook ([`liaise.hook`](liaise.hook.md#module-liaise.hook)) asked the operator
  about and the operator let run, how many of them it could not read, and the rules among
  their reasons; apart from the counts above, since liaise neither held nor sent them.

**The rollout rule** (discussion 32, decision 11): enforce when shadow mode has seen at
least [`MIN_SHADOW_MESSAGES`](#liaise.report.MIN_SHADOW_MESSAGES) messages, with no missed finding of severity
[`MISSED_SEVERITY`](#liaise.report.MISSED_SEVERITY) or above and a false-divert rate of at most
[`MAX_FALSE_DIVERT_RATE`](#liaise.report.MAX_FALSE_DIVERT_RATE). [`enforce_recommended()`](#liaise.report.enforce_recommended) applies it and says why not.

### Module Attributes

| [`MIN_SHADOW_MESSAGES`](#liaise.report.MIN_SHADOW_MESSAGES)   | the fewest messages shadow mode must have seen before enforcing.                      |
|------------------------------------------------------------------------|---------------------------------------------------------------------------------------|
| [`MISSED_SEVERITY`](#liaise.report.MISSED_SEVERITY)       | a missed finding at this severity or above blocks enforcing.                          |
| [`MAX_FALSE_DIVERT_RATE`](#liaise.report.MAX_FALSE_DIVERT_RATE) | the highest false-divert rate at which enforcing is recommended (one in ten).         |
| [`MAX_RULE_NAME`](#liaise.report.MAX_RULE_NAME)         | The longest rule name the report prints.                                              |
| [`GATE_KIND`](#liaise.report.GATE_KIND)             | The entry kind the gate's decisions are recorded as.                                  |
| [`SHADOW_UNOBSERVED`](#liaise.report.SHADOW_UNOBSERVED)     | Why shadow agreement and missed findings cannot be counted when nothing was compared. |

### Functions

| [`enforce_recommended`](#liaise.report.enforce_recommended)(\*, shadow_messages, ...)   | Decision 11's rollout rule: `{"recommended": bool, "reason": str}`.                                                                               |
|--------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------|
| [`gate_report`](#liaise.report.gate_report)(ledger, \*[, subject, since, ...])  | The counts of the gate's decisions in `ledger` (a `Ledger` or its store).                                                                         |
| [`hook_overrides`](#liaise.report.hook_overrides)(ledger, \*[, subject, since])    | What the Claude Code hook asked about and the operator let run ([`liaise.hook`](liaise.hook.md#module-liaise.hook)). |
| [`report_lines`](#liaise.report.report_lines)(report)                            | `report` ([`gate_report()`](#liaise.report.gate_report)) for a terminal: counts, a per-rule table, the rollout line.             |

### liaise.report.GATE_KIND *= 'gate'*

The entry kind the gate’s decisions are recorded as.

### liaise.report.MAX_FALSE_DIVERT_RATE *= 0.1*

the highest false-divert rate at which enforcing is recommended (one in ten).

* **Type:**
  Decision 11

### liaise.report.MAX_RULE_NAME *= 40*

The longest rule name the report prints.

### liaise.report.MIN_SHADOW_MESSAGES *= 30*

the fewest messages shadow mode must have seen before enforcing.

* **Type:**
  Decision 11

### liaise.report.MISSED_SEVERITY *= 4*

a missed finding at this severity or above blocks enforcing.

* **Type:**
  Decision 11

### liaise.report.SHADOW_UNOBSERVED *= "no shadow message carries 0.1's counterfactual, which the gate records since liaise #39"*

Why shadow agreement and missed findings cannot be counted when nothing was compared.

### liaise.report.enforce_recommended(, shadow_messages, missed_high_severity, false_divert_rate)

Decision 11’s rollout rule: `{"recommended": bool, "reason": str}`.

`shadow_messages` counts the shadow messages compared with 0.1’s counterfactual, and
`missed_high_severity` how many of them 0.1 would have sent with a finding of
severity [`MISSED_SEVERITY`](#liaise.report.MISSED_SEVERITY) or above (None: not observable, which never
recommends).

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

```pycon
>>> enforce_recommended(shadow_messages=40, missed_high_severity=0, false_divert_rate=0.05)
{'recommended': True, 'reason': '40 shadow messages, no missed finding of severity 4 or above, false-divert rate 0.05 (at most 0.1)'}
>>> enforce_recommended(shadow_messages=12, missed_high_severity=0, false_divert_rate=0.0)['reason']
'only 12 shadow messages compared with 0.1, fewer than 30'
```

### liaise.report.gate_report(ledger, , subject=None, since=None, shadow_subjects=())

The counts of the gate’s decisions in `ledger` (a `Ledger` or its store).

`subject` keeps one subject’s, `since` the entries at or after it.
`shadow_subjects` are the subjects whose policy is in shadow mode now; an entry whose
verdict recorded `mode = "shadow"` counts as shadow whatever the subject says today.
Counts only: nothing a message says is in it.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### liaise.report.hook_overrides(ledger, , subject=None, since=None)

What the Claude Code hook asked about and the operator let run ([`liaise.hook`](liaise.hook.md#module-liaise.hook)).

`overrides` counts them, `unread` the writes among them the hook could not read and
so never vetted (with `subject`, 0: an unread write names no subject), and `rules`
how often each rule was among the reasons of the writes to `subject`. Records of an
unexpected shape are skipped. They are kept
apart from the gate’s own counts and rates: liaise neither held nor sent these writes,
and on the hook’s path every write is tainted, so folding them into a rule’s precision
would measure the hook’s path, not the rule.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### liaise.report.report_lines(report)

`report` ([`gate_report()`](#liaise.report.gate_report)) for a terminal: counts, a per-rule table, the rollout line.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]
