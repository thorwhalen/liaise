---
name: liaise
description: Use when onboarding a new partner to liaise, checking what liaise is waiting on or has done (liaise status / liaise poll), or explaining what a liaise: state label on a GitHub issue means. Triggers on "add a partner to liaise", "onboard <name> to liaise", "what's liaise waiting on", "check liaise status", "what does liaise:needs-owner mean", "why hasn't liaise picked up this issue".
---

# liaise: the owner's agent skill

`liaise` runs the loop between a non-technical partner and a coding agent, over GitHub issues. This skill is for the owner's own agent — the one helping the owner run `liaise`, not the one `liaise` dispatches to do the work.

## Onboarding a new partner

Every partner is one config file plus one brief file, both under `~/.config/liaise/`. Nothing about a partner belongs in the `liaise` package itself.

1. Pick a short slug (e.g. `pat`).
2. Write `~/.config/liaise/partners/<slug>.toml` — at minimum `display_name`, `github_logins`, `repo`, `brief`. See the package README's quick start for the full shape, including `dispatch`, `verify`, `deploy`, `escalate`, and any global default you want to override for just this partner. `notify_login` (the login `@mentioned` in every partner-facing comment — it's the only way the partner gets notified) defaults to the first `github_logins` entry; set it explicitly for a partner identified by label only, and it's required when `reply_mode = "direct"`.
3. Write `~/.config/liaise/briefs/<slug>.md` — how to talk to this partner: tone, what they care about, which decisions are theirs and which are the owner's. This goes verbatim into every prompt the coding agent sees for their issues.
4. Run `liaise setup <slug>` — creates the partner's label and every `liaise:` state label in their repo. Safe to re-run.
5. Run `liaise poll --partner <slug>` to confirm `liaise` sees their issues and is computing readiness correctly.
6. If not already installed, `liaise schedule install` sets up the recurring job (once, covers every partner).

## Reading `liaise status`

`liaise status` first reports when the last run started and ended. It says `running` while a pass is still in progress, which a long dispatch can stretch to many minutes, and `interrupted` when a pass started but its process died before it could finish (a shutdown, or the job unloaded mid-run). Otherwise, a `run_started_at` more than a couple of minutes past the schedule interval means the scheduled job stopped — check `liaise schedule status`. Then, per partner: today's dispatch count against the daily cap, and every issue currently in `liaise:needs-owner`. That last list is the actual to-do list — everything else in the loop is either waiting on the partner or already handled.

## What each `liaise:` label means

Exactly one of these is on a `liaise`-tracked issue at a time. `liaise` never closes an issue — that's the partner's or the owner's call.

| Label | What it means | Who set it |
|---|---|---|
| `liaise:intake` | Seen. The partner may still be writing, or the quiet window is running. | liaise |
| `liaise:paused` | The partner asked to wait (a `#wait#` marker). | liaise |
| `liaise:working` | A coding agent is dispatched and running right now. | liaise |
| `liaise:needs-partner` | The agent asked a question and is waiting on the partner to answer. | the agent |
| `liaise:needs-owner` | Needs the owner: an escalation, a decision, or a crashed/stuck run. | the agent, or liaise on a crash or the daily budget cap |
| `liaise:deployed` | Live. The partner has been told to try it. | the agent, or liaise after a batch deploy |
| `liaise:budget` | Today's dispatch cap was hit for this partner. Resumes tomorrow. | liaise |

A `liaise:needs-owner` issue is where the owner's own judgment is actually needed — an escalation the agent declined to make alone, or reconciliation after something crashed. Read the issue thread and the dispatch log before deciding what to do — the agent's drafts and escalations are in it, a crash notification names the file, and every log lives under `log_dir` (`logs/` under `state_dir` by default); the label alone only tells you that a decision is due, not what it is.

## Why an issue isn't moving

- **Still in `liaise:intake` well past the quiet window?** Check `liaise poll` — a marker or a comment from someone who isn't the partner (including the owner) never restarts or shortens the clock, only the partner's own edits and comments do.
- **Nothing happening at all?** `liaise status`'s `last_run` lines are the first thing to check — `running` means a dispatch is still in progress, while an old `run_started_at` means the scheduled job stopped, which looks exactly like an unready issue from the partner's side.
- **Hit the daily cap?** `liaise:budget` resumes automatically the next day; there's nothing to do.
