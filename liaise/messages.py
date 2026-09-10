"""Partner-facing comment templates `liaise` posts on its own (#20).

An issue filed through the app is authored by the app's own credentials, not
the partner — GitHub subscribes the partner to nothing and sends them no
email unless a comment `@mentions` their login by name. Every template here
starts with that mention. Centralized so tests assert over these functions
directly rather than duplicating the copy (and risking it drifting from the
one actually posted).
"""

from __future__ import annotations

from liaise.config import PartnerConfig


def mention(partner: PartnerConfig) -> str:
    """The exact `@login` text every partner-facing comment must start with."""
    return f"@{partner.notify_login}"


def budget_capped_message(partner: PartnerConfig) -> str:
    """Posted once when a partner's daily dispatch cap trips."""
    return (
        f"{mention(partner)} Today's limit on automatic work has been reached "
        "— this will pick back up tomorrow."
    )


def deployed_message(partner: PartnerConfig) -> str:
    """Posted once a batch deploy lands and an issue is told it's live."""
    return f"{mention(partner)} It's live — please have a look and let us know how it goes."


def nudge_message(partner: PartnerConfig) -> str:
    """Posted once for a `deployed` issue gone quiet past `deployed_nudge_days`."""
    return (
        f"{mention(partner)} Just checking in — did you get a chance to try this? "
        "Let us know how it goes."
    )
