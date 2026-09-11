# liaise

`liaise` runs the loop between a non-technical partner who tests an app and a coding agent that answers, fixes, builds and redeploys — over GitHub issues. The partner files what they notice as a GitHub issue; `liaise` watches the repo, waits until they've finished writing, hands the ready issue to a coding agent, and keeps the state visible on the issue itself, so nobody has to learn a tool or a syntax to be part of the loop.

## Quick start

```
pip install liaise
mkdir -p ~/.config/liaise/subjects
cat > ~/.config/liaise/config.toml <<'EOF'
owner_login = "you"
state_dir = "~/.local/share/liaise"
EOF
cat > ~/.config/liaise/subjects/example-app.toml <<'EOF'
bindings = ["github:example/app?labels=partner:pat"]
workspace = { path = "~/code/example-app" }

[policy]
people = { "github:pat" = "pat" }
roles = { pat = "partner" }
relays = ["github:example-bot"]
claim_labels = { "partner:pat" = "pat" }
EOF
liaise subject show example-app
liaise run --once --dry-run
```

That's read-only by default (non-negotiable #4). `liaise subject show` prints the subject as `liaise` reads it, every default applied, and names any binding that could never match. `liaise run --once --dry-run` prints what one pass would do (what it takes in, which finished runs it collects and what their outcomes would send, which cases it would start) and changes nothing. Once you believe the plan, `liaise setup example-app` creates the labels in the repository, dropping `--dry-run` acts, and `liaise schedule install` sets up the recurring job once you're ready to stop running it by hand.

That's one subject: the fictional `example/app`, whose app files issues as `example-bot` with a `partner:pat` label for its tester `pat`. Replies start in `draft` mode, each kept for you to send, until the subject sets `default_reply_mode = "direct"` under `[policy]`. Coming from 0.0.x? `liaise migrate-config` prints the subject files your `partners/` files become, and `--apply` writes them.

## Concepts

**Partners.** Everything about a partner — who they are on GitHub, which repo they test, how to talk to them, what commands verify and deploy their app — is one TOML file under `~/.config/liaise/partners/`. Nothing about a partner ever lives in this package's code.

**Issues are the channel.** A partner (or an app filing on their behalf) opens a GitHub issue. `liaise` decides an issue is theirs by author or by a `partner:<slug>` label, and only ever reads their own activity — a comment from anyone else, including the owner, never moves the clock.

**Mentions are the only notification a partner gets.** An issue filed through the app is authored by the app's own credentials, not the partner, so GitHub subscribes them to nothing — a reply that doesn't `@mention` them is a reply nobody reads. Every partner-facing comment `liaise` posts, and every one the operating rules ask the dispatched agent to post, starts with `@<notify_login>`. `notify_login` defaults to the first entry of `github_logins`; set it explicitly for a partner identified by `partner:<slug>` label only (no `github_logins` of their own — the app-filed case). It's required whenever `reply_mode = "direct"`: a direct reply that can't notify the partner is a misconfiguration, not a degraded mode. If the agent ever forgets the mention, `liaise` repairs its last comment on the issue before the run ends rather than leaving the partner unnotified.

**Readiness.** `liaise` waits for a quiet window after the partner's last word before treating an issue as ready, so a request written across three comments doesn't get grabbed mid-sentence. A partner can speed that up with a `#startwork#` marker in their text, or pause it with `#wait#` — both optional, and neither is required to use `liaise` at all.

**State labels.** Exactly one `liaise:` label is on an issue at a time: `intake`, `paused`, `working`, `needs-partner`, `needs-owner`, `deployed`, or `budget`. That label is the entire state machine — read it on the issue, and you know exactly where things stand. `liaise` never closes an issue; that's the partner's or the owner's call.

**Dispatch.** A ready issue gets handed to a coding agent (headless `claude` by default) with a composed prompt: the packaged operating rules, the partner's brief, the issue, which label to set on each exit path, the dispatch log's path, the verify/deploy commands, and a budget. The agent asks questions in the thread, does the work in the repo's own conventions (branch, PR, CI, land), and reports back through the label. An issue dispatched again resumes its earlier session, under the same `permission_mode` as the first run (`auto` unless a partner's `[dispatch]` table sets it). If you override `command`, put `--permission-mode {permission_mode}` in it and set the mode with `permission_mode`. A `command` that hardcodes its mode (or passes none) makes the default resume do the same, but only while neither `permission_mode` nor `resume_command` is set.

**Dispatch logs.** Every dispatch gets its own log file under `log_dir`: `logs/` under `state_dir` by default, and a relative `log_dir` in `config.toml` is also taken under `state_dir`. The prompt names the file; the agent appends its drafts and escalations to it, and `liaise` adds the exit code and output once the agent stops. The file lies outside the agent's repo, where its permission mode may not let it write, so the agent is told to fall back to its final message, which `liaise` copies into the log, unescaped, when the agent stops. A crash notification names the file. `liaise run --once --dry-run` prints where the logs go.

**Batch or per-issue deploy.** With `deploy_per = "batch"` (the default), the agent lands each ready issue without deploying, and `liaise` runs the deploy command once after processing every ready issue, then tells each partner it's live. With `deploy_per = "issue"`, the agent deploys and posts itself.

**Budgets.** A per-dispatch timeout and turn cap, and a daily dispatch cap per partner — mandatory, not optional. Hitting the daily cap is a visible `liaise:budget` label, never silence.

**Notifications.** `liaise` has no dashboard. When something needs the owner — a crash, an escalation, a budget cap — it's a label plus an `ntfy` push if `LIAISE_NTFY_TOPIC` (or whatever `notify.ntfy_topic_env` names) is set. Unset, it's silent, never an error.

**Scheduling.** `liaise schedule install` sets up a launchd agent (macOS) or a systemd user timer (Linux) that runs `liaise run --once` every minute or two, with an environment snapshot taken at install time — both schedulers otherwise hand a job a nearly empty environment.

## Commands

- `liaise run [--once] [--dry-run] [--subject SLUG]` — one pass: take in what arrived, collect finished runs and carry out their outcomes through the gate, start ready cases, deploy, label. `--dry-run` prints the plan and changes nothing; without `--once`, it runs a pass every minute.
- `liaise status` — when the last run started and ended (`running` while one is in progress, `interrupted` if its process died first), the holds, the runs in flight, each subject's cases by state and dispatches today, the unrouted queue, the drafts waiting for you, and the latest digest notes.
- `liaise hold <scope> [--mode block|drain|cancel] [--reason TEXT]` / `liaise unhold <scope>` — stop and resume work in a scope: `global`, `processor`, `subject:<slug>`, `person:<id>`, `repo:<owner/repo>`, `checkout:<path>` or `effect:<kind>`.
- `liaise subject list` / `liaise subject show <slug>` — see each subject as `liaise` reads it, defaults applied, with any binding that could never match.
- `liaise setup <subject>` — create the subject's claim labels and every state label in each repository it binds. Idempotent.
- `liaise migrate-config [--apply]` — derive subject files from a 0.0.x `partners/` configuration. A dry run unless `--apply`, which never overwrites a file.
- `liaise schedule install` / `uninstall` / `status` — manage the scheduled job.

## Design

The non-negotiables (nothing personal in the package, `gh` as the only GitHub credential, labels as the state machine, read-only by default, mandatory budgets, and the rest) are worth reading if you're extending `liaise` rather than just running it — see the design discussion linked from the project's GitHub repository.
