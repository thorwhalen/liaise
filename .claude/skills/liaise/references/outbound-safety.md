# Outbound safety for the owner of liaise

A summary of the research report *Outbound message safety*, kept in this repository at [misc/docs/research/outbound_message_safety.md](../../../../misc/docs/research/outbound_message_safety.md), and of what liaise now does about it ([ADR 0002](../../../../docs/adr/0002-outbound-policy.md), and the project's discussion 32). Section numbers below refer to the report.

## The rule

**Register comes from the recipient; the content ceiling comes from the least-cleared reader of the channel** (§1).

A partner's brief tunes the words. It does not make an issue thread private. A reply that suits the partner can still disclose too much to everyone else who can read the repository.

## What the gate does now

Every message liaise sends — a run's outcome, a draft the owner releases, a message an agent sends outside a case — passes every filter, and the most restrictive answer decides.

- **It asks who can read the destination**, through correspond, right before sending and never from a cache. A repository it cannot read resolves to public. The channel's name decides nothing: `policy.public_channels` is still read and no longer consulted.
- **It asks what each reader may be told**, through acquaint: their tier, the clearance that follows, what is sealed from them, and the terms of anything they are not cleared for. Without acquaint, every reader counts as `need-to-know` and `policy.leak_terms` are the terms to look for.
- **It looks at what the message holds** — the text, the title and each attachment name: secrets and canaries, terms this audience may not hear, links and images to hosts outside `policy.link_allowlist`, encoded runs, invisible characters, private addresses, local paths, email addresses, and other people's names.
- **It looks at what the run had read.** A case whose messages include one from someone the subject does not trust to request work taints the run: what it wrote waits for the owner, and is refused outright when it also names something the audience may not hear. `tainted_runs = "send"` waives the first, never the second.
- **It answers in one of five ways:** send, hold for a cancellable window (`delay`), send back to be revised, ask the owner, or refuse as written. A refusal is never released: the text has to change.

## What an owner can do

- **Record the people you write to in acquaint**, with a tier and a review date. A recipient with no record is a stranger, and every message to them waits for you.
- **Give every project, organisation and person that must not reach an audience a label and its vocabulary** in acquaint; `policy.leak_terms` still works, and counts as a label nobody is cleared for.
- **Set `policy.link_allowlist`** to the hosts your messages may link to, and `policy.canary_terms` to words planted where only a private context has them.
- **Keep `default_reply_mode = "draft"`** for anyone where a mistake would be expensive. On a public repository nothing sends by itself today anyway: an irreversible send waits for you until the delay outbox is built.
- **Shadow mode never loosens the gate.** `mode = "shadow"` holds back exactly what `enforce` does; it also records what liaise 0.1 would have decided, so `liaise gate report` can print the shadow agreement and the missed findings, and say whether to enforce.
- **Read drafts with `liaise case show`.** It gives the gate's answer, the audience in words, the whole text with invisible characters spelled out, and every link in full. `liaise case send-draft` shows the same and sends only on a typed `y`, recording your approval with `--justification`.

## Not built yet

- **`liaise vet`, the `before_send` seam and the Claude Code hook** (issue #37): a draft written outside liaise, or a `gh` command in a coding session, does not meet this gate.
- **The delay outbox** (issue #38): `delay` is a draft for you until it exists.
- **A private word inside a link to an unknown host** still reaches a wide audience at `approve` when nothing else holds the message back: you are shown the link in full, and since issue #46 the word among the reasons, even when it is glued to other words in the path (`/HeronTerms.pdf`). A term glued to an all-capitals run (`HERONterms`) or into the host is not found, nor a term under four characters inside a link.
- **Returning a `revise` to the processor**: it is a flagged draft for you today.
- **A semantic pass**: nothing catches a paraphrase that names no term (scenario S7, a known miss).
