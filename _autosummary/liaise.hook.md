# liaise.hook

The Claude Code hook: every `gh` or `correspond` write from any session is vetted first (discussion 32, §5.8).

`liaise hook install` adds two hooks to the user’s Claude Code settings, each running
`liaise vet --hook`:

- **PreToolUse**, on `Bash` and on correspond’s MCP write tools. [`pre_tool_use()`](#liaise.hook.pre_tool_use)
  reads the hook’s JSON, finds each write in it ([`writes_in()`](#liaise.hook.writes_in)), vets it
  ([`liaise.vet.vet()`](liaise.vet.md#liaise.vet.vet), with the provenance unknown) and answers `deny` with the
  reasons for a block and `ask` with the reasons for anything for the operator. A write
  it cannot read (a body in a file that is not there, a body the shell computes, a `gh`
  command behind `eval` or `xargs`) is `ask` with the reason, never let through.
- **PostToolUse**, on the same tools. When a command the hook answered `ask` ran, the
  operator said yes: [`post_tool_use()`](#liaise.hook.post_tool_use) records that in the ledger as an override
  ([`liaise.ledger.Ledger.add_override()`](liaise.ledger.md#liaise.ledger.Ledger.add_override)), never the text.

**The hook only tightens.** A write the gate would send, and every command that is not a
write, gets no answer at all: the hook prints nothing and exits 0, so the operator’s own
permission rules decide, as they did before the hook. Claude Code’s `allow` would skip
the operator’s prompt, which is new outbound behaviour, so the hook never gives it. A
`delay` is `ask`, since the write happens at once and nothing can hold it.

**The grammar is a fixed table** ([`GH_WRITES`](#liaise.hook.GH_WRITES), not a seam): `gh issue
comment|create|edit`, `gh pr comment|create|edit|review`, `gh api` writes to issues,
comments, pulls, discussions and GraphQL mutations, `correspond send|edit`, and the
correspond MCP tools `send` and `edit`. Bodies come from `--body`/`-b`,
`--body-file`/`-F` (a regular file; `-` is the command’s one heredoc, or
`cat <<'EOF' |` before it), `-f body=…`, `-F body=@file` and `--input`.

**Only plain commands are read** ([`writes_in()`](#liaise.hook.writes_in)). A command that names `gh` or
`correspond` is read when it is a list of `gh`/`correspond` commands, a
`cat <<'EOF' |` feeding one, or a gh read piped into a filter. Anything else in it
(another program, `cd`, a `$` or a backtick, an assignment such as `GH_REPO=…`, a
subshell or compound command, a redirection but `2>&1` and `>/dev/null`, a second
heredoc, a cluster of short flags, a `gh` alias or extension, a `gh` command outside
the table that changes something) is `ask`: an independent review showed that each
shell construct the hook tries to see through is one more way around it. A `GH_REPO`
already exported in the session’s shell is not visible to the hook, which then judges
the checkout’s repository.

### Module Attributes

| [`ALLOW`](#liaise.hook.ALLOW)               | What the hook answers, least restrictive first.                                                                                                              |
|----------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`ASK`](#liaise.hook.ASK)                 | What the hook answers, least restrictive first.                                                                                                              |
| [`DENY`](#liaise.hook.DENY)                | What the hook answers, least restrictive first.                                                                                                              |
| [`HOOK_COMMAND`](#liaise.hook.HOOK_COMMAND)        | The command both hooks run.                                                                                                                                  |
| [`HOOK_MATCHER`](#liaise.hook.HOOK_MATCHER)        | Bash, and correspond's MCP write tools under any server name that holds "correspond".                                                                        |
| [`DFLT_SETTINGS`](#liaise.hook.DFLT_SETTINGS)       | The user's Claude Code settings, where `liaise hook install` writes.                                                                                         |
| [`PENDING_TTL`](#liaise.hook.PENDING_TTL)         | How long a pending ask waits for its tool to run before it is pruned (an answer of no leaves one behind).                                                    |
| [`UNKNOWN_REF`](#liaise.hook.UNKNOWN_REF)         | The ref a write goes to when the command does not say and the checkout cannot tell: its audience is unknown, which resolves to public.                       |
| [`MAX_BODY_FILE_BYTES`](#liaise.hook.MAX_BODY_FILE_BYTES) | The largest body file the hook reads; a larger one is ask.                                                                                                   |
| [`NO_BODY`](#liaise.hook.NO_BODY)             | What the hook answers when it cannot read a write's body.                                                                                                    |
| [`GH_BODY_FLAGS`](#liaise.hook.GH_BODY_FLAGS)       | The flags of a `gh` command that carry text to someone.                                                                                                      |
| [`GH_READ_VERBS`](#liaise.hook.GH_READ_VERBS)       | `gh` verbs that only read, whatever flags they take (`gh run list -b main`).                                                                                 |
| [`GH_QUIET_VERBS`](#liaise.hook.GH_QUIET_VERBS)      | `gh` verbs that change nothing anyone reads (a local checkout, a rerun, a login).                                                                            |
| [`GH_GROUPS`](#liaise.hook.GH_GROUPS)           | `gh`'s own command groups; anything else is an alias or an extension.                                                                                        |
| [`GH_SINGLE_COMMANDS`](#liaise.hook.GH_SINGLE_COMMANDS)  | `gh` commands that are one word and read.                                                                                                                    |
| [`GH_PUBLISHES`](#liaise.hook.GH_PUBLISHES)        | `gh <group> <verb>` commands that publish files or text the hook does not read.                                                                              |
| [`GH_WRITES`](#liaise.hook.GH_WRITES)           | The `gh <group> <verb>` commands that write text, and whether the first positional is the issue or pull request (`True`) or nothing (`False`: it opens one). |
| [`INERT_FILTERS`](#liaise.hook.INERT_FILTERS)       | they print, and run nothing.                                                                                                                                 |

### Functions

| [`hook_command`](#liaise.hook.hook_command)()                                   | `liaise vet --hook` with liaise's absolute path when it is on `PATH`.                             |
|---------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| [`hook_status`](#liaise.hook.hook_status)([settings])                          | Each hook event's state in `settings`: `installed`, `outdated` or `missing`.                      |
| [`install_hooks`](#liaise.hook.install_hooks)([settings, command])               | Write the PreToolUse and PostToolUse hooks into `settings`, keeping every other hook and setting. |
| [`judge_writes`](#liaise.hook.judge_writes)(writes, \*[, vet_fn, root])         | The hook's answer for `writes`: the most restrictive of each one's, with every reason.            |
| [`pending_key`](#liaise.hook.pending_key)(payload)                             | The key a pending ask is kept under: the tool-use id, else a hash of the call.                    |
| [`post_tool_use`](#liaise.hook.post_tool_use)(payload, \*, ledger[, now])        | Record the override when a tool call the hook answered `ask` ran; return what was recorded.       |
| [`pre_tool_use`](#liaise.hook.pre_tool_use)(payload, \*[, ledger, vet_fn, ...]) | The PreToolUse answer for `payload`: Claude Code's JSON for `ask` or `deny`, else None.           |
| [`repo_of_checkout`](#liaise.hook.repo_of_checkout)(cwd)                            | `owner/repo` of the GitHub remote `origin` of the checkout at `cwd`, or None.                     |
| [`run_hook`](#liaise.hook.run_hook)(raw, \*[, ledger_store, vet_fn, ...])   | What `liaise vet --hook` prints for the hook JSON `raw`: an answer, or nothing.                   |
| [`uninstall_hooks`](#liaise.hook.uninstall_hooks)([settings])                      | Remove liaise's hooks from `settings`, keeping everything else.                                   |
| [`writes_in`](#liaise.hook.writes_in)(command, \*[, cwd, repo_of])           | Every `gh` or `correspond` write in the shell `command`, each with its text or its problem.       |
| [`writes_of`](#liaise.hook.writes_of)(payload, \*[, repo_of])                | The writes a hook payload's tool call makes: a Bash command's, or a correspond MCP tool's.        |

### Classes

| [`Answer`](#liaise.hook.Answer)(decision[, reasons, records, unread])      | The hook's answer: `decision` (one of `DECISIONS`) and the reasons for it.       |
|----------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------|
| [`Write`](#liaise.hook.Write)(what[, ref, text, title, cc, bcc, problem]) | One write the hook found: where it goes, and its text, or why it cannot be read. |

### liaise.hook.ALLOW *= 'allow'*

What the hook answers, least restrictive first. `allow` is never printed.

### liaise.hook.ASK *= 'ask'*

What the hook answers, least restrictive first. `allow` is never printed.

### *class* liaise.hook.Answer(decision, reasons=(), records=(), unread=0)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

The hook’s answer: `decision` (one of `DECISIONS`) and the reasons for it.

#### unread *: [int](https://docs.python.org/3/builtins/functions.html#int)* *= 0*

How many writes could not be read or vetted, and so went to the operator unvetted.

### liaise.hook.DENY *= 'deny'*

What the hook answers, least restrictive first. `allow` is never printed.

### liaise.hook.DFLT_SETTINGS *= PosixPath('~/.claude/settings.json')*

The user’s Claude Code settings, where `liaise hook install` writes.

### liaise.hook.GH_BODY_FLAGS *= frozenset({'--body', '--body-file', '--comment', '--field', '--input', '--message', '--notes', '--notes-file', '--raw-field', '--title', '-F', '-b', '-c', '-f', '-m', '-n', '-t'})*

The flags of a `gh` command that carry text to someone.

### liaise.hook.GH_GROUPS *= frozenset({'alias', 'api', 'attestation', 'auth', 'browse', 'cache', 'codespace', 'completion', 'config', 'extension', 'gist', 'gpg-key', 'help', 'issue', 'label', 'org', 'pr', 'project', 'release', 'repo', 'ruleset', 'run', 'search', 'secret', 'ssh-key', 'status', 'variable', 'version', 'workflow'})*

`gh`’s own command groups; anything else is an alias or an extension.

### liaise.hook.GH_PUBLISHES *= frozenset({('gist', 'create'), ('gist', 'edit'), ('release', 'create'), ('release', 'edit'), ('release', 'upload')})*

`gh <group> <verb>` commands that publish files or text the hook does not read.

### liaise.hook.GH_QUIET_VERBS *= frozenset({'cancel', 'checkout', 'clone', 'login', 'logout', 'refresh', 'rerun', 'setup-git', 'switch', 'token'})*

`gh` verbs that change nothing anyone reads (a local checkout, a rerun, a login).

### liaise.hook.GH_READ_VERBS *= frozenset({'browse', 'checks', 'diff', 'download', 'get', 'list', 'search', 'status', 'view', 'watch'})*

`gh` verbs that only read, whatever flags they take (`gh run list -b main`).

### liaise.hook.GH_SINGLE_COMMANDS *= frozenset({'browse', 'completion', 'help', 'status', 'version'})*

`gh` commands that are one word and read.

### liaise.hook.GH_WRITES *: [Mapping](https://docs.python.org/3/library/collections.abc.html#collections.abc.Mapping)[[tuple](https://docs.python.org/3/builtins/stdtypes.html#tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str), [str](https://docs.python.org/3/builtins/stdtypes.html#str)], [bool](https://docs.python.org/3/builtins/functions.html#bool)]* *= {('issue', 'comment'): True, ('issue', 'create'): False, ('issue', 'edit'): True, ('pr', 'comment'): True, ('pr', 'create'): False, ('pr', 'edit'): True, ('pr', 'review'): True}*

The `gh <group> <verb>` commands that write text, and whether the first positional is
the issue or pull request (`True`) or nothing (`False`: it opens one).

### liaise.hook.HOOK_COMMAND *= 'liaise vet --hook'*

The command both hooks run.

### liaise.hook.HOOK_MATCHER *= 'Bash|mcp_\_.\*correspond.\*_\_(send|edit)'*

Bash, and correspond’s MCP write tools under any server name
that holds “correspond”.

* **Type:**
  The tools the hooks watch

### liaise.hook.INERT_FILTERS *= frozenset({'cat', 'column', 'cut', 'egrep', 'fgrep', 'grep', 'head', 'jq', 'less', 'sort', 'tail', 'tee', 'tr', 'uniq', 'wc'})*

they print, and run nothing.

* **Type:**
  Programs a gh read may be piped into

### liaise.hook.MAX_BODY_FILE_BYTES *= 1000000*

The largest body file the hook reads; a larger one is ask.

### liaise.hook.NO_BODY *= 'could not read the body'*

What the hook answers when it cannot read a write’s body.

### liaise.hook.PENDING_TTL *= datetime.timedelta(days=1)*

How long a pending ask waits for its tool to run before it is pruned (an answer of no
leaves one behind).

### liaise.hook.UNKNOWN_REF *= 'unknown:destination'*

The ref a write goes to when the command does not say and the checkout cannot tell:
its audience is unknown, which resolves to public.

### *class* liaise.hook.Write(what, ref='unknown:destination', text=None, title=None, cc=(), bcc=(), problem=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

One write the hook found: where it goes, and its text, or why it cannot be read.

`what` names the command in words (`gh issue comment`). `problem` set means the
hook answers `ask` with it, without vetting.

### liaise.hook.hook_command()

`liaise vet --hook` with liaise’s absolute path when it is on `PATH`.

The hook runs in a shell whose `PATH` may not hold liaise; a hook command that is not
found fails without blocking, so every write would go through unvetted.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.hook.hook_status(settings=PosixPath('~/.claude/settings.json'))

Each hook event’s state in `settings`: `installed`, `outdated` or `missing`.

* **Return type:**
  [`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.hook.install_hooks(settings=PosixPath('~/.claude/settings.json'), , command=None)

Write the PreToolUse and PostToolUse hooks into `settings`, keeping every other hook and setting.

Idempotent: liaise’s own hook commands are replaced, never doubled, and a hook someone
else put in the same entry stays. Returns what it did.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.hook.judge_writes(writes, \*, vet_fn=<function vet>, root=None)

The hook’s answer for `writes`: the most restrictive of each one’s, with every reason.

A write with a problem is `ask`. Each other is vetted with the provenance unknown; a
route of `send` is `allow`, `draft` (a `delay` included) `ask`, `block`
`deny`. Vetting that raises is `ask` with the error.

* **Return type:**
  [`Answer`](#liaise.hook.Answer)

### liaise.hook.pending_key(payload)

The key a pending ask is kept under: the tool-use id, else a hash of the call.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> pending_key({"tool_use_id": "toolu_1"})
'toolu_1'
```

### liaise.hook.post_tool_use(payload, , ledger, now=None)

Record the override when a tool call the hook answered `ask` ran; return what was recorded.

The tool ran, so the operator said yes. The entry holds what the pending ask held (the
refs, flows, rules and hashes), who and when, never the text.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)]

### liaise.hook.pre_tool_use(payload, \*, ledger=None, vet_fn=<function vet>, root=None, repo_of=<function repo_of_checkout>, now=None)

The PreToolUse answer for `payload`: Claude Code’s JSON for `ask` or `deny`, else None.

An `ask` is kept as pending in `ledger` (when there is one), so the PostToolUse hook
can tell the operator said yes. Nothing is printed for `allow`.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`dict`](https://docs.python.org/3/builtins/stdtypes.html#dict)]

### liaise.hook.repo_of_checkout(cwd)

`owner/repo` of the GitHub remote `origin` of the checkout at `cwd`, or None.

* **Return type:**
  [`Optional`](https://docs.python.org/3/library/typing.html#typing.Optional)[[`str`](https://docs.python.org/3/builtins/stdtypes.html#str)]

### liaise.hook.run_hook(raw, \*, ledger_store=None, vet_fn=<function vet>, root=None, repo_of=<function repo_of_checkout>, now=None)

What `liaise vet --hook` prints for the hook JSON `raw`: an answer, or nothing.

Fails closed: input that is not a hook payload, or anything that goes wrong on the way,
is `ask` with the reason on a PreToolUse event (and on any event the hook cannot
name). A PostToolUse event never answers; its failure to record is silent.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.hook.uninstall_hooks(settings=PosixPath('~/.claude/settings.json'))

Remove liaise’s hooks from `settings`, keeping everything else.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.hook.writes_in(command, \*, cwd=None, repo_of=<function repo_of_checkout>)

Every `gh` or `correspond` write in the shell `command`, each with its text or its problem.

A command that names neither is no write at all. One that names either is read only
when it is plain: a list of `gh` and `correspond` commands (joined by `&&`,
`||`, `;`, `|` or newlines), a `cat <<EOF |` feeding one of them, and a gh read
piped into a filter ([`INERT_FILTERS`](#liaise.hook.INERT_FILTERS)). Anything else in such a command (another
program, a `$`, a backtick, a subshell or a compound command, an assignment, a
redirection, more than one heredoc) is one problem write, since the hook cannot tell
what the shell will run, where, or with what text: `ask`. The shell is not parsed
beyond that, on purpose.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Write`](#liaise.hook.Write)]

### liaise.hook.writes_of(payload, \*, repo_of=<function repo_of_checkout>)

The writes a hook payload’s tool call makes: a Bash command’s, or a correspond MCP tool’s.

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Write`](#liaise.hook.Write)]
