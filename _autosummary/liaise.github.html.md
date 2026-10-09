# liaise.github

The GitHub seam: one protocol, two implementations.

`liaise` never holds a token. All real access goes through the `gh` CLI, whose
auth belongs to the machine ([`GhCli`](#liaise.github.GhCli)). Every other module in this
package talks to [`GitHub`](#liaise.github.GitHub), never to `gh` or `subprocess` directly, so
tests use [`FakeGitHub`](#liaise.github.FakeGitHub) — in-memory, no network, no real repo.

Issues are read and labelled through `gh issue`; pull requests ([`Pull`](#liaise.github.Pull), for
[`liaise.review`](liaise.review.html.md#module-liaise.review)) through `gh pr`: listed, read with their head commit, checks and
mergeability, diffed, reviewed (`post_review`) and merged (`merge_pull`, bound to the
head commit it was approved at). A pull request is an issue to GitHub, so its labels go
through the same label verbs, with `pull=True`.

### Module Attributes

| [`PULL_LIST_LIMIT`](#liaise.github.PULL_LIST_LIMIT)   | The most open pull requests one `gh pr list` asks for.              |
|--------------------------------------------------------------------|---------------------------------------------------------------------|
| [`CHECKS_SUCCESS`](#liaise.github.CHECKS_SUCCESS)    | all green, still running, at least one failed, or no checks at all. |
| [`CHECKS_PENDING`](#liaise.github.CHECKS_PENDING)    | all green, still running, at least one failed, or no checks at all. |
| [`CHECKS_FAILURE`](#liaise.github.CHECKS_FAILURE)    | all green, still running, at least one failed, or no checks at all. |
| [`CHECKS_NONE`](#liaise.github.CHECKS_NONE)       | all green, still running, at least one failed, or no checks at all. |
| [`CHECK_STATES`](#liaise.github.CHECK_STATES)      | all green, still running, at least one failed, or no checks at all. |
| [`REVIEW_EVENTS`](#liaise.github.REVIEW_EVENTS)     | A review's event, as the GitHub API names it.                       |
| [`MERGE_METHODS`](#liaise.github.MERGE_METHODS)     | The merge methods `gh pr merge` offers.                             |
| [`MERGED_STATE`](#liaise.github.MERGED_STATE)      | `gh`'s word for a pull request that is merged, in `state`.          |

### Functions

| [`checks_state`](#liaise.github.checks_state)(rollup)   | One of [`CHECK_STATES`](#liaise.github.CHECK_STATES) for a `statusCheckRollup` as `gh pr view` gives it.   |
|-------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------|

### Classes

| [`Comment`](#liaise.github.Comment)(author, body, created_at, updated_at)   | One issue comment.                                                                                                                |
|--------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------|
| [`FakeGitHub`](#liaise.github.FakeGitHub)([issues])                            | In-memory [`GitHub`](#liaise.github.GitHub), for tests.                                                     |
| [`GhCli`](#liaise.github.GhCli)(\*[, gh_bin])                             | The default [`GitHub`](#liaise.github.GitHub): every call shells out to the `gh` CLI.                       |
| [`GitHub`](#liaise.github.GitHub)(\*args, \*\*kwargs)                      | What `liaise` needs from GitHub.                                                                                                  |
| [`Issue`](#liaise.github.Issue)(repo, number, title, author, body, ...)   | One GitHub issue, with its comments.                                                                                              |
| [`Pull`](#liaise.github.Pull)(repo, number, title, author, body, ...)    | One pull request, as a review needs it (see [`liaise.review`](liaise.review.html.md#module-liaise.review)). |

### Exceptions

| [`GitHubError`](#liaise.github.GitHubError)   | Raised when the `gh` CLI fails — its stderr is the message.   |
|----------------------------------------------------------------|---------------------------------------------------------------|

### liaise.github.CHECKS_FAILURE *= 'failure'*

all green, still running,
at least one failed, or no checks at all.

* **Type:**
  What a pull request’s checks add up to (`Pull.checks`)

### liaise.github.CHECKS_NONE *= 'none'*

all green, still running,
at least one failed, or no checks at all.

* **Type:**
  What a pull request’s checks add up to (`Pull.checks`)

### liaise.github.CHECKS_PENDING *= 'pending'*

all green, still running,
at least one failed, or no checks at all.

* **Type:**
  What a pull request’s checks add up to (`Pull.checks`)

### liaise.github.CHECKS_SUCCESS *= 'success'*

all green, still running,
at least one failed, or no checks at all.

* **Type:**
  What a pull request’s checks add up to (`Pull.checks`)

### liaise.github.CHECK_STATES *= ('success', 'pending', 'failure', 'none')*

all green, still running,
at least one failed, or no checks at all.

* **Type:**
  What a pull request’s checks add up to (`Pull.checks`)

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

#### merge_error *: [str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None)*

What the next merge fails with, if anything (a test sets it).

#### merges *: [list](https://docs.python.org/3/builtins/stdtypes.html#list)[[tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [int](https://docs.python.org/3/builtins/functions.html#int), [str](https://docs.python.org/3/builtins/stdtypes.html#str), [str](https://docs.python.org/3/builtins/stdtypes.html#str)]]*

`(repo, number, method, match_head_sha)`, in order.

* **Type:**
  Every merge made

#### reviews *: [list](https://docs.python.org/3/builtins/stdtypes.html#list)[[tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [int](https://docs.python.org/3/builtins/functions.html#int), [str](https://docs.python.org/3/builtins/stdtypes.html#str), [str](https://docs.python.org/3/builtins/stdtypes.html#str)]]*

`(repo, number, body, event)`, in order.

* **Type:**
  Every review posted

#### seed(issue)

Add or replace an issue, for test setup.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### seed_pull(pull, , diff=None)

Add or replace a pull request, and its diff when given, for test setup.

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

#### add_labels(repo, number, labels, , pull=False)

Add one or more labels to an issue, or with `pull` a pull request. No-op for a label already present.

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

#### get_pull(repo, number)

Read one pull request, with its head commit, checks and mergeability as of now.

* **Return type:**
  [`Pull`](#liaise.github.Pull)

#### list_issues(repo, , label=None, author=None, state='open')

List issues in `repo`, optionally filtered by label and/or author.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Issue`](#liaise.github.Issue)]

#### list_pulls(repo, , author=None, state='open')

The pull requests of `repo` in `state` (`open`, `closed`, `merged`, `all`), by `author` when given.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Pull`](#liaise.github.Pull)]

#### merge_pull(repo, number, , method='squash', match_head_sha)

Merge a pull request by `method`, only while its head is still `match_head_sha`.

Raises [`GitHubError`](#liaise.github.GitHubError) when the head moved, the pull request cannot be
merged, or GitHub refuses.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### post_comment(repo, number, body)

Post a comment on an issue.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### post_review(repo, number, body, , event)

Post a review with `body` and `event` (one of [`REVIEW_EVENTS`](#liaise.github.REVIEW_EVENTS)) on a pull request.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

#### pull_diff(repo, number)

The pull request’s unified diff against its base, as GitHub computes it.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

#### remove_labels(repo, number, labels, , pull=False)

Remove one or more labels from an issue, or with `pull` a pull request. No-op for a label already absent.

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

### liaise.github.MERGED_STATE *= 'merged'*

`gh`’s word for a pull request that is merged, in `state`.

### liaise.github.MERGE_METHODS *= ('squash', 'merge', 'rebase')*

The merge methods `gh pr merge` offers.

### liaise.github.PULL_LIST_LIMIT *= 200*

The most open pull requests one `gh pr list` asks for.

### *class* liaise.github.Pull(repo, number, title, author, body, head_sha, base, created_at, updated_at, state='open', is_draft=False, mergeable=None, checks='none', labels=<factory>, url='')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One pull request, as a review needs it (see [`liaise.review`](liaise.review.html.md#module-liaise.review)).

`head_sha` is the commit the review is of; `base` the branch it targets;
`mergeable` GitHub’s answer (None while it has not computed one); `checks` one of
[`CHECK_STATES`](#liaise.github.CHECK_STATES); `state` `open`, `closed` or `merged`.

#### *property* ref *: [str](https://docs.python.org/3/builtins/stdtypes.html#str)*

The encoded conversation reference liaise keys the review case on, lower-cased as GitHub refs compare.

### liaise.github.REVIEW_EVENTS *= ('APPROVE', 'REQUEST_CHANGES', 'COMMENT')*

A review’s event, as the GitHub API names it.

### liaise.github.checks_state(rollup)

One of [`CHECK_STATES`](#liaise.github.CHECK_STATES) for a `statusCheckRollup` as `gh pr view` gives it.

A check run (`__typename` `CheckRun`) counts by its `conclusion` once its
`status` is `COMPLETED`; a commit status (`StatusContext`) by its `state`.
Any failure makes the whole `failure`. Otherwise `success` only when every item
concluded success, neutral or skipped; anything else (running, queued, expected, a
`STALE` conclusion, an unknown word) is `pending`, which never merges.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> checks_state([])
'none'
>>> checks_state([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}])
'success'
>>> checks_state([{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"__typename": "StatusContext", "state": "PENDING"}])
'pending'
>>> checks_state([{"status": "IN_PROGRESS"}, {"state": "FAILURE"}])
'failure'
>>> checks_state([{"status": "COMPLETED", "conclusion": "STALE"}])
'pending'
```
