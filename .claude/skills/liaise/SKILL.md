---
name: liaise
description: Use when running liaise as its owner, such as onboarding a partner or a subject, reading liaise status, holding and unholding work, looking into the unrouted queue, migrating a 0.0.x liaise config to 0.1, or explaining what a liaise label on a GitHub issue means. Triggers on "add a partner to liaise", "onboard <name> to liaise", "add a subject to liaise", "check liaise status", "what is liaise waiting on", "hold liaise", "pause liaise for <subject>", "unhold", "why is this issue unrouted", "migrate my liaise config", "what does liaise:needs-owner mean", "why hasn't liaise picked up this issue".
---

# liaise: the owner's agent skill

`liaise` runs the loop between the people who test an app and a coding agent, over GitHub issues and web inboxes. This skill is for the agent helping the owner run `liaise`, not for the agent `liaise` starts on a case. The package README has the full picture; this is what you need at the terminal.

Everything personal lives under `~/.config/liaise/` on the owner's machine, never in the package. Start read-only: `liaise subject show`, `liaise status` and `liaise run --once --dry-run` change nothing.

## Onboarding a partner in 0.1

In 0.1 a partner is a person on a subject: the repository or site they give feedback on. If the subject exists, add the person to its file; otherwise create `~/.config/liaise/subjects/<slug>.toml`.

1. **Write the subject file.** For a partner `pat`, whose reports reach `example/app` through the app's bot and through the site's web inbox, and an observer `sam`:

   ```toml
   bindings = ["github:example/app?labels=partner:pat", "webinbox:example-site"]
   workspace = { path = "~/code/example-app" }
   verify = "npm test"
   delivery = { kind = "deploy", per = "batch", command = "./deploy.sh" }

   [policy]
   default_reply_mode = "draft"
   people = { "github:pat" = "pat", "webinbox:pat" = "pat", "github:sam" = "sam" }
   roles = { pat = "partner", sam = "observer" }
   relays = ["github:example-bot"]
   claim_labels = { "partner:pat" = "pat" }
   briefs = { pat = "~/.config/liaise/briefs/pat.md" }
   ```

   - `people`: every address the person writes from, as `channel:handle`, mapped to their person id. Case does not matter.
   - `roles`: `partner` may report and have work started; `observer` may only report.
   - `relays`: accounts that file issues on others' behalf, such as the app's bot. Only a relay's claim labels count.
   - `claim_labels`: the routing label a relay adds, and the person it names. The same label on an issue anyone else opened goes to the unrouted queue.
   - `briefs`: how to talk to this person (tone, what they care about, which decisions are theirs). The agent reads it on every run of their cases. A top-level `brief` is the fallback for anyone without one. Write it at the path you give.
   - `reply_modes = { pat = "direct" }` lets one person's replies go out without the owner; the default `draft` keeps every reply for the owner to send.
   - `notify = { pat = "github:pat" }` when the handle to mention is not their first `github:` address.
2. **Create the labels.** `liaise setup <slug>` creates the subject's claim labels and every `liaise:` state label in each repository it binds. Safe to run again.
3. **Check the file.** `liaise subject show <slug>` prints it with every default applied; you want `binding problems: none` at the end.
4. **Check the plan.** `liaise run --once --dry-run --subject <slug>` shows how each report is taken in. `opened example-app-4 (... pat via relay-label)` or `via handle` is right; `unrouted: <reason>` is not (see below). The per-case lines then say what would start.
5. **Schedule it**, if it is not already: `liaise schedule install` (once for every subject).

## Reading `liaise status`

```
last_run: finished
  run_started_at: 2026-09-12T09:30:00+00:00 (1m ago)
  run_ended_at:   2026-09-12T09:30:04+00:00 (1m ago)
holds: 1
  effect:deploy: block, set by operator since 2026-09-12 08:00: release freeze
runs in flight: 1
  example-app-3-r1 (example-app-3, fresh): started 12m ago, heartbeat 20s ago
subject example-app: 3 case(s), 2/6 dispatches today
  working: example-app-3
  needs-partner: example-app-1
  needs-owner: example-app-2
unrouted: 1
  2026-09-12T09:12:00+00:00 github:someone-else on github:example/app#7: label claim by an untrusted author
drafts waiting for the operator: 1
  example-app-2 escalate to github:example/app#5: the fix needs a paid plan
digest notes: 1
  example-app-1: The export code has no tests.
```

- **Stamps.** `finished` is normal. `running` means a tick is in progress; ticks are short in 0.1, since runs are detached. `interrupted` means a tick started and its process died before it could stamp its end (a shutdown, the job unloaded). A `run_started_at` older than a few schedule intervals (2 minutes by default) means the scheduled job has stopped: check `liaise schedule status`.
- **Holds.** Each hold's scope, mode, who set it (`operator`, or `auto:<error class>` when `liaise` set it itself) and why.
- **Runs in flight.** The heartbeat is the last time the run wrote to its stream. The tick stops a run past its `timeout_minutes` (60 by default) and hands the case to the owner as `timed_out`. It signals a run's process only once it has verified, by the process's start time, that it is the one `liaise` started; one it cannot verify is never signalled, and the run is given up 10 minutes after the stop. A run whose pid names another process by now (after a reboot) is collected at once, usually as `crashed`.
- **Cases by state**, per subject, with today's dispatches against the daily cap. `needs-owner` is the owner's to-do list.
- **Unrouted.** Messages that matched a binding but could not be routed, with the reason; see "Why an issue isn't moving".
- **Drafts waiting for the operator.** Messages `liaise` kept instead of sending: draft reply mode, a gate divert (a leak, deslop, no handle to mention), an escalation, held effects, a failed send. Nothing sends them later: the owner sends what they want by hand. `liaise case show <case>` prints each draft's full text.
- **Digest notes.** `note` outcomes: what the agent noticed for the owner, never shown to the partner.

## A notification from liaise

Notifications carry no case text. A push to the owner's ntfy topic, which anyone who knows the topic's name can read, names only the subject, the case, the event (an escalation, a diverted or failed message, a failed deploy, a lost run, an error, the daily cap) and its cause (an error class, the gate filter that diverted a message, a deploy's exit code), and ends `see liaise case show <case>`. Its title is fixed text with the subject or the case id, never a person's name or address, nor an issue's title. Read the details with that command: the drafts with their text, the escalation's reason, the failed deploy's output, and the case's latest entries.

## Holds

```
liaise hold subject:example-app --reason "pat is away until Monday"
liaise hold effect:deploy --reason "release freeze"
liaise hold person:sam --mode drain
liaise hold global --mode cancel --reason "something is wrong"
liaise unhold subject:example-app
```

- **Scopes:** `global`, `processor`, `subject:<slug>`, `person:<id>`, `repo:<owner/repo>`, `checkout:<path>`, `effect:<kind>`.
- **`block`** (the default): nothing new starts; runs in flight go on and are collected; their messages and deploys wait, kept as drafts, and the case goes to `needs-owner`.
- **`drain`**: nothing new starts; runs in flight finish and their effects go out as usual. Use it before a release.
- **`cancel`**: nothing new starts; runs in flight are interrupted, then terminated after 15 seconds, and stay resumable, their cases back in `intake`; effects wait.
- `effect:deploy` holds deliveries only: runs still start and messages still go out, but a delivery waits as a draft.
- Notifications to the owner always go out, whatever the mode.
- **Automatic holds.** `liaise` holds `processor` itself after `config_error` or `auth_expired`, and lifts it once preflight passes again (it probes every 30 minutes). It holds `effect:deploy` after a deploy refused for billing, CI minutes or a 403, and that one stays until `liaise unhold effect:deploy`. It never replaces or lifts a hold the owner set.
- **Unhold sends nothing.** Messages kept while a hold stood stay as drafts, their cases in `needs-owner`.

## What each `liaise:` label means

The label is a projection of the case's state in the ledger: `liaise` sets it, the agent never does. Relabelling an issue by hand changes nothing, and the next tick overwrites it; to move a case, use `liaise case set-state` (see "Moving a case on"). Exactly one is on an issue, and `liaise` never closes an issue.

| Label | What it means | What moves it on |
|---|---|---|
| `liaise:intake` | Taken in. The partner may still be writing: the quiet window (10 minutes by default) is running, or the case was deferred. | The window closing, or `#startwork#` from the partner. A run starts, and the case is `working`. |
| `liaise:paused` | The partner wrote `#wait#`. | The partner writing `#startwork#`. |
| `liaise:working` | A run is in flight. | The run ending: its outcomes, or its error class, decide the next state. A case left `working` with no run in flight, its run lost, goes to `needs-owner`, and the owner is told once. |
| `liaise:needs-partner` | A question or a proposal went to the partner. | The partner writing again. No run starts before they do; a case adopted from 0.0.x in this state waits for them to write after the adoption. |
| `liaise:needs-owner` | Waiting on the owner: an escalation or decline, a run that failed or was lost, a start refused for the reporter's role or grade, a deploy that failed or has no command, held effects. | The owner, with `liaise case set-state <case> intake`: the case starts again once it is ready, resuming its session. |
| `liaise:deployed` | Delivered: the deploy command ran and succeeded, and the partner was asked to try it. A quiet partner is nudged once, after 3 days, unless the issue was closed. | Nothing: the case is done. A follow-up comment is recorded but starts nothing; `liaise case set-state <case> intake` reopens the work. |
| `liaise:budget` | Ready, but the subject's daily dispatch cap is used up. The partner was told once, and the owner once that day. | The next day: it starts again. |

## Why an issue isn't moving

Start with `liaise run --once --dry-run --subject <slug>`: its plan has a line per case saying what stops it. Then:

- **No case at all: it is unrouted.** `liaise status` lists it with its reason:
  - `label claim by an untrusted author`: the issue has a claim label, but its author is neither a person with a role nor one of `policy.relays`. Add the app's account to `relays`, or the author to `people` and `roles`.
  - `unresolved sender`: the author is not in `policy.people` (nor in acquaint's records) and the issue has no claim label. Add them to `people` and give them a role.
  - `no role on this subject`: the person resolves but has no entry in `policy.roles`.
  - `grade <grade> not accepted for report`: the message's authenticity is below what `policy.grades.report` accepts, such as an unsigned web-inbox report (`claimed`) where `report` was narrowed.
  - `label claims name more than one person`: two claim labels on one issue.
  - Queued messages are not retried: once the policy is fixed, have the partner report again.
  - Not in the queue either: the message matched no binding (another repository, a missing label) or is a closed issue. `liaise subject show` names a binding that could never match.
- **`needs-partner`, waiting for a reply.** After an `ask` or a `propose`, no run starts until the reporter writes again (`awaiting the partner's reply since the last run`), and then the quiet window runs again. Comments by the owner, a relay or `liaise` itself do not count.
- **A hold.** `held by <scope> (<mode>)`. `liaise status` lists the holds; an `auto:` one is `liaise`'s own (see Holds).
- **`defer_until`.** `deferred until <time>`: a `defer` outcome (24 hours), a rate limit (2 minutes, doubling up to 30), an unavailable API (5 minutes), an exhausted quota (until it resets) or a busy workspace (10 minutes). It clears itself.
- **The budget.** `still over the daily cap (6/6)`: it starts again the next day. `ready, but 1 run(s) in flight (concurrent cap 1)`: it waits for the subject's running run.
- **A workspace conflict.** `workspace_conflict: live session <name> in <path>`: a Claude Code session, probably the owner's, is working in the checkout; the case tries again in 10 minutes. `the checkout is locked by run <id>`: another run of the subject holds it.
- **Not ready yet.** `not ready (waiting, 4m to go)` is the quiet window; `paused (partner asked to wait)` is a `#wait#`.
- **`needs-owner`.** It waits on the owner. Read the case with `liaise case show <case>` (its drafts, the escalation's reason, a failed deploy's output, its latest entries), act on what it needs, then move it on with `liaise case set-state <case> intake` (see "Moving a case on"). Relabelling the issue, the 0.0.x way, changes nothing: the next tick overwrites the label. A case found `working` with no run in flight lands here too, and the owner hears `run lost`.
- **Its issue is closed.** `its issue is closed`: a case whose issue was closed is neither started nor nudged. Reopening the issue starts it again, once `liaise` reads it: a closed case's issue is read at most once an hour (`read again in <time>` on the plan line). `its issue could not be read`: the case waits for the next tick. Only reads that fail for good count (the issue not found, or not permitted): three in a row send the case to `needs-owner`, and the owner is told once. A network that is down or a login that expired never does.
- **Nothing happens at all.** An old `run_started_at` in `liaise status` means the scheduled job stopped (`liaise schedule status`); `interrupted` means its last tick died. `liaise schedule status` saying `installed (outdated: re-run liaise schedule install)` means the job was installed by 0.0.x and kills the runs its ticks start: install it again.

## Moving a case on

```
liaise case list --state needs-owner
liaise case show example-app-2
liaise case set-state example-app-2 intake --reason "brief fixed" --dry-run
liaise case set-state example-app-2 intake --reason "brief fixed"
```

- `liaise case list [--state STATE]` lists every case, or those in one state, with its conversations. It changes nothing.
- `liaise case show CASE` prints what a notification leaves out: the case's state, the reason of its last escalation, its last failed deploy with the command's output, each draft waiting for the owner with its text, and its latest entries. It changes nothing.
- `liaise case set-state CASE STATE [--reason TEXT] [--dry-run]` moves a case as the owner, recorded on the case with the reason. `intake` has the tick start the case again once it is ready, resuming its session. Any state is allowed but `working`, which only a run makes true, and a case with a run in flight is refused until that run is collected. While a tick is running it refuses too, changing nothing: run it again once the tick is done.
- The case's label follows on the next tick. `--dry-run` says what would change and writes nothing.

## Migrating from 0.0.x

```
liaise schedule uninstall        # stop the 0.0.x job while you migrate
liaise migrate-config            # print the plan; nothing is written
liaise migrate-config --apply    # write the subject files the plan printed
liaise run --once --dry-run      # check what 0.1 would do
liaise schedule install          # required after migrating
```

- `migrate-config` is a dry run by default. `--apply` creates each missing `subjects/<slug>.toml`, never overwrites one, and never touches `config.toml`, `partners/` or `briefs/`.
- **Install the schedule again after migrating.** The 0.1 launchd agent sets `AbandonProcessGroup` (the systemd unit, `KillMode=process`) so detached runs survive the tick; a job installed by 0.0.x lacks it, and launchd would kill every run when its tick exits. Until it is installed again, `liaise schedule status` says `installed (outdated: re-run liaise schedule install)`.
- Partners sharing a repository become one subject; read every `conflict:` line, since the first partner's value was kept.
- Each partner's brief moves to `policy.briefs`, so it stays theirs.
- 0.0.x also took issues a partner's login opened without the label. 0.1 does not; each `note:` line names the `?author=` binding that opts back in.
- Custom `dispatch.command` and `resume_command` templates are not carried (a `warning:` line shows what each passed), nor is a partner's `display_name`.
- On a repository's first real poll, open issues with a `liaise:` state label are adopted into cases in that state; 0.0.x sessions are not carried over, so their next run starts fresh.
