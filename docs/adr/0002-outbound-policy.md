# ADR 0002: one outbound policy, every filter, and approvals bound to what the operator saw

- **Status:** accepted
- **Date:** 2026-09-16
- **Amends:** [ADR 0001](0001-liaise-0.1-seams.md) ("the gate's order is fixed, and it fails closed")
- **Design of record:** liaise discussion 32, *Outbound message safety* (§5.1, §5.3, §5.4, §5.6, §5.7, §8); slice L3 of the epic, issue #36 of issue #33

## Context

0.1's gate decided by channel name. `leak_scan` ran only on channels listed in `policy.public_channels`, looked for shapes and for `policy.leak_terms`, and knew nothing about who could actually read the conversation it was sending to. `reply_mode` ran first and the first divert ended the gate, so a draft held for the operator was never scanned: a leak reached them unflagged. Nothing recorded what the run that wrote a message had read, and an approval was the operator's name and the time, which released any message on that case, whatever it said by the time it went out.

correspond now says who can read a conversation (`Audience`), acquaint says what each reader may be told (`disclosure`), liaise's detectors say what a message holds (slice L1) and its policy table turns those into a verdict (slice L2). This record is how the gate uses them.

## Decision

### `outbound_policy` replaces `leak_scan`, and the audience decides

The filters are, in this order: `outside_a_case`, `outbound_policy`, `writing_card`, `deslop`, `notify_recipient`. `outbound_policy` takes the audience from the gate's context (computed through correspond right before the gate runs, and never cached), the disclosure through the `disclosure=` seam, the findings of the detectors over the text, the title and each attachment name, and the run's provenance, and evaluates the table of discussion §5.4.

- **A channel's name decides nothing.** `policy.public_channels` is still read from a subject file and is no longer consulted: a name is not an audience (§11), and an audience nobody could compute is already public. It goes after one release.
- **`policy.leak_terms` are still scanned for**, as vocabulary under the entity `policy.leak_terms` at label `red`, which no reader of any channel but the operator's own devices is cleared for. So a leak term is still held back everywhere 0.1 held it back, and on the channels 0.1 ignored as well. It goes after one release, once the same words live in acquaint.
- **Draft reply mode is a row of the table**, not a filter of its own. `outside_a_case` keeps the one thing the table cannot say: a message outside any case waits for the operator, because its sender chose where it goes (issue #28, discussion §6.2).

### Every filter runs, and the most restrictive concern decides

Each `Divert` says how far it holds the message back (its `flow`, one of `liaise.policy.FLOWS`), and becomes one or more `Concern` records — the policy's, one per rule that fired. The decision's flow is the most restrictive concern still standing and its reasons are all of them, most restrictive first. A message goes out only at `send`. A filter that raises, or answers anything but a `Pass` or a `Divert`, contributes an `approve` concern with the error as its reason: the operator sees a flagged draft rather than nothing.

The order stays fixed and is not a seam. The mention, the gate's one rewrite, comes last, so every filter judges the text as it was written, and the rewrite reaches the channel only when nothing held the message back.

### An approval binds to the payload and the audience it was given for

An `Approval` carries the payload hash and the audience hash of the decision the operator was shown, the id of that verdict, the rules it overrides and a justification. At send time the gate runs again and recomputes both hashes:

- while they match, the approval settles each concern whose rule it names and whose flow is at most `approve`;
- a `refuse` is never settled, nor is a concern with no rule (deslop, a missing handle, a filter that failed);
- an approval whose hashes differ settles nothing and is itself the first concern the operator reads, naming what changed.

So a released draft whose repository went public between the answer and the send is not sent, and the operator sees the new verdict. `liaise case send-draft` and `liaise message send-draft` judge the message first, show the audience in words and what the answer would release it past, and send only on a typed `y` at a terminal.

### What the ledger keeps

Every gated message's `gate` entry records the flow, each concern with its findings (kind, rule, position and a keyed fingerprint, never the value), what an approval settled, the verdict (its audience snapshot, the readers' tiers and clearances, the mode), the labels, seals and gaps consulted, the provenance, both hashes and the approval with whether it bound. Operator notifications are unchanged: the subject, the case, the event and the filter that held the message back, and nothing the message said.

`liaise case show` and `liaise message show` render a held message with the gate's flow and the audience in words, every invisible and control character written as `<U+XXXX>`, and every link and image destination in full.

### Provenance is computed from the ledger, not guessed

A run is tainted when the case holds a message its subject does not trust for `request_work`: an author with no role, a role that does not grant it, or a grade it does not accept. The channel's own posts are trusted; the tick's own template messages (the daily-cap message, a nudge) are clean; a message outside a case is unknown, which counts as tainted. Every `message` entry of the case counts, not only those a run had read when it started: the ledger does not say which a resumed session saw, and counting one it did not can only hold a message back.

### Subject policy gains four values, and they are configuration, not seams

`tainted_runs` (`approve`, or `send` to waive the taint rule), `link_allowlist`, `canary_terms` and `mode` (`enforce` or `shadow`).

## Consequences

- **More messages wait for the operator, and none that waited before goes out unseen.** A subject whose people have no acquaint records will see every recipient counted a stranger, which is `approve`.
- **On a public repository nothing sends by itself yet.** An irreversible send to an `org` or `public` audience is `delay`, which §5.5 degrades to a draft until the delay outbox (issue #38) exists, and the reason says so. Issue #36's acceptance line reads "`send` for the export-only draft"; on a public repository the table gives that draft `delay`, and it is held. The same draft to a private repository sends. This is the design's own degradation, not a new rule, and L5 lifts it.
- **A `revise` verdict is a flagged draft for the operator.** Returning it to the processor to resubmit is not built; the flow, the route and the reasons are recorded so it can be, without changing a caller.
- **acquaint that imports and then fails holds every message back**, with the error as the reason: the gate does not judge a message with less than it should know. Without acquaint, every reader is `need-to-know` and the subject's leak terms are the vocabulary.
- **`mode = "shadow"` is accepted, recorded on every verdict, and enforces like `enforce`** until shadow mode lands (issue #39).
- **Not wired yet, and left for the slices that own them:** the operator's own addresses and handles as `personal` terms, and a recipient's AI tolerance for the disclosure-stance rule. Both are inputs the policy already reads.
- **A known miss, filed as issue #46:** private words inside a link's path reach a wide audience at `approve`, since a plain link is shown to the operator in full rather than refused.

## Rejected

- **Keeping `leak_scan` beside the policy.** Two scans drift, and the leak scan's channel-name rule is what the policy exists to replace.
- **Letting an approval settle a refusal.** A secret, a seal or an exfiltration shape is not a judgement call: the text has to change.
- **Binding an approval to the case rather than the message.** A case-wide release sends whatever the draft says by the time it goes out.
- **Trusting `public_channels` to widen an audience.** A name cannot make a private repository public, and it must never narrow what unknown already resolves to.
