"""liaise: the loop between a non-technical partner and a coding agent, over conversations.

A subject (``subjects/<slug>.toml``, :func:`load_subjects`) binds conversations, such as
GitHub issues or a web inbox, and says who may ask for what. Each tick (:func:`run_once`)
takes in what arrived as cases in the :class:`Ledger`, starts ready cases as detached
processor runs, and carries out their outcomes through the outbound gate
(:func:`run_gate`). ``liaise run --once --dry-run`` prints one tick's plan and changes
nothing.
"""

from liaise.access import authorize, resolve_person
from liaise.config import ConfigError, GlobalConfig, load_global_config
from liaise.gate import DFLT_OUTBOUND_FILTERS, run_gate
from liaise.github import FakeGitHub, GhCli, GitHub, GitHubError
from liaise.holds import hold, unhold
from liaise.intake import IntakeReport, intake
from liaise.ledger import Ledger, default_ledger_store
from liaise.migrate import migrate_config
from liaise.model import (
    Case,
    Health,
    Hold,
    LedgerEntry,
    Outcome,
    RunRecord,
    RunResult,
)
from liaise.notify import notify
from liaise.outcomes import OUTCOME_SCHEMA, parse_outcomes, plan_outcomes
from liaise.processor import ClaudeHeadless, EchoProcessor, Job, Processor
from liaise.subjects import Subject, load_subjects
from liaise.tick import TickReport, run_once, status_lines

__all__ = [
    "Case",
    "ClaudeHeadless",
    "ConfigError",
    "DFLT_OUTBOUND_FILTERS",
    "EchoProcessor",
    "FakeGitHub",
    "GhCli",
    "GitHub",
    "GitHubError",
    "GlobalConfig",
    "Health",
    "Hold",
    "IntakeReport",
    "Job",
    "Ledger",
    "LedgerEntry",
    "OUTCOME_SCHEMA",
    "Outcome",
    "Processor",
    "RunRecord",
    "RunResult",
    "Subject",
    "TickReport",
    "authorize",
    "default_ledger_store",
    "hold",
    "intake",
    "load_global_config",
    "load_subjects",
    "migrate_config",
    "notify",
    "parse_outcomes",
    "plan_outcomes",
    "resolve_person",
    "run_gate",
    "run_once",
    "status_lines",
    "unhold",
]
