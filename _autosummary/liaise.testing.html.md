# liaise.testing

Fakes shipped with liaise: for its tests, and for the one-command smoke test.

[`FakeGitHubChannel`](#liaise.testing.FakeGitHubChannel) is a correspond channel adapter held in memory. Its references,
messages, delivery ids and events have the shapes correspond’s GitHub adapter gives them,
so `liaise.intake.intake()` cannot tell the two apart, and nothing it does leaves the
process. [`add_webinbox_report()`](#liaise.testing.add_webinbox_report) stores a report in correspond’s own `WebInbox`
exactly as its collector stores one. [`demo_registry()`](#liaise.testing.demo_registry) puts the two together as the
channel registry `correspond.listen` and `correspond.send` take.

Every value here is fictional: the partner `pat`, the repository `example/app`, the
site `example-site` and the relay `example-bot`.

```pycon
>>> import correspond
>>> github = FakeGitHubChannel()
>>> _ = github.add_issue(
...     "example/app", 12, author="pat", title="Export", body="The export drops a row.",
...     labels=["partner:pat"], created_at="2026-09-11T09:00:00Z",
... )
>>> events = correspond.listen(
...     "github:example/app", cursors={}, registry=demo_registry(github=github)
... )
>>> [(e.delivery_id, e.message.native["labels"]) for e in events]
[('github:example/app:issue-12@2026-09-11T09:00:00Z', ['partner:pat'])]
```

### Module Attributes

| [`DFLT_GITHUB_CHANNEL`](#liaise.testing.DFLT_GITHUB_CHANNEL)   | correspond's own adapter's.                                                                                                           |
|------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------|
| [`SELF_LOGIN`](#liaise.testing.SELF_LOGIN)            | The login a fake GitHub channel's own sends go out under (`FakeGitHub`'s, too).                                                       |
| [`DFLT_SITE_SECRET`](#liaise.testing.DFLT_SITE_SECRET)      | What the fake web inbox's host application signs its logged-in users with.                                                            |
| [`NATIVE_FIELDS`](#liaise.testing.NATIVE_FIELDS)         | The `native` fields an issue opening carries.                                                                                         |
| [`ISSUE_STATES`](#liaise.testing.ISSUE_STATES)          | The states an issue is in, as GitHub names them (see [`FakeGitHubChannel.set_state()`](#liaise.testing.FakeGitHubChannel.set_state)). |
| [`VISIBILITIES`](#liaise.testing.VISIBILITIES)          | GitHub's three, and `hidden` for one the account cannot see.                                                                          |

### Functions

| [`add_webinbox_report`](#liaise.testing.add_webinbox_report)(inbox, site, \*, text, ...)   | Store a report for `site` in `inbox` exactly as correspond's collector stores one.   |
|----------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------|
| [`demo_registry`](#liaise.testing.demo_registry)(\*[, github, webinbox])             | A correspond channel registry of fakes: `{"github": ..., "webinbox": ...}`.          |

### Classes

| [`FakeGitHubChannel`](#liaise.testing.FakeGitHubChannel)(\*[, name, lookback, ...])   | GitHub issues held in memory, as a correspond channel that reads, listens and sends.   |
|-------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------|

### liaise.testing.DFLT_GITHUB_CHANNEL *= 'github'*

correspond’s own adapter’s.

* **Type:**
  The name a fake GitHub channel registers under

### liaise.testing.DFLT_SITE_SECRET *= 'example-site-secret'*

What the fake web inbox’s host application signs its logged-in users with.

### *class* liaise.testing.FakeGitHubChannel(\*, name='github', lookback=None, clock=<function \_utc_now>, visibility='private', owner_type='User')

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

GitHub issues held in memory, as a correspond channel that reads, listens and sends.

Seed it with [`add_issue()`](#liaise.testing.FakeGitHubChannel.add_issue) and [`add_comment()`](#liaise.testing.FakeGitHubChannel.add_comment); [`poll()`](#liaise.testing.FakeGitHubChannel.poll) yields what was
added after its cursor, in the order it was added. A real send is recorded in `sent`
as `(ref, draft)` and posted as the channel’s own (`is_self`) at `clock()`, so
the next poll yields it, as GitHub’s would. Set `send_error` to a
`correspond.ChannelError` and every real send raises it, which `correspond.send`
turns into a failed result.

`lookback` makes a first poll (one without a cursor) skip what last changed more
than that long before `clock()`, as correspond’s GitHub adapter looks back only
`LISTEN_LOOKBACK`. With None, the default, a first poll yields everything.

[`audience()`](#liaise.testing.FakeGitHubChannel.audience) answers who reads a repository as correspond’s GitHub adapter does,
from its visibility: `visibility` for every repository, unless [`set_visibility()`](#liaise.testing.FakeGitHubChannel.set_visibility)
gave one its own. The default is a private repository its owner (a user) owns, whose
audience is `named` and so neither public nor organisation-wide; a test of the gate on a
public repository says so.

#### add_comment(repo, number, , author, body, created_at, edited_at=None, is_self=False)

Seed a comment on the seeded issue `repo#number`; the next poll yields it.

`edited_at`, when the comment was edited, becomes its `edited_at`, and its
delivery id ends with it, as correspond’s GitHub adapter ends one with the
comment’s `updated_at`. Raises `ValueError` when that issue was never seeded.

* **Return type:**
  `Message`

#### add_issue(repo, number, , author, title, body, labels=(), created_at, grade=Grade.PLATFORM, state='open', is_self=False)

Seed the opening post of issue `repo#number`; the next poll yields it.

The message carries `native` `number`, `title`, `labels` and `state`.
Raises `ValueError` for an issue already seeded.

* **Return type:**
  `Message`

#### audience(ref, , draft=None)

Who can read `ref`’s repository, as correspond’s GitHub adapter answers from its visibility.

A draft changes nothing: a mention decides who is notified, not who can read.

* **Return type:**
  `Audience`

#### *property* capabilities *: Capabilities*

Read, listen and send on issues, graded `platform`, as GitHub’s are.

#### parse_ref(id)

`owner/repo` or `owner/repo#N`, lower-cased, as correspond’s GitHub adapter parses them.

* **Return type:**
  `ConversationRef`

#### poll(ref, , cursor=None, limit=None)

Events for what was added after `cursor` (opaque), in the order it was added.

A repository’s poll yields its issue openings and comments; an issue’s poll only
that issue’s comments, as correspond’s GitHub adapter does. Delivery ids are
`<name>:owner/repo:issue-<N>@<created_at>` and
`<name>:owner/repo:issuecomment-<id>@<updated_at>`. A first poll with a
`lookback` yields only what changed within it.

* **Return type:**
  [`Iterator`](https://docs.python.org/3/library/collections.abc.html#collections.abc.Iterator)[`Event`]

#### read(ref, , since=None, limit=None)

An issue’s opening and comments; for a repository, its open issues’ openings.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[`Message`]

#### send(ref, draft, , dry_run=False)

Comment on an issue; on a repository, open an issue (`title` required).

A dry run returns the plan and records nothing.

* **Return type:**
  `SendResult`

#### set_state(repo, number, state)

Close or reopen the seeded issue `repo#number`, as someone on GitHub would.

`state` is one of [`ISSUE_STATES`](#liaise.testing.ISSUE_STATES). The opening’s `native["state"]`
changes, so a read of the issue, or of the repository’s open issues, sees it. A
poll yields nothing for it, as correspond’s GitHub adapter hears an older issue’s
changes only through its comments. Returns the opening as it is now; raises
`ValueError` for an unknown state or an issue never seeded.

* **Return type:**
  `Message`

#### set_visibility(repo, visibility, , owner_type=None)

Make `repo` (`owner/repo`) `public`, `private`, `internal` or `hidden`.

`hidden` answers as GitHub does for a repository the account cannot see, and the
audience defaults to public. `owner_type` is `User` or `Organization`; it
keeps the repository’s own, or the channel’s default.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)

### liaise.testing.ISSUE_STATES *= ('open', 'closed')*

The states an issue is in, as GitHub names them (see [`FakeGitHubChannel.set_state()`](#liaise.testing.FakeGitHubChannel.set_state)).

### liaise.testing.NATIVE_FIELDS *= ('number', 'title', 'labels', 'state')*

The `native` fields an issue opening carries. Comments carry none.

### liaise.testing.SELF_LOGIN *= 'liaise-bot'*

The login a fake GitHub channel’s own sends go out under (`FakeGitHub`’s, too).

### liaise.testing.VISIBILITIES *= ('public', 'private', 'internal', 'hidden')*

GitHub’s three, and `hidden` for one the
account cannot see. A fake repository is private and owned by a user unless told.

* **Type:**
  What a fake repository’s visibility may be

### liaise.testing.add_webinbox_report(inbox, site, , text, received_at, user=None, name=None, email=None, page=None, context=None, secret='example-site-secret')

Store a report for `site` in `inbox` exactly as correspond’s collector stores one.

With `user`, the report carries an identity the site’s host application signed with
`secret` (correspond’s `sign_identity`), checked as the collector checks it, so it
is `bound` and its author’s address is `webinbox:<user>`. Without `user` it is
`claimed`, and its author is only the `name` typed. The report’s id starts with
`received_at`, so reports poll in time order; its event’s delivery id is
`webinbox:<site>:<id>`. Returns the stored record.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]

### liaise.testing.demo_registry(, github=None, webinbox=None)

A correspond channel registry of fakes: `{"github": ..., "webinbox": ...}`.

`github` defaults to an empty [`FakeGitHubChannel`](#liaise.testing.FakeGitHubChannel), `webinbox` to
correspond’s `WebInbox(store={}, blobs={})`: both in memory.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Any`](https://docs.python.org/3/library/typing.html#typing.Any)]
