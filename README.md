# liaise

`liaise` runs the loop between the people who test an app and the coding agent that maintains it. A partner reports what they notice where they already are, in a GitHub issue or through the site's web inbox; `liaise` keeps it as a case, waits until they have finished writing, starts a coding agent on it, and carries what the agent reports back to them through checks that hold back drafts, leaks and replies nobody would be notified of. Nobody has to learn a tool or a syntax to be part of the loop, and you, the operator, hear only about what needs you.

## Quick start

```
pip install liaise
mkdir -p ~/.config/liaise/subjects ~/.config/liaise/briefs
cat > ~/.config/liaise/config.toml <<'EOF'
owner_login = "you"
state_dir = "~/.local/share/liaise"
EOF
cat > ~/.config/liaise/briefs/pat.md <<'EOF'
Pat tests the app on a phone and is not technical. Keep replies short and plain, one question at a time.
EOF
cat > ~/.config/liaise/subjects/example-app.toml <<'EOF'
bindings = ["github:example/app?labels=partner:pat"]
workspace = { path = "~/code/example-app" }
brief = "~/.config/liaise/briefs/pat.md"

[policy]
people = { "github:pat" = "pat" }
roles = { pat = "partner" }
relays = ["github:example-bot"]
claim_labels = { "partner:pat" = "pat" }
EOF
liaise subject show example-app
liaise run --once --dry-run
```

Everything above is read-only. `liaise subject show` prints the subject as `liaise` reads it, every default applied, and names any binding that could never match. `liaise run --once --dry-run` prints what one tick would do (what it takes in, which finished runs it collects and what their outcomes would send, which cases it would start) and changes nothing: nothing is sent, labelled, started or written.

When the plan looks right, `liaise setup example-app` creates the labels in the repository, `liaise run --once` does what the plan said, and `liaise schedule install` runs it every 2 minutes.

That is one subject: the fictional `example/app`, whose app files issues as `example-bot` with a `partner:pat` label for its tester `pat`. Replies start in `draft` mode, each kept for you to send, until the subject sets `default_reply_mode = "direct"` under `[policy]`. Coming from 0.0.x? Read [Migrating from 0.0.x](#migrating-from-00x) first.

## Concepts

### Subjects and bindings

A subject is one body of work, an app or a site, configured in `~/.config/liaise/subjects/<slug>.toml`. Its `bindings` say which conversations belong to it, as [correspond](https://pypi.org/project/correspond/) binding patterns: `github:example/app?labels=partner:pat` is every issue in `example/app` that carries the `partner:pat` label, and `webinbox:example-site` is every report sent through that site's web inbox. The rest of the file says where the work happens (`workspace`), how it is checked and shipped (`verify`, `delivery`), and its `[policy]`: who is who, what each person may do, and how replies go out. Every table has defaults, so a minimal subject needs only `bindings`, `policy.people` and `policy.roles`. Nothing about a partner lives in this package: identities, repositories, briefs and commands are all in these files.

**What polling costs.** correspond keeps a cursor per conversation, stored in the ledger, so each tick asks only for what changed since the last one. For a GitHub repository that is two REST requests per tick, the issue comments and the issues updated since the cursor, each a page of up to 100 items (a further page only when more than that changed), plus one request per `liaise` process to learn which account `gh` is logged in as. A tick every 2 minutes, the default schedule, comes to about 60 requests an hour per repository, far below GitHub's 5,000 an hour for a signed-in account. This answers issue #12. Before a repository's first poll, `liaise` also reads its open issues once, since a first poll looks back only a day. A web inbox is read from its own store and costs no request.

**One subject per conversation.** Bindings that name the same conversation are polled once. Two subjects may not bind the same conversation: correspond keeps one cursor for it, so the first subject would take every event, and `liaise` refuses that configuration when it loads.

A subject file with every setting, most of them at their defaults:

```toml
display_name = "Example app"
bindings = ["github:example/app?labels=partner:pat", "webinbox:example-site"]
workspace = { kind = "shared", path = "~/code/example-app" }
brief = "~/.config/liaise/briefs/example-app.md"   # for anyone without a brief of their own
verify = "npm test"
delivery = { kind = "deploy", per = "batch", command = "./deploy.sh" }   # kind: deploy or pr_only
label_prefix = "liaise:"

[policy]
default_reply_mode = "draft"                  # direct or draft
reply_modes = { pat = "direct" }              # one person's own mode
people = { "github:pat" = "pat", "webinbox:pat" = "pat" }
roles = { pat = "partner" }
relays = ["github:example-bot"]               # authors whose claim labels count
claim_labels = { "partner:pat" = "pat" }      # routing label to the person it claims
briefs = { pat = "~/.config/liaise/briefs/pat.md" }
notify = { pat = "github:pat" }               # whom to mention; default: the person's first handle
leak_terms = []                               # internal words the leak scan diverts on
public_channels = ["github"]                  # channels the leak scan applies to
deployed_nudge_days = 3

[policy.permissions]
partner = ["report", "request_work", "approve_candidate"]
observer = ["report"]

[policy.grades]
report = ["claimed", "platform", "domain", "bound", "crypto"]
request_work = ["platform", "domain", "bound", "crypto"]
approve_candidate = ["platform", "domain", "bound", "crypto"]

[policy.readiness]
quiet_minutes = 10
go_minutes = 2
markers = { go = "#startwork#", wait = "#wait#" }

[policy.escalate]
money_usd = 50.0
max_scope = "about a day of work"

[policy.budget]
concurrent = 1
timeout_minutes = 60
max_turns = 200
daily_dispatches = 6

[processor]
permission_mode = "auto"
```

A deploy's `per` is `batch` or `issue`; a subject file with any other value does not load. With `per = "batch"`, the agent lands each change and `liaise` runs the deploy command once per tick, after every finished run is collected. With `per = "issue"`, `liaise` runs it for each case as soon as that case's outcomes are carried out. Either way the agent never deploys, and the partner hears "it's live" only once the command has run and succeeded: a failed or empty command sends the case to `needs-owner` and tells you. `pr_only` runs no command and stops at a pull request.

### The case ledger

A conversation a binding takes in becomes a case (`example-app-1`), kept in the ledger: one JSON file per key under `state_dir/ledger`, behind the `store` seam. A case holds its conversations, its reporter, its state, an append-only history (messages, transitions, outcomes, gate decisions, runs, holds, label changes, digest notes), the drafts waiting for you, and the session id its next run resumes. The ledger also keeps the delivery ids already taken in, so nothing is taken in twice, and the run records, holds, unrouted queue, cursors and daily counters. A dry run puts an empty overlay in front of the store, so every step reads real state and every write vanishes.

**The ledger is the source of truth.** The `liaise:` label on a GitHub issue is a projection of its case's state, set at the end of each tick for the cases that changed. The vocabulary is the one 0.0.x used:

| Label | The case |
|---|---|
| `liaise:intake` | is taken in; the partner may still be writing |
| `liaise:paused` | waits because the partner wrote `#wait#` |
| `liaise:working` | has a run in flight |
| `liaise:needs-partner` | waits for the partner to answer |
| `liaise:needs-owner` | waits for you: an escalation, a failed run, a refused start, held effects |
| `liaise:deployed` | is delivered, and the partner was asked to try it |
| `liaise:budget` | is ready, but today's dispatch cap is used up |

Exactly one state label is on an issue. Labels are projections of the ledger: relabelling an issue by hand changes nothing, and the next tick overwrites the label. To move a case, use `liaise case set-state`, and its label follows on the next tick. `liaise` never closes an issue. A case whose issue someone closed is neither started nor nudged, and starts again once `liaise` sees the issue reopened, which is within the hour: it reads a closed case's issue again at most once an hour.

**Readiness.** A case is ready once its reporter has been quiet for `quiet_minutes` (10), so a request written across three comments is not picked up mid-sentence. `#startwork#` in their text makes it ready `go_minutes` (2) later, and `#wait#` pauses it until their next `#startwork#`. Only the reporter's own messages move this clock: not yours, not a relay's, not `liaise`'s. A case in `needs-partner` starts again only once its reporter writes after its last run; one adopted from 0.0.x in `needs-partner` waits the same way, for them to write after the adoption.

### Identity and access

**Who wrote it.** A message's author is a channel address, such as `github:pat`. The resolver (the `resolver=` seam) maps the address to a person: `policy.people` first, compared without regard to case, then acquaint's people records when acquaint is installed (`pip install liaise[people]`), trusted only when exactly one person matches. Anything else resolves to nobody, and a resolver error never stops a tick.

**How sure.** correspond grades each message's authenticity, weakest first: `claimed`, `platform`, `domain`, `bound`, `crypto`. A GitHub message is `platform`, since GitHub authenticated the account. A web-inbox report is `bound` when the site signs who sent it and `claimed` when it does not; one whose signature fails is `forged`, which no permission accepts.

**What they may do.** `policy.roles` gives each person a role, `policy.permissions` gives each role its permissions, and `policy.grades` gives each permission the grades it accepts. The defaults:

| Permission | Needed when | Grades accepted | Roles that hold it |
|---|---|---|---|
| `report` | a message opens a case or adds to one | `claimed` and up | `partner`, `observer` |
| `request_work` | the tick starts a run on the reporter's case, judged on their latest message | `platform` and up | `partner` |
| `approve_candidate` | reserved for candidate delivery, which 0.1 does not build | `platform` and up | `partner` |

A reporter who may not `request_work` gets no run: the case goes to `needs-owner`, and you are told.

**A label is a claim.** Anyone who can label an issue can add `partner:pat`, so a routing label never proves who wrote it. An issue whose author resolves to a person with a role is that person's, whatever its labels. Otherwise a label in `policy.claim_labels` counts only when the author is one of `policy.relays`, such as the app that files issues for its users (`github:example-bot`): the message is then attributed to the person the label names, at the relay's own grade. A claim label from any other author is refused as `label claim by an untrusted author`.

**The unrouted queue.** A message that matches a binding but fails any of this (an unresolved sender, a person with no role, a refused claim, labels claiming two people, a grade not accepted) opens no case and adds nothing to one. It goes to the unrouted queue with its reason, and `liaise status` lists the latest. Queued messages are not retried: a fixed policy applies to what arrives next. A message that matches no binding is not the subject's, and is ignored. Comments by a relay, or by the account `gh` is logged in as, are recorded on the case but never count as the partner's.

### Outcomes and the outbound gate

**The agent never posts or labels.** Its operating rules forbid posting comments, setting labels and opening issues. It reports only through the structured result its run ends with, a `summary` and one or more outcomes from a closed vocabulary, and `liaise` plans what each one does:

| Outcome | Needs | What `liaise` does |
|---|---|---|
| `ask` | `questions` | sends the text and the numbered questions, each with its default, then `needs-partner` |
| `reply` | `text` | sends it |
| `propose` | `text` | sends it, then `needs-partner` |
| `deliver` | `text` | delivers (the batch deploy, or the pull request), sends the text with a "try it" line, then `deployed` |
| `escalate` | `reason` | keeps `text` as a draft for you, notifies you, then `needs-owner` |
| `decline` | `reason` | the same as `escalate`: you decide |
| `defer` | `reason` | puts the case aside for 24 hours, in `intake` |
| `note` | `text` | a line in your digest, never shown to the partner |

When a run plans no state of its own, its case moves to `needs-partner` if a message was sent and to `needs-owner` if not.

**The gate.** Every message passes five filters, always in this order, before it is sent:

1. **Reply mode.** In `draft` mode (the default, or one person's `policy.reply_modes`), nothing goes out without you.
2. **Leak scan.** On a public channel (`policy.public_channels`, GitHub by default), a message holding any of these is diverted: an absolute local path (a home directory on macOS, Linux or Windows, its backslashes single or doubled as JSON writes them, a Windows home through a WSL mount, or a macOS temporary directory), a path ending in `.env`, an email address, a private key, a token shape (`ghp_`, `github_pat_`, `sk-`, `AKIA`, `hf_`, or `xoxb-` and its siblings, found even when wrapped across lines), or one of `policy.leak_terms`. It never redacts, and its notes say where the leak is, never what it is.
3. **Writing card.** A note with the recipient's acquaint writing card, kept in the ledger for the next run.
4. **Deslop.** acquaint's style lint diverts a message that reads as machine-written to its recipient.
5. **Notification guarantee.** On GitHub, the message starts with `@<login>`, added when missing: GitHub notifies only the people a comment mentions, and an issue an app filed subscribes its partner to nothing. A recipient with no GitHub handle is diverted.

The first filter that diverts ends the gate: the message is not sent, it is kept on the case as a draft, and you are told. The gate fails closed: a filter that raises, or answers anything but pass or divert, diverts too. acquaint is optional: without it, or for a person it does not know, the writing card and deslop filters add a note and let the message through. Messages go out through correspond, and one that fails to send is kept as a draft, recorded, and reported to you.

### The processor

`ClaudeHeadless` (the `processor=` seam) runs the `claude` CLI headless: `claude -p` pointing at a prompt file, `--output-format stream-json --verbose`, the subject's `--permission-mode` (`auto` by default, and the same on a resume), `--json-schema` for the outcomes, and `--session-id` for a new session or `--resume` for the case's stored one. The run is spawned detached, in its own process group, so the tick returns at once and the run outlives it. Its environment drops `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`, so it runs on the account `claude` is logged in to rather than billing a key. Before each start, preflight checks that `claude` and the checkout exist and that `claude auth status` passes.

**Stamps.** Each run keeps `prompt.md`, `stream.jsonl`, `stderr.log` and `record.json` in `state_dir/runs/<run_id>/`. The ledger stamps its start. Its heartbeat is the modification time of `stream.jsonl`, which `claude` writes one event at a time, so no watcher process is needed. It has ended once the stream holds a `result` event or its process is gone, and `liaise status` shows each run in flight with its heartbeat's age. Collecting a run reads its outcomes, its summary, its token usage and its estimated cost in USD, and the cost goes on the run's ledger entry. The prompt is the packaged operating rules, the reporter's brief, the case's conversations, the outcome vocabulary, the verify and delivery commands, and the budget.

**The run's own budget.** The timeout is enforced from outside: past `timeout_minutes`, the tick stops the run and classifies it `timed_out`. `liaise` signals a run's process only after verifying, by its start time, that it is the process `liaise` started, since after a reboot its pid can name another; a process it cannot verify is never signalled, and its run is given up 10 minutes after the stop was sent. A run that ended inside its `timeout_minutes` keeps its outcomes, however late a tick first collects it. The turn cap is a line in the prompt, since `claude -p` has no flag for it.

**How a run ended.** The tick classifies each finished run from its stream, structured fields before strings, the first match winning, and does what the class says. None of them sends anything to the partner.

| Class | When | The case | Auto-hold | You are told | Counts toward the daily cap |
|---|---|---|---|---|---|
| `config_error` | `claude` or the checkout is missing, or the request is rejected | `needs-owner` | `processor` | yes | yes |
| `auth_expired` | the login has expired | back to `intake`, its session kept | `processor`, lifted once preflight passes again | once, when the hold is new | no |
| `quota_exhausted` | the account's usage limit is reached | back to `intake` until the quota resets | none | once per reset | no |
| `rate_limited` | a 429 | back to `intake` for 2 minutes, doubling up to 30 | none | no | no |
| `unavailable` | the API is overloaded or failing | back to `intake` for 5 minutes | none | no | no |
| `policy_refusal` | a usage-policy refusal | `needs-owner` | none | yes | yes |
| `budget_exceeded` | the run's turn or spend limit | `needs-owner` | none | yes | yes |
| `timed_out` | past `timeout_minutes`, the first tick to see it stops it; a run that ignores the stop is killed a minute later (macOS and Linux), and given up if it still runs 10 minutes after that first stop | `needs-owner` | none | yes | yes |
| `crashed` | no result, an error result, a processor method that raised, or a checkout lock that could not be released | `needs-owner` | none | yes | yes |
| `needs_human` | no valid outcomes, or denied permissions | `needs-owner` | none | yes | yes |
| `workspace_conflict` | before a start: the checkout is busy | unchanged, deferred 10 minutes | none | no | no |
| `effect_blocked` | a deploy refused for CI minutes, billing or a 403 | `needs-owner` | `effect:deploy` | yes | the run already counted |

**A crash never ends the tick** (issue #24). Any exception a processor method raises is a `crashed` run: its case goes to `needs-owner`, you are told, and the tick goes on with the other cases. A deploy that raises hands its cases to `needs-owner` the same way. A case left `working` with no run in flight has lost its run, to a tick that died after collecting it or to a 0.0.x `working` label it was adopted with: it goes to `needs-owner` too, and you hear `run lost` once.

**Run ids.** A run id is `<case>-r<n>-<suffix>`: the case's n-th start and eight hex digits of a fresh uuid, so no run ever reuses an earlier run's directory. A run id that names another case's run directory is refused and classified `config_error`.

### Holds

A hold stops work in one scope until `liaise unhold`. The scopes are `global`, `processor`, `subject:<slug>`, `person:<id>`, `repo:<owner/repo>`, `checkout:<path>` and `effect:<kind>` (such as `effect:deploy`); when several apply, the most specific one is reported. The mode says what stops:

| Mode | New runs | Runs in flight | Their effects (messages, deploys) |
|---|---|---|---|
| `block` (the default) | none start | go on, and are collected | wait, kept on the case as drafts |
| `drain` | none start | go on | go out as usual |
| `cancel` | none start | are stopped gracefully (an interrupt, then a terminate after 15 seconds) and stay resumable; the case goes back to `intake` | wait, kept on the case as drafts |

```
liaise hold subject:example-app --reason "pat is away until Monday"
liaise hold effect:deploy --reason "release freeze"
liaise hold person:pat --mode drain
liaise hold global --mode cancel --reason "something is wrong"
liaise unhold subject:example-app
```

Notifications to you always go out, whatever the mode. A case whose effects waited goes to `needs-owner`, and its drafts stay for you: unhold does not send them. `liaise` also holds scopes itself, recorded as set by `auto:<class>`: `processor` after `config_error` or `auth_expired`, and `effect:deploy` after `effect_blocked`. It lifts its own `processor` hold once preflight passes again, probing every 30 minutes; `effect:deploy` stays until you unhold it. It never replaces or lifts a hold you set.

### The workspace

A subject's runs share its checkout (`workspace.path`, behind the `workspace=` seam), one run at a time. Two checks come before a start:

- **The lock.** A lock file per checkout under `state_dir/locks` names the run and its pid. Another run is refused while that pid is alive, and a lock left by a process that is gone is reclaimed.
- **The collision check.** `liaise` reads Claude Code's session records in `~/.claude/sessions` and defers the case for 10 minutes when a live session, not one of its own runs, is working in the checkout or below it: you, most likely, working there by hand.

### Budgets, notifications and scheduling

**Budgets** are mandatory, per subject under `[policy.budget]`: `concurrent` runs (1), `timeout_minutes` per run (60, enforced), `max_turns` (200, stated in the prompt) and `daily_dispatches` (6). A case that is ready once the daily cap is used up moves to `budget`: the partner is told once, as any reply would be (a draft in draft mode), you are told once per subject per day, and the case starts again the next day. Runs that end in `auth_expired`, `quota_exhausted`, `rate_limited` or `unavailable` do not count.

**Notifications.** `liaise` has no dashboard. When something needs you (an escalation, a diverted or failed message, a run that failed or was lost, held effects, a refused start, a failed deploy, the daily cap), it pushes to the [ntfy](https://ntfy.sh) topic in `LIAISE_NTFY_TOPIC`, or in the variable `notify.ntfy_topic_env` names in `config.toml`. Unset, it is silent, never an error. Anyone who knows a topic's name can read it, so no notification carries anything a case holds: not a draft, a message, the agent's reasons or summary, nor a command's output. A notification names the subject, the case, the event and its cause (an error class, the gate filter that diverted a message, a deploy's exit code), and points at `liaise case show <case>`, which shows the rest on your machine. Titles, like bodies, carry no names: a title is fixed text with the subject or the case, never a person's name or address, an issue's title or a report's subject.

**Scheduling.** `liaise schedule install` sets up a launchd agent on macOS, or a systemd user timer on Linux, that runs `liaise run --once` every 2 minutes (`--interval-minutes` to change it). Both schedulers hand a job a nearly empty environment, so it snapshots `PATH`, `HOME` and the ntfy variable from your shell; install again after moving `gh` or `claude`. The job leaves detached runs alive when its tick exits (launchd's `AbandonProcessGroup`, systemd's `KillMode=process`). One tick runs at a time: the run lock is an operating-system lock on `state_dir/run.lock`, so a tick that finds another holding it does not start, and a lock goes away with the process that held it. `liaise case set-state` takes the lock too. `liaise schedule status` says `installed (outdated: re-run liaise schedule install)` for a job installed before 0.1, which lacks those settings.

## Commands

- `liaise run [--once] [--dry-run] [--subject SLUG]`: one tick: take in what arrived, collect finished runs and carry out their outcomes through the gate, deploy, start ready cases, nudge quiet deliveries once, and set labels. `--dry-run` prints the plan and changes nothing. Without `--once` or `--dry-run`, it ticks every minute until interrupted.
- `liaise status`: the last tick (`running`, `interrupted` if its process died first, or `finished`), the holds, the runs in flight, each subject's cases by state and dispatches today, the unrouted queue, the drafts waiting for you, and the latest digest notes. It changes nothing.
- `liaise hold SCOPE [--mode block|drain|cancel] [--reason TEXT]` and `liaise unhold SCOPE`: stop and resume work in a scope.
- `liaise case list [--state STATE]`: every case, or those in one state, with its conversations. It changes nothing.
- `liaise case show CASE_ID`: what a notification about the case leaves out: its state, the reason of its last escalation, its last failed deploy with the command's output, each draft waiting for you with its text, and its latest entries. It changes nothing.
- `liaise case set-state CASE_ID STATE [--reason TEXT] [--dry-run]`: move a case as you, recorded on the case; this is how a case in `needs-owner` or `deployed` moves on. `intake` has the tick start it again once it is ready, resuming its session. Any state but `working` is allowed, and a case with a run in flight is refused. While a tick is running it refuses too, changing nothing: try again once the tick is done. Its label follows on the next tick; `--dry-run` writes nothing.
- `liaise subject list` and `liaise subject show SLUG`: each subject as `liaise` reads it, defaults applied, with any binding that could never match.
- `liaise setup SUBJECT`: create the subject's claim labels and every state label in each repository it binds. Safe to run again.
- `liaise migrate-config [--apply]`: derive subject files from a 0.0.x configuration; a dry run unless `--apply`.
- `liaise schedule install`, `liaise schedule uninstall` and `liaise schedule status`: manage the scheduled job. `status` says `installed (outdated: re-run liaise schedule install)` for a job installed by 0.0.x, which kills the runs its ticks start.

Every command takes `--root` for a config root other than `~/.config/liaise`.

## Migrating from 0.0.x

0.0.x configured one partner per file under `partners/`. 0.1 configures one subject per repository or site under `subjects/`, and nothing in the old files changes:

```
liaise schedule uninstall        # stop the 0.0.x job while you migrate
liaise migrate-config            # print the plan; nothing is written
liaise migrate-config --apply    # write the subject files the plan printed
liaise run --once --dry-run      # check what 0.1 would do
liaise schedule install          # required: the 0.0.x job would kill detached runs
```

`liaise migrate-config` is a dry run by default: for each repository, it prints the subject file its partners become, the files it came from, and what to check (conflicts, warnings, binding problems, notes). `--apply` creates each missing `subjects/<slug>.toml`; it never overwrites a file, and never touches `config.toml`, `partners/` or `briefs/`.

**Install the schedule again.** A machine upgrading from 0.0.x must run `liaise schedule install` after migrating. The 0.1 launchd agent sets `AbandonProcessGroup`, and the systemd unit `KillMode=process`, so detached runs survive the tick that started them. A job installed by 0.0.x has neither, so launchd would kill every run as soon as its tick exits.

What the migration does:

- **One subject per repository.** Partners sharing a repository become one subject, each partner a person with the `partner` role and their labels as claim labels. Their settings must agree: the first partner's value is kept and every other value is listed as a conflict, except budgets, which keep the strictest.
- **Relays.** `owner_login` becomes a relay (`github:<owner_login>`), since the 0.0.x apps filed issues with the owner's credentials, so their `partner:<slug>` labels keep counting as claims.
- **Per-person briefs.** Each partner's brief goes to `policy.briefs`, so partners sharing a repository keep their own; the subject's `brief` is set only when they all share one.
- **Author bindings are opt-in.** 0.0.x also took any issue a partner's login opened, labelled or not, which picked up issues never meant for `liaise`. 0.1 binds by label, and a note on each subject names the `?author=` binding that opts back in.
- **Not carried over.** Custom `dispatch.command` and `dispatch.resume_command` templates (0.1 builds the `claude` command itself, and a warning shows what each one passed, to carry by hand), a partner's `display_name`, and `log_dir`.
- **Open issues are adopted.** On a repository's first poll, `liaise` reads its open issues and adopts each one a binding matches, with its comments, so readiness sees the partner's markers and messages from before the upgrade. One that already carries a `liaise:` state label opens its case in that state, so nothing the partner sees changes; one carrying several opens in `needs-owner`. A case adopted in `needs-partner` waits for the partner to write after the adoption, and one adopted in `working`, with no run `liaise` could collect, goes to `needs-owner`. 0.0.x session ids are not carried over, so an adopted case's next run starts a new session. A dry run commits no cursor, so every dry run before the first real tick adopts again.

## Breaking changes in 0.1

- **Partners are subjects.** `liaise` runs from `subjects/<slug>.toml`; `partners/<slug>.toml` is read only by `liaise migrate-config`.
- **Removed commands.** `partner list`, `partner show` and `poll` are gone: use `subject list`, `subject show` and `run --once --dry-run`.
- **The agent no longer sets labels or posts comments.** It reports structured outcomes; `liaise` sends messages through the gate and sets the state label from the ledger.
- **Dispatch logs are replaced** by run directories under `state_dir/runs` and the case ledger under `state_dir/ledger`; `log_dir` is no longer read.
- **The scheduled job must be installed again** with `liaise schedule install`, or launchd kills detached runs when their tick exits.
- **The Python API changed.** The package exports the 0.1 surface (`run_once`, `status_lines`, `Ledger`, `Subject`, `load_subjects`, `intake`, `run_gate`, `plan_outcomes`, `ClaudeHeadless`, `EchoProcessor`, `hold`, `unhold`, `migrate_config` and their records). The 0.0.x `dispatch`, `run`, `state` and `messages` modules are gone, and so are `load_config`, `PartnerConfig`, `compose_prompt`, `dispatch_issue` and `setup_labels` at the package root.

## Known limitations

- Held effects are not replayed after unhold: they stay on the case as drafts, and the case in `needs-owner`, for you to send by hand.
- Detecting an expired login before a run depends on `claude auth status` exiting non-zero; when it does not, the run itself finds out, and the tick holds the processor after it.
- A label claim is judged by the issue's author, not by who applied the label, pending who-applied-the-label support upstream in correspond (thorwhalen/correspond#23).
- GitHub binding refs are lower-cased when a subject loads, a workaround for correspond comparing them case-sensitively (thorwhalen/correspond#24).
- On Windows, a `.cmd` shim for `claude` can mangle the quoting of `--json-schema`, whose value holds characters `cmd.exe` treats specially.
- A case in `needs-owner` or `deployed` never starts again on its own: move it on with `liaise case set-state`. A label changed by hand, 0.0.x's way to do it, changes nothing, and the next tick overwrites it.
- correspond's GitHub poll reports neither a closing nor a reopening, so `liaise` learns an issue's state by reading the issue, which costs the issue and every page of its comments: two requests or more. A case whose issue was read closed, but which is otherwise ready to start, has its issue read again at most once an hour, so a reopening can take up to an hour to be noticed. Three reads in a row that fail for good (the issue not found, or not permitted), such as for a deleted or transferred issue, send the case to `needs-owner`, and you are told once; a read that fails for a while, offline or logged out, only makes the case wait.

## Design

The seams, the decisions behind 0.1 and what comes next: [docs/adr/0001-liaise-0.1-seams.md](docs/adr/0001-liaise-0.1-seams.md).
