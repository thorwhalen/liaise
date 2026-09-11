# ADR 0001: the seams and decisions of liaise 0.1

- **Status:** accepted
- **Date:** 2026-09-12

## Context

`liaise` 0.0.x ran the loop between a partner and a coding agent over GitHub issues alone. Each partner was one file. The dispatched agent posted its own comments and set its own `liaise:` label, and that label was the whole state machine. `claude` ran synchronously inside the tick, with a log file per dispatch.

That shape worked for one partner on one repository, and it showed its limits as soon as it met more:

- A routing label anyone can add was both how an issue was routed and the only evidence of who it was from.
- Nothing checked a message before the partner saw it: a draft, a leaked local path or a missing mention was repaired after it was posted, if at all.
- State kept in labels could be changed by anyone and carried no history.
- A run held the tick, and its run lock, for up to an hour, and a crash could leave an issue at `working`.
- Partners sharing a repository were separate configurations that had to agree by hand.
- A report from a site's feedback form had no way in.

0.1 rebuilds the loop around correspond for channels, a local ledger for state, a detached processor, and an outbound gate. This record names the boundaries fixed before the build and the decisions taken during it.

## Seams

A seam is one keyword argument with a working default, declared only where its replacement already exists somewhere to point at. 0.1 has five.

| # | Seam | Keyword | 0.1 default | The replacement it points at |
|---|---|---|---|---|
| 1 | channels | `registry=` | correspond's channel registry: GitHub through `gh`, and the web inbox | the Discord and email adapters on correspond's roadmap |
| 2 | identity resolver | `resolver=` | `policy.people`, then `acquaint.resolve` when acquaint imports | a hosted acquaint |
| 3 | ledger store | `store=` | `dol.Jsons` under `state_dir/ledger` | a synced or remote key-value store |
| 4 | processor | `processor=` | `ClaudeHeadless`: `claude -p`, stream-JSON, detached | `EchoProcessor` (in the tests today), and a processor that hands a case to a live Claude Code session |
| 5 | workspace | `workspace=` | `SharedCheckout`: the subject's checkout, with a lock and a collision check | a git worktree per run |

## Surfaces

The command line is the only surface: `liaise`, dispatched with `cw` from one command tree in `liaise.cli`, with the seams hidden from it. An MCP server was asked about and not built. It would need no change to the core, since the command tree is the single source a later surface reads. The processor needs no surface of its own, because a run's outcomes come back as structured output.

## What is not a seam

- The state vocabulary and the label names.
- The message templates (the daily-cap message, "try it", the nudge).
- The readiness arithmetic.
- The config file format.
- The outcome vocabulary.
- The gate's filter order. `outbound_filters=` is a keyword argument, but 0.1 fixes it to the five filters.

Two dependencies were found during the build and are not seams yet. The label projection uses `liaise.github.GhCli` directly, since correspond has no label operations. Operator notification is `liaise.notify` (ntfy), as in 0.0.x.

## Decisions

### The ledger is the source of truth, and labels are a projection

A label anyone can change cannot be a state machine anyone can trust, and it keeps no history. The ledger holds each case with an append-only list of entries, dedupes channel events on their delivery id, and keeps runs, holds, the unrouted queue and cursors beside them. A dry run is the same code over an empty overlay in front of the store. The labels stay, in the 0.0.x vocabulary, because the partner and the owner read a case's state on the issue; `liaise` sets them after each tick for the cases that changed, and for any case whose state is not the one it last projected. A label changed by hand is overwritten, so the owner moves a case with `liaise case set-state`, recorded on the case like any other transition.

### A label is a claim, judged by the issue's author

Anyone who can label an issue can add `partner:pat`. So an author who resolves to a person with a role is that person whatever the labels say, and a claim label counts only when the author is a configured relay (the app filing issues for its users), at the relay's own grade. Anything else goes to the unrouted queue with its reason. The check is on the issue's author, not on whoever applied the label, because the API the channel uses does not say who applied a label; that waits on correspond (thorwhalen/correspond#23).

### Outcomes come back through `--json-schema`

The agent reports through structured output validated against a closed vocabulary (`ask reply escalate propose deliver decline defer note`) and never writes to a channel. `liaise` can then plan, gate and record every effect, instead of trusting the agent's own posts or parsing prose. A `decline` is planned as an `escalate`, so a refusal reaches the owner before the partner.

### The gate's order is fixed, and it fails closed

The order is reply mode, leak scan, writing card, deslop, then the notification guarantee.

- A draft is diverted before anything else looks at it.
- The deterministic leak scan runs before the acquaint filters.
- The mention, the gate's only rewrite, comes last, so every earlier filter judges the text the agent wrote.

A filter that raises, or answers anything but pass or divert, diverts the message: a message not sent can still be sent, and one sent cannot be taken back. acquaint is optional, and without it the two filters that need it add a note instead.

### The processor is detached, and its heartbeat is the stream file's mtime

A synchronous run held the tick for as long as it ran. A detached run lets one tick collect, start and label while other runs go on, and it survives the tick: the scheduled job sets launchd's `AbandonProcessGroup`, or systemd's `KillMode=process`. `claude` writes its stream-JSON one event per line, so the file's modification time is a heartbeat at no cost: no watcher process and no hooks. The ledger records a run as running until a tick collects it, so a run that ended at once is still collected.

### An error taxonomy with fixed actions

0.0.x handed every failed run to the owner. A quota reset or an overloaded API is not the owner's problem and must not use up the daily cap, while an expired login must stop every run rather than fail each one. One table maps each class to its next state, auto-hold, notification, deferral and whether it counts:

- `config_error` holds the processor.
- `auth_expired` goes back to `intake`, holds the processor until preflight passes again, and does not count.
- `quota_exhausted` defers until the reset.
- `rate_limited` and `unavailable` defer for minutes and stay silent.
- `policy_refusal`, `budget_exceeded`, `timed_out`, `crashed` and `needs_human` go to `needs-owner`.
- `workspace_conflict` defers for 10 minutes.
- `effect_blocked` holds `effect:deploy`.

Any exception a processor method raises is `crashed`, never the end of the tick.

### Three hold modes

The owner needs to say three different things:

- "start nothing new" (`block`, whose effects wait);
- "let what is running finish" (`drain`, whose effects go out);
- "stop now" (`cancel`, which interrupts running runs and keeps them resumable).

Each mode applies to any scope, down to one kind of effect such as `effect:deploy` during a billing problem. Notifications to the owner always go out. When several holds apply, the most specific one is reported. The tick's own automatic holds never replace or lift an owner's.

### The collision check reads Claude Code's session registry

A subject's checkout is often the owner's own working copy, and a run started beside a live interactive session would trample it. Claude Code records each live session, with its pid and working directory, under `~/.claude/sessions`. `liaise` reads those records and defers the case while a live session other than its own runs works in the checkout. That catches the collision without asking the owner to set a hold every time they open the repository. A lock file keeps two `liaise` runs out of one checkout.

### Migration groups partners by repository, with per-person briefs

A subject is what the feedback is about, so partners sharing a repository become one subject with several people. A dry run against a real configuration showed that a single `brief` per subject would drop one partner's brief, so each partner's brief moves to `policy.briefs`, and a run reads its reporter's. The migration does not keep matching issues by author: 0.0.x did, and picked up issues never meant for `liaise`, so in 0.1 author matching is an opt-in binding the plan names. `migrate-config` is a dry run by default, never overwrites a file, and leaves the 0.0.x files as they were.

### The permission mode stays `auto`

Runs are unattended, so a mode that waits for approval would stall them. `auto` lets a headless run go ahead with what is judged safe and refuse the rest, and a refusal surfaces as a denied permission, which classifies as `needs_human`. It does this without the blanket approval of bypassing permissions. It was the 0.0.x default. A subject can set another mode under `[processor]`, and a resumed run uses the same mode as the run it continues.

## Known limitations

- Held effects are not replayed after unhold: they stay on the case as drafts for the owner to send.
- Detecting an expired login before a run depends on `claude auth status` exiting non-zero.
- A label claim is judged by the issue's author, pending who-applied-the-label support in correspond (thorwhalen/correspond#23).
- GitHub binding refs are lower-cased when a subject loads, a workaround for correspond comparing them case-sensitively (thorwhalen/correspond#24).
- On Windows, a `.cmd` shim for `claude` can mangle the quoting of `--json-schema`.
- A case in `needs-owner` or `deployed` never starts again on its own: the owner moves it on with `liaise case set-state`. Relabelling the issue changes nothing, and the next tick overwrites the label, since labels are projections of the ledger.
- A case whose GitHub issue is closed, but which is otherwise ready to start, has its issue read again on every tick, so that a reopening is noticed.

## Next steps

These were named when 0.1 was scoped. They are not cuts from it.

- **A fresh-session reviewer:** an independent session reviews a run's outcomes before they go out.
- **Retry and dead-letter handling:** a `stuck` state, and `liaise retry`, for cases that keep failing. A `stuck` state would change the label vocabulary, which 0.1 keeps as 0.0.x had it, so it brings a new label that every subject's repositories must be set up with.
- **A watcher process, or Claude Code's `SessionEnd` and `StopFailure` hooks:** these would collect a run the moment it ends, not on the next tick.
- **Triage** (issue #19). The seam is in 0.1, `run_once(..., triage=)`, which groups and orders a subject's ready cases before they start; nothing implements it yet.
- **A webhook listener:** a GitHub event would start a tick, instead of the schedule.
- **Candidate delivery:** a preview the partner approves (`approve_candidate`) before it ships. It needs a branch of its own in the tick's `_execute`, which knows only `pr_only` and a deploy, per batch or per issue.
- **An MCP surface** over the same command tree.
- **Upstream in correspond:** who applied a label (thorwhalen/correspond#23), and case-insensitive binding refs (thorwhalen/correspond#24).
