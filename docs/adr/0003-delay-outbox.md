# ADR 0003: the delay outbox — a held send, released only into the conversation it was written for

- **Status:** accepted
- **Date:** 2026-09-22
- **Amends:** [ADR 0002](0002-outbound-policy.md) (its consequence "on a public repository nothing sends by itself yet")
- **Design of record:** liaise discussion 32 §5.5 and §12; slice L5 of the epic, issue #38 of issue #33
- **Decided by:** an agent acting for the owner, under the rule that a reversible internal design call is made, recorded and acted on. Each decision below says what would make it wrong.

## Context

The policy gives an irreversible send to an `org` or `public` audience the flow `delay` (discussion §5.4, the `irreversibility` row). Until now `delay` degraded to a draft for the operator, so on a public repository every reply waited for a person, whatever the reply mode. Discussion §12 left open whether the outbox was wanted at all. Four decisions were open.

## Decision 1: build the outbox, on by default

**Chose:** build it. Subject policy `delay_minutes` (default 10, `0` sends at once) sets the window. There is no separate switch to keep the old degradation: a subject that wants every public reply to wait for a person sets its reply mode to `draft`, which already does that.

**Alternatives:** keep draft-by-default on public channels (§12's other option); add an `outbox = false` switch beside `delay_minutes`.

**Why:** a draft for every public reply makes direct mode meaningless on the channel most partners use, and the operator's attention is then spent on messages nothing flagged. The window keeps the one thing the degradation bought — a person can stop an irreversible post — at the cost of a notification instead of a release. A second switch would be a second way to say what reply mode already says.

**Wrong if:** operators do not act on the notification within the window in practice (the ping arrives while nobody is watching), so the window protects nothing and a draft would have been the only real check. Then the default should become draft on public channels, which is additive.

## Decision 2: the hold is an approval by the outbox, bound like an operator's, that settles only the delay

**Chose:** when the gate gives `delay`, the tick keeps the message (as it entered the gate, before the mention is added) with `liaise.gate.hold_for`'s `Approval` by `liaise-outbox`. That approval is bound to the payload hash, the audience hash and the verdict id of the held decision, and overrides only the rules of its `delay` concerns (today `irreversibility`). At release the tick runs the whole gate again, with the audience asked of the channel then, the provenance read from the ledger then, and that approval on the context. While all three still match, the delay is settled and the message goes out; the `gate` entry records the approval and that it bound. On any mismatch the approval is void, and the message becomes a draft with the new verdict. It is never held again, and it is not sent even when the new verdict would be a plain `send`.

**Alternatives:** a gate-context flag that suppresses `delay` at release (binds to nothing, and the recheck would be reimplemented outside the gate); comparing the new decision to the held one in the tick (duplicates `binds()`); holding again with a fresh window on a mismatch (a changed audience could loop with no person in it); sending when the fresh verdict is `send` (a second code path for a case GitHub hardly produces).

**Why:** it reuses the one binding rule ADR 0002 already has, so the gate's surface does not change, and the ledger shows exactly what the outbox released a message past. A stranger's comment during the window taints the case, changes the verdict and voids the hold with no code of its own.

**Wrong if:** the audience a channel reports is unstable from one call to the next (a transient API failure resolves to `unknown`, which is public), so held messages turn into drafts for reasons nobody can act on. The fix would be in the audience reader, not here.

## Decision 3: items live on the case, are claimed before they are sent, and lapse when reached too late

**Chose:** `Case.outbox`, a tuple of JSON items next to `Case.drafts`, so taking an item off and recording why is one save of the case. There is no mutable status on an item: every transition is an append-only `gate` entry whose `decision` is `hold`, `cancel`, `divert`, `lapse`, `interrupted` or, for a release that goes out, `send`. Delivery is at most once. The tick marks the item `claimed_at` and saves the case before it sends, and takes the item off after. An item still claimed on a later tick is never sent again. It becomes a draft that says to check the channel first. An item the tick reaches more than `delay_stale_minutes` (default one day; `0`: never) after its release becomes a draft (`lapse`). The drain is step 2 of the tick, after intake, so it sees what intake heard, and before reconcile, so a held reply goes out before a newer run's. `liaise case cancel-send CASE [INDEX]` takes the run lock, as every operator verb that changes a case does.

**Alternatives:** entries only, the state rebuilt by replaying them (index addressing and "what is due" become a replay per tick); a separate key space (two writes with no atomicity between them); a status field on the item (duplicates the entries and leaves dead items shifting indices); sending before recording (at least once: a crash reposts a public comment, the one thing `delay` exists to prevent); sending a stale item whatever its age (a reply posted days late, after a notification nobody saw acted on).

**Why:** it is the shape drafts already have, and it fails towards the operator. A message whose fate is unknown goes to a person, and never goes out twice by liaise's hand.

**Wrong if:** a channel posts and then raises (a timeout after GitHub accepted the comment). That becomes a failed send and a draft, and the operator's release duplicates it. `send-draft` has the same hole. The fix is an idempotency key in correspond, not ledger design.

## Decision 4: a held message goes out only into the conversation it was written for

**Chose:** each item records `seen`, how many entries the case had when the run that wrote the message started (its prompt was written then, so a comment heard while it ran is one its reply never answered). On every tick, for every item, due or not, any later entry of these kinds turns the item into a draft for the operator (`divert`, reason "the conversation moved on"):

- a `message`, unless the channel's own account wrote it with the text of a message liaise sent on the case (the operator commenting by hand on liaise's account counts);
- a `transition` by anyone but liaise, such as the operator setting the state;
- a `run` entry that read the issue closed.

Right before a release the tick reads the issue's state (once a tick, as it does before a start). If the issue is closed, the message becomes a draft. If the state cannot be read, the message waits.

A later item of the same case is judged by the same rule, so it goes the same way. An effect hold keeps an item held, and it lapses under decision 3 if the hold outlasts the stale bound. An inactive subject is not ticked, so its items wait and lapse the same way. When a subject's intake failed this tick, or reported a problem for any conversation, its outboxes wait for the next tick, since the conversation may have moved unheard. A message the tick wrote itself (a nudge) keeps the provenance it was judged with, so its release is judged alike. One case's failure to release is a problem line, never the tick's end. Within a case the items go in order, and one that does not go out keeps the rest for the next tick.

**Alternatives:** cancel instead of draft (loses the text, and a draft still lets the operator send it); compare times rather than entry counts (an entry carries the time the message was written, so a comment heard late would be missed); rely on the verdict alone (blind to a trusted partner saying "never mind" and to the operator's own moves, the common cases); act at each event (four call sites and a dry-run overlay to reason about, where the drain sees the same entries once).

**Why:** the hold's binding (decision 2) sees who can read the conversation and what the message says, but not what was said since. A reply to a question the partner has withdrawn is exactly the kind of irreversible public mistake the window exists for, and converting to a draft costs one release by the operator.

**Wrong if:** partners routinely add a comment within ten minutes of every reply ("thanks!"). Then most held messages become drafts, and the rule should ignore messages that carry no new request. That judgement belongs to the processor, not to this rule.

## Consequences

- On a public repository, direct mode sends again, `delay_minutes` after the gate held the message and after a fresh check. The degradation note in ADR 0002 no longer applies.
- `liaise case show` and `liaise status` list held messages and their release times; the operator notification says only that a message is held and for how long.
- An operator's release of a draft (`send-draft`) is not held again: the operator's approval settles the delay, since the person the window protects has seen the message.
- A message outside a case never reaches the outbox: it waits for the operator whatever its audience (ADR 0002).
- Shadow mode and `liaise gate report` (issue #39) should leave approvals by `liaise-outbox` out of per-rule override counts.
