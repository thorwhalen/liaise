# liaise.github

The GitHub seam: one protocol, two implementations.

`liaise` never holds a token. All real access goes through the `gh` CLI, whose
auth belongs to the machine ([`GhCli`](#liaise.github.GhCli)). Every other module in this
package talks to [`GitHub`](#liaise.github.GitHub), never to `gh` or `subprocess` directly, so
tests use [`FakeGitHub`](#liaise.github.FakeGitHub) — in-memory, no network, no real repo.

### Classes

| [`Comment`](#liaise.github.Comment)(author, body, created_at, updated_at)   | One issue comment.                                                                                          |
|--------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------|
| [`FakeGitHub`](#liaise.github.FakeGitHub)([issues])                            | In-memory [`GitHub`](#liaise.github.GitHub), for tests.                               |
| [`GhCli`](#liaise.github.GhCli)(\*[, gh_bin])                             | The default [`GitHub`](#liaise.github.GitHub): every call shells out to the `gh` CLI. |
| [`GitHub`](#liaise.github.GitHub)(\*args, \*\*kwargs)                      | What `liaise` needs from GitHub.                                                                            |
| [`Issue`](#liaise.github.Issue)(repo, number, title, author, body, ...)   | One GitHub issue, with its comments.                                                                        |

### Exceptions

| [`GitHubError`](#liaise.github.GitHubError)   | Raised when the `gh` CLI fails — its stderr is the message.   |
|----------------------------------------------------------------|---------------------------------------------------------------|

### *class* liaise.github.Comment(author, body, created_at, updated_at)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One issue comment.

### *class* liaise.github.FakeGitHub(issues=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

In-memory [`GitHub`](#liaise.github.GitHub), for tests. No network, no real repo, no token.

Every other test in this package should use this rather than [`GhCli`](#liaise.github.GhCli).

#### SELF_AUTHOR *= 'liaise-bot'*

The fixed identity every comment posted through this fake carries —
stands in for “whatever GitHub identity `gh` is authenticated as” in
tests, so `ensure_last_comment_mentions()` has something to match.

#### labels_created(repo)

Test helper: labels created (via `create_label()`) for `repo`.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

#### seed(issue)

Add or replace an issue, for test setup.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### *class* liaise.github.GhCli(, gh_bin='gh')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

The default [`GitHub`](#liaise.github.GitHub): every call shells out to the `gh` CLI.

Auth belongs to whatever machine `gh` is configured on. This class never
reads, stores or passes a token.

### *class* liaise.github.GitHub(\*args, \*\*kwargs)

Bases: [`Protocol`](https://docs.python.org/3/library/typing.html#typing.Protocol)

What `liaise` needs from GitHub. Implemented by [`GhCli`](#liaise.github.GhCli) and [`FakeGitHub`](#liaise.github.FakeGitHub).

#### add_labels(repo, number, labels)

Add one or more labels to an issue. No-op for a label already present.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### create_label(repo, name, , color='ededed', description='')

Create a label if it does not already exist. Idempotent.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### ensure_last_comment_mentions(repo, number, mention)

Repair this identity’s own last comment on `number` to carry `mention`.

If the last comment posted by liaise’s own GitHub identity on this
issue does not already contain `mention`, prepends it (body content
otherwise untouched) and returns True. Returns False when the last
such comment already contains `mention`, or when this identity has
posted no comment on the issue at all. A rule the dispatched agent
forgets must still hold (#20).

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

#### get_issue(repo, number)

Read one issue, with its comments.

* **Return type:**
  [`Issue`](#liaise.github.Issue)

#### list_issues(repo, , label=None, author=None, state='open')

List issues in `repo`, optionally filtered by label and/or author.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Issue`](#liaise.github.Issue)]

#### post_comment(repo, number, body)

Post a comment on an issue.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### remove_labels(repo, number, labels)

Remove one or more labels from an issue. No-op for a label already absent.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### *exception* liaise.github.GitHubError

Bases: [`Exception`](https://docs.python.org/3/builtins/exceptions.html#Exception)

Raised when the `gh` CLI fails — its stderr is the message.

### *class* liaise.github.Issue(repo, number, title, author, body, created_at, updated_at, state, labels=<factory>, comments=<factory>)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One GitHub issue, with its comments.

#### *property* url *: [str](https://docs.python.org/3/builtins/stdtypes.html#str)*

The issue’s GitHub URL.
