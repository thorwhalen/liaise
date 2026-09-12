"""Processor error taxonomy (design §3.6): classify how a run ended, and what the tick does.

A run is classified from its `claude` stream-JSON output. Structured fields are checked
before strings, and the first match wins, so a run that failed authentication and then
crashed is `auth_expired`, whose action (hold the processor, count no dispatch) is the right
one. A run whose final result is a success is never classified by strings it happens to
contain.

:data:`ERROR_ACTIONS` is the single source of truth for what each class does to a case;
the tick consults it and decides nothing about error classes on its own.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Optional

ERROR_CLASSES = (
    "config_error",
    "auth_expired",
    "quota_exhausted",
    "rate_limited",
    "unavailable",
    "policy_refusal",
    "budget_exceeded",
    "timed_out",
    "crashed",
    "workspace_conflict",
    "needs_human",
    "effect_blocked",
)

_AUTH_API_ERRORS = ("authentication_failed", "oauth_org_not_allowed")
_AUTH_STRINGS = (
    "not logged in",
    "login expired",
    "oauth token revoked",
    "oauth token has expired",
    "invalid api key",
    "api error: 401",
)
_QUOTA_API_ERRORS = ("billing_error", "account_on_hold")
_QUOTA_STRINGS = ("you've hit your", "spend limit", "credits_required")
_BUDGET_SUBTYPES = ("error_max_turns", "error_max_budget_usd")
_STRUCTURED_OUTPUT_SUBTYPE = "error_max_structured_output_retries"
_UNAVAILABLE_API_ERRORS = ("overloaded", "server_error")
_CONFIG_API_ERRORS = ("model_not_found", "invalid_request")
_RATE_LIMITED_RE = re.compile(r"\b429\b|rate limit", re.IGNORECASE)
_UNAVAILABLE_RE = re.compile(
    r"\b529\b|overloaded|api error: 5\d\d|request timed out", re.IGNORECASE
)
_POLICY_RE = re.compile(r"usage polic", re.IGNORECASE)
_EFFECT_BLOCKED_RE = re.compile(
    r"spending limit|billing|payment|actions minutes|minutes quota|\b403\b",
    re.IGNORECASE,
)

#: Fallbacks and caps for deferrals, named rather than inlined.
DFLT_QUOTA_DEFER = timedelta(minutes=30)
RATE_LIMITED_DEFER_CAP = timedelta(minutes=30)


@dataclass(frozen=True)
class StreamSummary:
    """What classification and collection need from one run's stream-JSON output."""

    result: Optional[Mapping] = None
    rate_limit: Optional[Mapping] = None
    api_errors: tuple[str, ...] = ()
    event_count: int = 0


def parse_stream(lines: Iterable[str]) -> StreamSummary:
    """Read stream-JSON lines: the final `result` event, the last rate-limit info, retry errors.

    Lines that are blank, not JSON, or not objects are skipped: a stream cut off mid-line
    by a kill must still yield whatever came before it.
    """
    result: Optional[Mapping] = None
    rate_limit: Optional[Mapping] = None
    api_errors: list[str] = []
    count = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        count += 1
        kind = event.get("type")
        if kind == "result":
            result = event
        elif kind == "rate_limit_event":
            info = event.get("rate_limit_info")
            if isinstance(info, dict):
                rate_limit = info
        elif kind == "system" and event.get("subtype") == "api_retry":
            if event.get("error"):
                api_errors.append(str(event["error"]))
    return StreamSummary(
        result=result,
        rate_limit=rate_limit,
        api_errors=tuple(api_errors),
        event_count=count,
    )


def _result_text(result: Mapping) -> str:
    parts = [str(result.get("result") or "")]
    for error in result.get("errors") or ():
        parts.append(error if isinstance(error, str) else json.dumps(error))
    return "\n".join(parts)


def _contains_any(text: str, needles: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(needle in lowered for needle in needles)


def classify(
    summary: StreamSummary,
    *,
    stderr_text: str = "",
    timed_out: bool = False,
    has_outcomes: bool = True,
) -> Optional[str]:
    """The error class of a finished run, or None for a success with valid outcomes."""
    if timed_out:
        return "timed_out"

    result = summary.result
    if result is None:
        if _contains_any(stderr_text, _AUTH_STRINGS):
            return "auth_expired"
        if summary.event_count == 0 and stderr_text.strip():
            return "config_error"  # rejected before any stream: a bad flag or setup
        return "crashed"

    subtype = result.get("subtype")
    if not result.get("is_error") and subtype in (None, "success"):
        return None if has_outcomes else "needs_human"

    text = _result_text(result)
    errors = summary.api_errors
    if any(e in _AUTH_API_ERRORS for e in errors) or _contains_any(text, _AUTH_STRINGS):
        return "auth_expired"
    if (
        (summary.rate_limit or {}).get("status") == "rejected"
        or any(e in _QUOTA_API_ERRORS for e in errors)
        or _contains_any(text, _QUOTA_STRINGS)
    ):
        return "quota_exhausted"
    if subtype in _BUDGET_SUBTYPES:
        return "budget_exceeded"
    if subtype == _STRUCTURED_OUTPUT_SUBTYPE:
        return "needs_human"
    if "rate_limit" in errors or _RATE_LIMITED_RE.search(text):
        return "rate_limited"
    if any(e in _UNAVAILABLE_API_ERRORS for e in errors) or _UNAVAILABLE_RE.search(
        text
    ):
        return "unavailable"
    if any(e in _CONFIG_API_ERRORS for e in errors):
        return "config_error"
    if _POLICY_RE.search(text):
        return "policy_refusal"
    if result.get("permission_denials"):
        return "needs_human"
    return "crashed"


def classify_delivery_failure(output: str) -> Optional[str]:
    """`effect_blocked` when a failed delivery says it was refused (CI minutes, billing, 403).

    None for any other failure, which the tick reconciles to `needs-owner` as 0.0.x did.
    """
    return "effect_blocked" if _EFFECT_BLOCKED_RE.search(output or "") else None


@dataclass(frozen=True)
class ErrorAction:
    """What the tick does with a case whose run ended in one error class.

    `state` is the case's next state, or None to leave it unchanged; `intake` makes the
    case dispatchable again (its session id stays, so the next dispatch resumes).
    `counts` is whether the run counts against the daily dispatch cap.
    """

    state: Optional[str]
    auto_hold: Optional[str] = None
    notify: bool = False
    counts: bool = True
    defer: Optional[timedelta] = None


ERROR_ACTIONS: Mapping[str, ErrorAction] = {
    "config_error": ErrorAction("needs-owner", auto_hold="processor", notify=True),
    "auth_expired": ErrorAction(
        "intake", auto_hold="processor", notify=True, counts=False
    ),
    "quota_exhausted": ErrorAction("intake", notify=True, counts=False),
    "rate_limited": ErrorAction("intake", counts=False, defer=timedelta(minutes=2)),
    "unavailable": ErrorAction("intake", counts=False, defer=timedelta(minutes=5)),
    "policy_refusal": ErrorAction("needs-owner", notify=True),
    "budget_exceeded": ErrorAction("needs-owner", notify=True),
    "timed_out": ErrorAction("needs-owner", notify=True),
    "crashed": ErrorAction("needs-owner", notify=True),
    "workspace_conflict": ErrorAction(None, counts=False, defer=timedelta(minutes=10)),
    "needs_human": ErrorAction("needs-owner", notify=True),
    "effect_blocked": ErrorAction(
        "needs-owner", auto_hold="effect:deploy", notify=True
    ),
}


def reset_time(rate_limit: Optional[Mapping]) -> Optional[datetime]:
    """When a rejected quota resets, from `rate_limit_info.resets_at` (epoch or ISO), if given."""
    raw = (rate_limit or {}).get("resets_at")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        seconds = raw / 1000 if raw > 1e12 else raw  # tolerate milliseconds
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def defer_until(
    error: str,
    *,
    now: datetime,
    attempt: int = 0,
    rate_limit: Optional[Mapping] = None,
) -> Optional[datetime]:
    """When a case whose run ended in `error` may be dispatched again, or None for no deferral."""
    if error == "quota_exhausted":
        return reset_time(rate_limit) or now + DFLT_QUOTA_DEFER
    base = ERROR_ACTIONS[error].defer
    if base is None:
        return None
    if error == "rate_limited":
        return now + min(base * (2**attempt), RATE_LIMITED_DEFER_CAP)
    return now + base
