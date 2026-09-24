# liaise

The loop between a non-technical partner and a coding agent, over conversations
(GitHub issues, a web inbox). A `subject` (`subjects/<slug>.toml`) binds
conversations and says who may ask for what; each tick takes in cases, starts ready
ones as detached processor runs, and carries their outcomes through an outbound
gate before anything is sent. `liaise run --once --dry-run` prints the plan and
changes nothing.

## Module map (`liaise/`)

**Intake & identity:** `subjects.py` (loads `subjects/<slug>.toml`), `access.py`
(who a message is from, what they may ask), `intake.py` (which messages belong to
which case), `github.py` (the GitHub seam — one protocol, two implementations,
incl. `FakeGitHub`).

**State:** `model.py` (cases, ledger entries, outcomes, holds, runs — the 0.1 data
model), `ledger.py` (liaise's own record of what it has seen/opened/decided/started),
`holds.py` (operator- or tick-set stops on work), `readiness.py` (is a case ready to
dispatch, read off the ledger), `projection.py` (a case's state as one GitHub label).

**Running work:** `tick.py` (one pass of the loop), `processor.py` (what runs a
case's work, detached), `prompt.py` (the prompt a processor run starts from),
`errors.py` (processor error taxonomy), `outcomes.py` (what a run reports, checked
and planned into actions), `workspace.py` (one checkout per subject, one run at a
time, never beside a live session).

**Outbound gate:** `gate.py` (the checks every outbound message passes + the
verdict), `detect.py` (detectors: what a message holds, reported without the
value), `policy.py` (findings + audience -> flow), `outbound.py` (what the policy
filter gathers), `messages.py` (agent-initiated messages, also through the gate),
`outbox.py` (the delay outbox — messages gated `delay`, held in a cancellable
window), `release.py` (send: through the gate, then through `correspond`),
`legacy.py` (the 0.1 gate replayed as a counterfactual, recorded beside each
verdict for shadow agreement).

**Operator surface:** `cli.py` / `__main__.py` (the `liaise` command), `cases.py`
(`liaise case list`, `show`, `set-state`, `send-draft`, `reject-draft`), `report.py`
(`liaise gate report` — counts, whether to enforce), `notify.py` (ntfy owner notification),
`schedule.py` (launchd/systemd periodic run), `config.py` + `migrate.py` (global
config; `liaise migrate-config` from 0.0.x partner files), `testing.py` (fakes for
tests and the one-command smoke test).

## Tests & lint (verified)

```bash
uv venv .venv && uv pip install -e ".[dev]"
.venv/bin/pytest -v --tb=short   # 1734 passed, 1 skipped, 9 xfailed
.venv/bin/ruff check .
```
Or `wads ci-local` (Python 3.11+3.12, Windows included, `extras = "dev"` — see
`[tool.wads.ci]`). The test suite includes an outbound-gate adversarial scenario
suite that reports a severity-weighted miss rate and a false-divert rate — read
these numbers, not just pass/fail, when touching `gate.py`/`policy.py`/`detect.py`.

## Docs

- `docs/adr/0001` (0.1 seams), `0002` (outbound policy), `0003` (delay outbox).
- `misc/docs/research/` — background research.
- `.claude/skills/liaise` — the shipped operator-facing skill.

## Dependents

None recorded in the fleet dependency graph. `liaise` itself depends on `cw` and
(per its release path) `correspond` — check those before changing `gate.py`'s or
`release.py`'s public contracts.
