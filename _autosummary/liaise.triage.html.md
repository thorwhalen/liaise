# liaise.triage

Triage: sort a batch of ready cases before dispatch (liaise #19).

A cheap headless pass (its own call, a small fixed prompt and a hard turn cap) reads every
case that became ready in the same run, plus the partner’s open cases for context, and
returns one verdict per ready case: `kind`, `duplicate_of`, `group`, `size`,
`priority`. From the verdicts this module builds the *groups* the tick dispatches (one job
per group, lead first, groups ordered by priority then age) and the labels and the one
plain-language comment that make the verdict visible on the issues.

The pass is a seam: an [`Ask`](#liaise.triage.Ask) (`prompt -> structured answer`), so tests use a fake and
[`headless_ask()`](#liaise.triage.headless_ask) is the real one. It stays optional and off by default
(`triage = true` in the subject). A pass that fails never blocks dispatch: the tick falls
back to one case at a time, in its own order, and says so.

The prompt (`data/triage_prompt.md`) and `TRIAGE_SCHEMA` are generic: nothing in
them names a partner, a repository or a product.

### Example

```pycon
>>> verdict = parse_verdicts({"verdicts": [{"id": "a-1", "kind": "problem",
...     "duplicate_of": None, "group": "login", "size": "small",
...     "priority": "blocking"}]}, ["a-1"])["a-1"]
>>> (verdict.kind, verdict.priority)
('problem', 'blocking')
```

### Module Attributes

| [`DFLT_KIND`](#liaise.triage.DFLT_KIND)             | Verdict applied to a case the pass said nothing usable about.                        |
|------------------------------------------------------------------------|--------------------------------------------------------------------------------------|
| [`DFLT_TRIAGE_MAX_TURNS`](#liaise.triage.DFLT_TRIAGE_MAX_TURNS) | it only classifies, so it needs very few.                                            |
| [`DFLT_TRIAGE_TIMEOUT`](#liaise.triage.DFLT_TRIAGE_TIMEOUT)   | Seconds the triage call may take before it counts as failed.                         |
| [`DFLT_EXCERPT_CHARS`](#liaise.triage.DFLT_EXCERPT_CHARS)    | Characters of a case's first message shown to the pass.                              |
| [`DUPLICATE_LABEL`](#liaise.triage.DUPLICATE_LABEL)       | The label every duplicate carries, besides its `kind:` / `size:` / `priority:` ones. |
| [`DUPLICATE_COMMENT`](#liaise.triage.DUPLICATE_COMMENT)     | The one comment on a duplicate; `{ref}` is the issue it duplicates.                  |
| [`DUPLICATE_PURPOSE`](#liaise.triage.DUPLICATE_PURPOSE)     | Ledger purpose of that comment.                                                      |
| [`GROUPED_EVENT`](#liaise.triage.GROUPED_EVENT)         | `{"lead": case_id}`.                                                                 |
| [`Ask`](#liaise.triage.Ask)                   | the triage call seam.                                                                |

### Functions

| [`compose_triage_prompt`](#liaise.triage.compose_triage_prompt)(cases[, context, ...])   | The prompt: the packaged text, the batch, and the partner's open cases as context.        |
|-------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| [`group_cases`](#liaise.triage.group_cases)(cases, verdicts)                   | The dispatch groups: cases sharing a `group` slug or a duplicate chain, together.         |
| [`headless_ask`](#liaise.triage.headless_ask)(prompt, \*[, max_turns, ...])     | Run the triage prompt as one headless, turn-capped call and return its structured answer. |
| [`label_specs`](#liaise.triage.label_specs)([prefix])                          | Every label a verdict can carry, as `(name, description)`, so they can be created.        |
| [`parse_verdicts`](#liaise.triage.parse_verdicts)(raw, case_ids)                  | A verdict for each of `case_ids` from the pass's answer; tolerant of bad entries.         |

### Classes

| [`LlmTriage`](#liaise.triage.LlmTriage)(ask, \*[, excerpt_chars])               | The [`Triage`](liaise.tick.html.md#liaise.tick.Triage) seam backed by an [`Ask`](#liaise.triage.Ask).   |
|----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`TriageVerdict`](#liaise.triage.TriageVerdict)(case_id[, kind, duplicate_of, ...]) | What the pass decided about one case.                                                                                                                    |

### liaise.triage.Ask

the triage call seam.

* **Type:**
  `prompt -> structured answer` (a mapping, or JSON text)

alias of [`Callable`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Callable)[[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)], [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### liaise.triage.DFLT_EXCERPT_CHARS *= 600*

Characters of a case’s first message shown to the pass.

### liaise.triage.DFLT_KIND *= 'request'*

Verdict applied to a case the pass said nothing usable about.

### liaise.triage.DFLT_TRIAGE_MAX_TURNS *= 3*

it only classifies, so it needs very few.

* **Type:**
  The turn cap of the triage call

### liaise.triage.DFLT_TRIAGE_TIMEOUT *= 300*

Seconds the triage call may take before it counts as failed.

### liaise.triage.DUPLICATE_COMMENT *= "This looks like the same thing as {ref}. I'll handle them together, so there is nothing more you need to do."*

The one comment on a duplicate; `{ref}` is the issue it duplicates.

### liaise.triage.DUPLICATE_LABEL *= 'duplicate'*

The label every duplicate carries, besides its `kind:` / `size:` / `priority:` ones.

### liaise.triage.DUPLICATE_PURPOSE *= 'triage_duplicate'*

Ledger purpose of that comment.

### liaise.triage.GROUPED_EVENT *= 'grouped'*

`{"lead": case_id}`.

* **Type:**
  Ledger entry kind detail marking a case grouped under a lead

### *class* liaise.triage.LlmTriage(ask, , excerpt_chars=600)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

The [`Triage`](liaise.tick.html.md#liaise.tick.Triage) seam backed by an [`Ask`](#liaise.triage.Ask).

Calling it with the ready cases returns the dispatch groups (lead first); `verdicts`
keeps the verdicts of the latest call, which the tick turns into labels and comments.
An `ask` that raises propagates: the tick treats that as a failed triage and falls
back to its own order.

#### accepts_context *= True*

The tick passes the partner’s open cases as `context=` to a seam that says so.

#### merges_groups *= True*

The tick starts each multi-case group as one job, its lead carrying the rest.

### *class* liaise.triage.TriageVerdict(case_id, kind='request', duplicate_of=None, group='', size='medium', priority='important')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

What the pass decided about one case. `duplicate_of` and `group` name case ids/slugs.

#### labels(prefix='')

The labels that show this verdict, each `<prefix>kind:...` style, plus `duplicate`.

* **Return type:**
  [`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`...`](https://docs.python.org/3/builtins/constants.html#Ellipsis)]

### liaise.triage.compose_triage_prompt(cases, context=(), , excerpt_chars=600)

The prompt: the packaged text, the batch, and the partner’s open cases as context.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.triage.group_cases(cases, verdicts)

The dispatch groups: cases sharing a `group` slug or a duplicate chain, together.

Within a group the lead is the highest-priority, then oldest case (it is the one that is
dispatched; the rest ride along in its prompt). Groups are ordered by their best priority,
then the age of their lead. Every case is in exactly one group.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Case`](liaise.model.html.md#liaise.model.Case)]]

### liaise.triage.headless_ask(prompt, , max_turns=3, claude_bin='claude', timeout=300, cwd=None)

Run the triage prompt as one headless, turn-capped call and return its structured answer.

No tools are granted beyond what the call needs to answer: it classifies text it was
handed. Raises `RuntimeError` if the call fails or returns nothing structured.

* **Return type:**
  [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)

### liaise.triage.label_specs(prefix='')

Every label a verdict can carry, as `(name, description)`, so they can be created.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]]

### liaise.triage.parse_verdicts(raw, case_ids)

A verdict for each of `case_ids` from the pass’s answer; tolerant of bad entries.

An id the pass did not name, or named with unusable values, gets the defaults (a
`request`, `medium`, `important`, alone in its own group). A `duplicate_of` that
is not another id of the batch or the open context, or names the case itself, is dropped,
and so is one that would make a cycle.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`TriageVerdict`](#liaise.triage.TriageVerdict)]
