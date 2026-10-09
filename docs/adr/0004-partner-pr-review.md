# ADR 0004: reviewing partner pull requests on the owner's machine

- **Status:** accepted
- **Date:** 2026-10-09
- **Builds on:** [ADR 0001](0001-liaise-0.1-seams.md) (the processor and ledger seams), [ADR 0002](0002-outbound-policy.md) (every post goes through the gate)
- **Decided by:** an agent acting for the owner, under the rule that a reversible internal design call is made, recorded and acted on. Each decision below says what would make it wrong.

## Context

A partner on a subject has read access to its repositories and files issues; `liaise` runs the loop from those issues. Some partners now also write code. With read access, their change can only arrive as a pull request from a fork, and nothing has looked at it: no colleague, no CI the owner trusts, no reviewer. The owner wants every such pull request reviewed adversarially before it lands, the verdict posted on the pull request in plain language, the state visible on the pull request, and, where the owner says so, the change squash-merged after approval without the owner at the keyboard. Partners never merge.

Two facts shape the design. The review has to be done by a coding agent that can read the repository and run its tests, and that agent should be the owner's: the owner's machine, the owner's `claude` login, the owner's subscription, not a key, not the partner's account. And `liaise` already has the parts: a processor seam that runs a detached `claude` session with a prompt and a JSON schema and collects its structured result, a ledger with a case per conversation, a label projection, an outbound gate every post goes through, budgets and notifications. A pull request is an issue to GitHub, and `github:owner/repo#N` is already a valid conversation reference.

## Decision 1: a review is a processor run, and the ledger keeps the case

**Chose:** a pull request under review is a case of a second kind (`Case.kind == "pull"`), keyed on the pull request's reference, with a state vocabulary of its own (`pr-reviewing`, `pr-approved`, `pr-changes`, `pr-declined`, `pr-merged`), projected as `liaise:<state>` labels the way issue cases are. The review itself is a `Job` on the subject's processor, with its own prompt (`liaise/data/review_rules.md`, the briefs, the pull request and its diff) and its own JSON schema (`verdict`, `summary`, `findings`, `for_owner`). It runs in the subject's checkout when the pull request's repository is the subject's, else in a scratch directory, and it counts against the subject's `daily_dispatches` and `concurrent` like a case's run. The tick's reconcile step collects it like any run.

**Alternatives:** a separate daemon with its own budgets; a GitHub Action in the repository (runs on GitHub's minutes under GitHub's identity, and a partner can edit a workflow in their fork); a review posted by the owner's interactive session on request (no loop).

**Why:** one seam, one ledger, one budget. A review is work the subject's agent does, and the subject's caps are the owner's one statement of how much automatic work a subject gets a day. A second kind of case, rather than an issue case in a new state, keeps the two vocabularies from leaking into each other: `project_labels` strips only the labels of the case's own kind, so a pull request never loses a label an issue would carry, and vice versa.

**Wrong if:** reviews turn out to starve the issue cases of their daily cap, in which case the review step needs a cap of its own (one more field on `[review]`, additive).

## Decision 2: the session posts nothing; `liaise` posts, through the gate

**Chose:** the review run is told, in its packaged rules, not to post a review, comment, label, merge, close or edit anything. It ends with a structured result. `liaise` builds the posted body from `summary` and `findings`, runs it through the outbound gate (`run_gate`: the policy, the writing card, deslop, the mention of the author), and posts it as a pull-request review with the event the verdict names (`APPROVE`, `REQUEST_CHANGES`, or a `COMMENT` for a decline). A verdict the gate holds back stays on the case as a draft; `liaise review post` posts it once the owner has read it at a terminal. `for_owner` never reaches the pull request: it is a `note` on the case, and the owner's notification says a note is there (never what it says, since anyone who knows the ntfy topic can read it).

**Alternatives:** let the session post with `gh pr review` (the Claude Code hook would then vet it, as it vets every `gh pr review` from any session); let the session post and have `liaise` only label.

**Why:** the same reason ADR 0001 gave for issue cases. A post the session makes is judged after the fact, if at all; a post `liaise` makes is judged first, by the policy the owner wrote, and recorded with the decision. It also keeps the hook's job honest: the hook exists for posts an agent chooses to make, and a review run should never be choosing.

**Wrong if:** the gate holds back nearly every verdict on real subjects (draft reply mode and the tainted-run rule on public repositories both do), making the loop a queue for the owner. The remedy is configuration (`reply_modes`, `tainted_runs = "send"`), and the README says so; if that proves too blunt, a `[review]`-level reply mode is additive.

## Decision 3: one review per head commit, ever

**Chose:** a review is of a commit. Each run is recorded with the head SHA it reviewed, and the same SHA is never dispatched twice, except once more after a run was lost before it could be collected. A new push gets a new review from a fresh session. A run that ended in an error that counts against the cap (crashed, timed out, no valid result) is the owner's, and that SHA is not retried; an error that does not count (a rate limit, an outage, an expired login) defers and retries, as a case's run does.

**Alternatives:** review on every tick while the pull request is open (an unbounded cost on a pull request nobody touches); review once per pull request (a push after the review goes unseen); resume the session for a new push (the session's view of the diff is stale, and the partner's pushes are the one input the owner does not control).

**Why:** the cost of a review is bounded by what the partner pushes, which is what the daily cap is for, and the record answers "was this commit reviewed, and what did it say" from the ledger alone.

**Wrong if:** partners push many small commits while iterating and burn the cap on intermediate states; then a quiet window before a review (as readiness gives issues) is the additive fix.

## Decision 4: a merge is a later tick's, after a veto window, bound to the approved commit

**Chose:** with `merge = "squash"`, an approved pull request is merged only on a tick after the one that approved it, once `veto_minutes` (60 by default) have passed since the approval, and only while every blocker is clear: not a draft, open, GitHub finds it mergeable, the checks are green (or the repository has none; `require_checks = false` waives this), the head is still the commit that was approved, and nobody set the `liaise:hold` label. The merge call is bound to that head commit (`--match-head-commit`), so a push between the check and the merge fails the merge rather than merging what was not reviewed. A merge that fails is recorded with the commit and the owner told; it is not retried for that commit.

**Alternatives:** merge in the same tick as the approval; no window and no hold label; retry a failed merge each tick.

**Why:** the window is the one place a person can stop an automatic merge, and a label is the simplest thing a person on GitHub can do to stop it. A later tick, rather than the same one, guarantees the verdict was on the pull request for at least one interval before anything irreversible happened. Binding the merge to the commit closes the race that a window opens.

**Wrong if:** nobody acts within the window in practice, in which case the window protects nothing and `merge = "squash"` should stay off; or the owner wants a shorter window on package repositories with full CI, which is one number per subject already.

## Consequences

- `liaise setup` creates five more state labels and `liaise:hold` in every reviewed repository, including the extra ones a `[review]` table names.
- A pull request that matches an issue binding (a `partner:pat` label on a pull request) opens no issue case: intake ignores conversations of kind `pull_request`. A pull request's comments are not taken in either.
- The GitHub protocol grew five verbs (`list_pulls`, `get_pull`, `pull_diff`, `post_review`, `merge_pull`) and the label verbs take `pull=True`, since `gh` edits a pull request's labels through `gh pr edit`.
- `RunResult` carries the run's structured output as read (`structured`), since a review result has no `outcomes`.
- Not built: a cap of its own for reviews, a quiet window before a review, a per-subject reply mode for verdicts, and a review of pull requests by people outside the subject (the authors list is the gate).
