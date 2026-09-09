"""The run loop (A.5 batching, A.7): intake, label, dispatch, batch-deploy, reconcile.

`liaise poll` (intake.py + cli.py) only *reports*. This module is where liaise
*acts* — always behind `--dry-run`, which prints the same plan without calling
any mutating `GitHub` method.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, MutableMapping, Optional

from liaise.config import Config, PartnerConfig
from liaise.dispatch import Dispatcher, DispatchOutcome, dispatch_issue
from liaise.github import GitHub
from liaise.intake import compute_readiness, find_partner_issues
from liaise.notify import notify as _default_notify
from liaise.state import current_state, set_state

#: States from which an issue is eligible to be (re-)considered this pass.
#: `working`, `needs-owner`, `deployed` and `budget` are not — those are
#: either mid-flight or waiting on someone other than the partner's clock.
_ELIGIBLE_STATES = ("intake", "needs-partner", "paused")

#: liaise never deploys or "it's live" a partner whose issue ended up in one
#: of these states — the agent asked a question, escalated, or the partner
#: asked to wait.
_NOT_LANDED_STATES = ("needs-partner", "needs-owner", "paused", "budget")


@dataclass(frozen=True)
class PlanItem:
    """One line of what `run_once` did (or, in `--dry-run`, would do)."""

    partner_slug: str
    issue_number: int
    issue_title: str
    action: str  # "intake" | "pause" | "skip (not ready)" | "dispatch"


@dataclass(frozen=True)
class RunReport:
    """Everything one `run_once` pass did."""

    stamped_at: datetime
    plan: list[PlanItem] = field(default_factory=list)
    dispatched: list[DispatchOutcome] = field(default_factory=list)
    #: partner slug -> issue numbers deployed (posted "it's live", set deployed)
    deployed: dict[str, list[int]] = field(default_factory=dict)


def run_once(
    gh: GitHub,
    dispatcher: Dispatcher,
    store: MutableMapping,
    config: Config,
    *,
    partner: Optional[str] = None,
    dry_run: bool = False,
    notify_fn: Callable[..., bool] = _default_notify,
    log_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> RunReport:
    """One pass: intake new issues, dispatch ready ones, batch-deploy landed ones.

    `dry_run` prints the same plan without calling any mutating `GitHub`
    method and without stamping `last_run` — it changes nothing, the same
    promise `liaise poll` makes.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    partners = [config.partner(partner)] if partner else list(config.partners.values())

    plan: list[PlanItem] = []
    dispatched: list[DispatchOutcome] = []
    deployed: dict[str, list[int]] = {}

    for p in sorted(partners, key=lambda p: p.slug):
        landed_this_pass: list[int] = []

        for issue in find_partner_issues(gh, p):
            state = current_state(issue, p)

            if state is None:
                plan.append(PlanItem(p.slug, issue.number, issue.title, "intake"))
                if not dry_run:
                    gh.add_labels(issue.repo, issue.number, [p.label])
                    set_state(gh, issue, p, "intake")
                continue

            if state not in _ELIGIBLE_STATES:
                continue  # working / needs-owner / deployed / budget: not this pass

            readiness = compute_readiness(issue, p, now=now)

            if readiness.paused:
                if state != "paused":
                    plan.append(PlanItem(p.slug, issue.number, issue.title, "pause"))
                    if not dry_run:
                        set_state(gh, issue, p, "paused")
                continue

            if not readiness.ready:
                plan.append(PlanItem(p.slug, issue.number, issue.title, "skip (not ready)"))
                continue

            plan.append(PlanItem(p.slug, issue.number, issue.title, "dispatch"))
            if dry_run:
                continue

            outcome = dispatch_issue(
                gh,
                dispatcher,
                store,
                p,
                issue,
                notify_fn=notify_fn,
                log_dir=log_dir,
                now=now,
                expect_working_on_success=(p.deploy_per == "batch"),
            )
            dispatched.append(outcome)
            if outcome.landed_awaiting_batch_deploy:
                landed_this_pass.append(issue.number)

        if landed_this_pass and not dry_run and p.deploy:
            _run_batch_deploy(gh, p, landed_this_pass)
            deployed[p.slug] = landed_this_pass

    if not dry_run:
        store["last_run"] = now.isoformat()

    return RunReport(stamped_at=now, plan=plan, dispatched=dispatched, deployed=deployed)


def _run_batch_deploy(gh: GitHub, partner: PartnerConfig, issue_numbers: list[int]) -> None:
    """Run the partner's deploy command once, then post + set deployed per issue."""
    subprocess.run(shlex.split(partner.deploy), check=False)
    for number in issue_numbers:
        gh.post_comment(
            partner.repo,
            number,
            "It's live — please have a look and let us know how it goes.",
        )
        issue = gh.get_issue(partner.repo, number)
        set_state(gh, issue, partner, "deployed")


def last_run_age(store: MutableMapping, *, now: Optional[datetime] = None) -> Optional[float]:
    """Seconds since the last non-dry-run `run_once`, or None if it never ran."""
    stamp = store.get("last_run")
    if not stamp:
        return None
    now = now if now is not None else datetime.now(timezone.utc)
    return (now - datetime.fromisoformat(stamp)).total_seconds()
