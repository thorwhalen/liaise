# liaise.projection

Label projection: a case’s state, shown on each of its GitHub issues as one label.

In 0.1 the ledger holds a case’s state. The `<label_prefix><state>` label on its GitHub
issues (`liaise:working`) is a projection of it, kept for the partner and the owner to
see at a glance. [`project_labels()`](#liaise.projection.project_labels) makes each issue carry exactly the current state’s
label: the other state labels come off, then the current one goes on. It is the one place
a state label changes, so the one-label invariant is kept there.

**Waiting labels.** When a subject sets `policy.waiting_labels`, a case that waits on a
person ([`WAITING_STATES`](#liaise.projection.WAITING_STATES): its reporter was asked) also carries that person’s label,
`needs-pat`, and the same function keeps that invariant too: at most one waiting label,
and none once the case waits on no one. With several people on a subject, it is the fact a
reader filters a backlog by. It is the mirror of a claim label: liaise reads a claim label
as a claim coming in, and writes a waiting label as a fact going out.

[`setup_labels()`](#liaise.projection.setup_labels) creates the labels a subject needs in each repository it binds
(`liaise setup`): its claim labels, its waiting labels, and one label per case state.

The labels go through [`liaise.github.GitHub`](liaise.github.md#liaise.github.GitHub) (`GhCli`, or `FakeGitHub` in
tests), since correspond has no label operations yet. Only a case’s
`github:owner/repo#N` conversations are projected; any other conversation has no labels.

### Module Attributes

| [`GITHUB_CHANNEL`](#liaise.projection.GITHUB_CHANNEL)            | The channel whose conversations carry labels.                                       |
|----------------------------------------------------------------------------|-------------------------------------------------------------------------------------|
| [`LABEL_SPECS_RESOURCE`](#liaise.projection.LABEL_SPECS_RESOURCE)      | Each state label's description and colour, in `liaise/data`.                        |
| [`DFLT_LABEL_COLOR`](#liaise.projection.DFLT_LABEL_COLOR)          | GitHub's own grey.                                                                  |
| [`CLAIM_LABEL_DESCRIPTION`](#liaise.projection.CLAIM_LABEL_DESCRIPTION)   | What a claim label says on GitHub, formatted with the person it files an issue for. |
| [`WAITING_STATES`](#liaise.projection.WAITING_STATES)            | its reporter, asked a question or a proposal.                                       |
| [`WAITING_LABEL_DESCRIPTION`](#liaise.projection.WAITING_LABEL_DESCRIPTION) | What a waiting label says on GitHub, formatted with the person the case waits on.   |

### Functions

| [`github_issue`](#liaise.projection.github_issue)(conversation)                        | `(owner/repo, number)` for an encoded `github:owner/repo#N`, else None.                      |
|----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| [`github_repos`](#liaise.projection.github_repos)(subject)                             | The `owner/repo` of each GitHub repository `subject` binds, once each, in binding order.     |
| [`project_labels`](#liaise.projection.project_labels)(case, subject, \*, labeler[, ...]) | Label each of `case`'s GitHub issues with its state, and with no other state.                |
| [`setup_labels`](#liaise.projection.setup_labels)(labeler, subject)                    | Create the labels `subject` needs in each GitHub repository it binds; a line per repository. |
| [`waiting_label`](#liaise.projection.waiting_label)(case, subject)                      | The waiting label `case` carries now: its reporter's while it waits on them, else None.      |

### liaise.projection.CLAIM_LABEL_DESCRIPTION *= 'Files the issue for {person}. It counts only when a relay sets it.'*

What a claim label says on GitHub, formatted with the person it files an issue for.

### liaise.projection.DFLT_LABEL_COLOR *= 'ededed'*

GitHub’s own grey.

* **Type:**
  The colour of a label no spec describes

### liaise.projection.GITHUB_CHANNEL *= 'github'*

The channel whose conversations carry labels.

### liaise.projection.LABEL_SPECS_RESOURCE *= 'labels.json'*

Each state label’s description and colour, in `liaise/data`.

### liaise.projection.WAITING_LABEL_DESCRIPTION *= 'Waiting on {person} to answer. liaise sets and removes it.'*

What a waiting label says on GitHub, formatted with the person the case waits on.

### liaise.projection.WAITING_STATES *= ('needs-partner',)*

its reporter, asked a question or a
proposal. `needs-owner` waits on the operator, who has no waiting label.

* **Type:**
  The states in which a case waits on a person

### liaise.projection.github_issue(conversation)

`(owner/repo, number)` for an encoded `github:owner/repo#N`, else None.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`tuple`](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`int`](https://docs.python.org/3/builtins/functions.html#int)]]

```pycon
>>> github_issue("github:example/app#12")
('example/app', 12)
>>> github_issue("github:example/app") is None, github_issue("webinbox:example-site") is None
(True, True)
```

### liaise.projection.github_repos(subject)

The `owner/repo` of each GitHub repository `subject` binds, once each, in binding order.

A binding on one issue names its repository. A binding with a wildcard in its
conversation part is polled on nothing (see [`liaise.subjects.poll_ref()`](liaise.subjects.md#liaise.subjects.poll_ref)), so it
names none. Repositories compare without regard to case, as GitHub’s do.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.projection.project_labels(case, subject, , labeler, dry_run=False, stale=())

Label each of `case`’s GitHub issues with its state, and with no other state.

For every `github:owner/repo#N` conversation of the case, the other
`<label_prefix><state>` labels are removed and the current one is added (labels
that are not state labels stay). When the subject has waiting labels, the other
people’s come off too, and [`waiting_label()`](#liaise.projection.waiting_label) goes on beside the state label while
the case waits on its reporter. `stale` are waiting labels projected before that the
subject no longer gives (turned off, or renamed): they come off as well, except a label
that is now a claim label. Returns one line per issue. A dry run returns the lines and
calls nothing on `labeler`. A `GitHubError` from `labeler` propagates.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.projection.setup_labels(labeler, subject)

Create the labels `subject` needs in each GitHub repository it binds; a line per repository.

Those are its `policy.claim_labels`, the routing labels a relay puts on the issues it
files; its `policy.waiting_labels`, coloured as the state they go with; and one
`<label_prefix><state>` label per case state, described and coloured as
`data/labels.json` says. Idempotent, since `create_label` updates a label that
already exists. A `GitHubError` from `labeler` propagates.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.projection.waiting_label(case, subject)

The waiting label `case` carries now: its reporter’s while it waits on them, else None.

A case waits on its reporter in [`WAITING_STATES`](#liaise.projection.WAITING_STATES), and the label is
`policy.waiting_labels[reporter]`. A reporter the policy gives no label has none.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]
