# Outbound safety for the owner of liaise

A summary of the research report *Outbound message safety*, kept in this repository at [misc/docs/research/outbound_message_safety.md](../../../../misc/docs/research/outbound_message_safety.md). Section numbers below refer to it.

## The rule

**Register comes from the recipient; the content ceiling comes from the least-cleared reader of the channel** (§1).

A partner's brief tunes the words. It does not make an issue thread private. A reply that suits the partner can still disclose too much to everyone else who can read the repository.

## What the 0.1 gate covers, and what it does not (§2.1)

- **`leak_scan` decides by channel name.** It runs only on channels named in `policy.public_channels` (by default, `github`). It does not know whether a repository is public, and it does not scan email or any channel left off the list.
- **It finds shapes, not projects.** It diverts on home and temporary paths, `.env` paths, email addresses, private keys, six token shapes and the terms in `policy.leak_terms`. It knows no project name unless one is listed there.
- **`reply_mode` runs first,** so a message kept as a draft reaches the owner without leak-scan notes.
- **A secret is caught only on a channel listed as public.**
- **Nothing tracks what the run read.** A run that read a stranger's issue has read untrusted content, seen the checkout, and can write to a public thread: all three legs of the lethal trifecta (§8.1).

## What an owner can do today

- **Keep `default_reply_mode = "draft"`** on any subject whose repository is public, and for any person with whom care is needed. Give `direct` only to people and channels where a mistake would be cheap.
- **List every project name, alias and codename** that must not reach a partner in `policy.leak_terms`.
- **List every channel whose audience is wider than the partner** in `policy.public_channels`, not only `github`. A private repository in an organisation is readable by every member by default (§3.3).
- **Read drafts with `liaise case show`** before sending them by hand. Name the audience to yourself before you post.
- **Treat a case opened by an unrecorded sender as untrusted:** keep its replies as drafts.

## Proposed in the report, not built yet

- **One `outbound_policy` filter** replaces the channel-name list with an audience computed by correspond. Unknown resolves to public (§10.2, §10.4).
- **Detectors run on every message, drafts included:**
  - secrets, always;
  - project vocabulary taken from acquaint;
  - exfiltration shapes (links and images to unknown hosts, encoded strings);
  - other people's names and personal details.
- **A declared policy table returns a verdict** (`send`, `delay`, `revise`, `approve`, `approve_twice` or `refuse`). The most restrictive rule wins; a tainted run needs approval; a sealed topic or a secret is refused.
- **An approval binds to the payload's hash and the audience snapshot**, and is re-checked at send time (§7.2).
- **A `liaise vet` command** checks a draft outside any case, so `acquaint-write` and hand-directed sends get the same evaluation.
- **Rules run in shadow mode first**, against a scenario suite of invented people (§9).
