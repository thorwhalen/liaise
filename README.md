# liaise

`liaise` runs the loop between a non-technical partner who tests an app and a coding agent that answers, fixes, builds and redeploys — over GitHub issues. The partner files what they notice as a GitHub issue; `liaise` watches the repo, waits until they've finished writing, hands the ready issue to a coding agent, and keeps the state visible on the issue itself, so nobody has to learn a tool or a syntax to be part of the loop.

## Quick start

```
pip install liaise
mkdir -p ~/.config/liaise/partners ~/.config/liaise/briefs
cat > ~/.config/liaise/config.toml <<'EOF'
owner_login = "you"
state_dir = "~/.local/share/liaise"
EOF
cat > ~/.config/liaise/partners/pat.toml <<'EOF'
display_name = "Pat"
github_logins = ["pat"]
repo = "example/app"
brief = "~/.config/liaise/briefs/pat.md"
EOF
echo "Pat likes short, plain answers and hates surprises." > ~/.config/liaise/briefs/pat.md
liaise setup pat
liaise poll
```

That's read-only by default (non-negotiable #4) — `poll` reports Pat's issues and their readiness without changing anything. Once you believe the plan it shows, `liaise run --once --dry-run` prints what a real pass would do, still without acting; drop `--dry-run` to actually act; and `liaise schedule install` sets up the recurring job once you're ready to stop running it by hand.

That's a working loop for one partner (`pat`, testing the fictional `example/app`) polling every couple of minutes. Add more partners by adding more files under `partners/` and `briefs/`.

## Concepts

**Partners.** Everything about a partner — who they are on GitHub, which repo they test, how to talk to them, what commands verify and deploy their app — is one TOML file under `~/.config/liaise/partners/`. Nothing about a partner ever lives in this package's code.

**Issues are the channel.** A partner (or an app filing on their behalf) opens a GitHub issue. `liaise` decides an issue is theirs by author or by a `partner:<slug>` label, and only ever reads their own activity — a comment from anyone else, including the owner, never moves the clock.

**Mentions are the only notification a partner gets.** An issue filed through the app is authored by the app's own credentials, not the partner, so GitHub subscribes them to nothing — a reply that doesn't `@mention` them is a reply nobody reads. Every partner-facing comment `liaise` posts, and every one the operating rules ask the dispatched agent to post, starts with `@<notify_login>`. `notify_login` defaults to the first entry of `github_logins`; set it explicitly for a partner identified by `partner:<slug>` label only (no `github_logins` of their own — the app-filed case). It's required whenever `reply_mode = "direct"`: a direct reply that can't notify the partner is a misconfiguration, not a degraded mode. If the agent ever forgets the mention, `liaise` repairs its last comment on the issue before the run ends rather than leaving the partner unnotified.

**Readiness.** `liaise` waits for a quiet window after the partner's last word before treating an issue as ready, so a request written across three comments doesn't get grabbed mid-sentence. A partner can speed that up with a `#startwork#` marker in their text, or pause it with `#wait#` — both optional, and neither is required to use `liaise` at all.

**State labels.** Exactly one `liaise:` label is on an issue at a time: `intake`, `paused`, `working`, `needs-partner`, `needs-owner`, `deployed`, or `budget`. That label is the entire state machine — read it on the issue, and you know exactly where things stand. `liaise` never closes an issue; that's the partner's or the owner's call.

**Dispatch.** A ready issue gets handed to a coding agent (headless `claude` by default) with a composed prompt: the packaged operating rules, the partner's brief, the issue, which label to set on each exit path, the dispatch log's path, the verify/deploy commands, and a budget. The agent asks questions in the thread, does the work in the repo's own conventions (branch, PR, CI, land), and reports back through the label. An issue dispatched again resumes its earlier session, under the same `permission_mode` as the first run (`auto` unless a partner's `[dispatch]` table sets it). If you override `command` and hardcode `--permission-mode` in it, a resume runs under that same mode (and under none if `command` passes none); set `permission_mode` instead to change both at once.

**Dispatch logs.** Every dispatch gets its own log file under `log_dir`: `logs/` under `state_dir` by default, and a relative `log_dir` in `config.toml` is also taken under `state_dir`. The prompt names the file; the agent appends its drafts and escalations to it, and `liaise` adds the exit code and output once the agent stops. The file lies outside the agent's repo, where its permission mode may not let it write, so the agent is told to fall back to its final message, which the log records as part of the output. A crash notification names the file. `liaise run --once --dry-run` prints where the logs go.

**Batch or per-issue deploy.** With `deploy_per = "batch"` (the default), the agent lands each ready issue without deploying, and `liaise` runs the deploy command once after processing every ready issue, then tells each partner it's live. With `deploy_per = "issue"`, the agent deploys and posts itself.

**Budgets.** A per-dispatch timeout and turn cap, and a daily dispatch cap per partner — mandatory, not optional. Hitting the daily cap is a visible `liaise:budget` label, never silence.

**Notifications.** `liaise` has no dashboard. When something needs the owner — a crash, an escalation, a budget cap — it's a label plus an `ntfy` push if `LIAISE_NTFY_TOPIC` (or whatever `notify.ntfy_topic_env` names) is set. Unset, it's silent, never an error.

**Scheduling.** `liaise schedule install` sets up a launchd agent (macOS) or a systemd user timer (Linux) that runs `liaise run --once` every minute or two, with an environment snapshot taken at install time — both schedulers otherwise hand a job a nearly empty environment.

## Commands

- `liaise setup <partner>` — create the partner's label and every state label in their repo. Idempotent.
- `liaise poll [--partner SLUG]` — report every partner issue, its state, and a readiness countdown. Changes nothing.
- `liaise run [--once] [--dry-run] [--partner SLUG]` — intake, label, dispatch ready issues, batch-deploy, reconcile. `--dry-run` prints the plan and the log directory, and changes nothing.
- `liaise status` — when the last run started and ended (`running` while one is in progress, `interrupted` if its process died first), today's dispatch counts, and anything waiting on the owner.
- `liaise partner list` / `liaise partner show <slug>` — see the resolved config, defaults applied.
- `liaise schedule install` / `uninstall` / `status` — manage the scheduled job.

## Design

The non-negotiables (nothing personal in the package, `gh` as the only GitHub credential, labels as the state machine, read-only by default, mandatory budgets, and the rest) are worth reading if you're extending `liaise` rather than just running it — see the design discussion linked from the project's GitHub repository.
