# ADR 0005: the chat loop, an agent that answers a channel at once

- **Status:** Accepted
- **Date:** 2026-11-23

## Context

The coding loop (ADR 0001) takes a partner's report, waits until they have finished writing, and starts a coding agent that may run for an hour. Some partners want something else: a channel where they ask a question and the operator's agent answers within a minute, from the team's shared material, while the operator is elsewhere. The asks that come with it are the same as the coding loop's, sharper: answer only named people (the operator only when they address the agent), never leak a secret or anything personal, treat every message as data rather than instructions, never commit the operator to anything, and keep a record of everything said in the channel, edits and deletions included, outside the platform.

## Decision

A **chat subject** is a subject file under `<root>/chat/<slug>.toml`: the usual `bindings` (exactly one channel) and `[policy]`, plus a `[chat]` table. It lives outside `<root>/subjects/`, so the tick never polls it and the case machinery never sees it. `liaise chat run <slug>` runs it as a long-lived process; `liaise chat log` and `liaise chat show` read it. The module is `liaise.chat`.

What it reuses, and what is new:

| Part | Reused | New |
|---|---|---|
| Intake | correspond `listen` (Slack over Socket Mode or the Events API: no polling delay, and edits and deletions with their original text) | following the threads of recent messages |
| Who is answered | `policy.people` and `policy.roles` | `answer_roles` (always) and `trigger_roles` (only after a `trigger` such as `agent:`); bots, stale messages and unauthenticated grades are logged only |
| Injection | the gate's taint rule | a deterministic screen that refuses an override attempt or a request for a secret before the agent sees it |
| The answer | the headless `claude -p` pattern and `--json-schema` (as `triage.headless_ask`) | the fence below |
| The sensitivity filter | the outbound gate, `liaise.vet.vet`, with the chat subject's policy (`leak_terms`, `canary_terms`, `link_allowlist`, secrets by shape, local paths), failing closed | nothing |
| The record | nothing fitted: the case ledger only holds routed messages | an append-only JSON-lines audit log |
| The post | correspond `send` with an idempotency key | nothing |
| Telling the operator | `liaise.notify` (no content in a notice) | nothing |

**The fence.** Each answer is a fresh `claude -p` in the subject's `context_dir`, with `--restricted` (no code-running tools, file tools confined to the working directory, user and project settings ignored), `--tools Read,Grep,Glob`, `--safe-mode` (no CLAUDE.md, skills, hooks or plugins), `--strict-mcp-config` (no MCP server), `--permission-mode dontAsk`, `--no-session-persistence`, and an environment cut to a short allowlist of names, so no platform token reaches it. Checked against claude 2.1.317 and 2.1.330: a read outside the directory is refused, a symlink pointing outside is refused, and there is no shell. The agent returns `answer`, `escalate` or `decline` and posts nothing; the loop posts, after the gate.

**Escalation** is the outcome for anything that would commit the operator (a decision, a date, money, access, a key): the channel is told the question was passed on, the agent's draft stays in the audit log, and the operator gets a notice.

**Dry run by default.** `live = false` until the operator sets it: every answer is produced and gated, and what would be posted is logged instead. `--dry-run` forces it.

## Considered and rejected

- **One long-lived session fed by correspond's Claude Code channel server.** An injection would persist in its context from one message to the next, the session's tools would be whatever its settings allow, and its context would grow without bound. A fresh, fenced process per message bounds both the blast radius and the cost.
- **Putting it in correspond.** correspond is the messaging layer and knows no people; who may be answered, the gate and the agent are liaise's.
- **A new package.** No second consumer yet; a module whose imports point one way can be extracted when one appears.
- **Answering edits.** An edited question is logged with both texts, not answered again: re-answering would let a message be rewritten into something else after the fact.

## Seams

| Seam | Default | Replacement you can point at |
|---|---|---|
| `answer=` | `claude_answer`, the fenced `claude -p` | the fakes in `tests/test_chat.py`; any callable `(prompt, *, chat) -> mapping` |
| `registry=` | correspond's channels | correspond's `FakeChannel` |
| `audit=` | `JsonlLog` under the state directory | a `list` (tests) |
| `cursors=` / `sends=` | correspond's stores, under a kind of their own | `dict`s (tests) |
| `notify_fn=` | `liaise.notify.notify` (ntfy) | a recorder (tests) |

Not seams: the screen's patterns and the notices (configuration), the audit record format, the prompt (`data/chat_prompt.md`), the thread reference grammar (`<channel>/<message id>`, Slack's).

## Consequences

- Limits are per person per hour and per day, counted when an agent call is dispatched, since every call costs.
- While the process is down, nothing is answered; on Socket Mode, Slack retries delivery for a while, and a message older than `stale_minutes` when it arrives is logged, not answered.
- Replies in a thread under a message older than `thread_days` are not followed, so not logged.
