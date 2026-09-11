"""Tests for liaise.errors: stream parsing, the error taxonomy, and the tick's action table."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from liaise.errors import (
    ERROR_ACTIONS,
    ERROR_CLASSES,
    classify,
    classify_delivery_failure,
    defer_until,
    parse_stream,
    reset_time,
)
from liaise.state import STATE_LABELS

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _lines(*events) -> list[str]:
    return [json.dumps(e) for e in events]


def _result(**fields) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, **fields}


def _summary(*events):
    return parse_stream(_lines(*events))


# ---- parse_stream ----


def test_parse_stream_keeps_the_last_result_rate_limit_and_retry_errors():
    lines = _lines(
        {"type": "system", "subtype": "init"},
        {"type": "system", "subtype": "api_retry", "error": "overloaded"},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning"}},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}},
        _result(result="first"),
        _result(result="last"),
    ) + ["", "not json", "[1, 2]", '{"type": "assista']  # junk and a cut-off line

    summary = parse_stream(lines)

    assert summary.result["result"] == "last"
    assert summary.rate_limit == {"status": "rejected"}
    assert summary.api_errors == ("overloaded",)
    assert summary.event_count == 6


# ---- classify: no result event ----


def test_timed_out_wins_over_everything():
    assert classify(_summary(_result()), timed_out=True) == "timed_out"


def test_no_result_with_auth_text_on_stderr_is_auth_expired():
    assert classify(parse_stream([]), stderr_text="Not logged in · Please run /login") == "auth_expired"


def test_no_result_and_an_empty_stream_with_stderr_is_config_error():
    assert classify(parse_stream([]), stderr_text="error: unknown option '--bogus'") == "config_error"


def test_no_result_after_some_events_is_crashed():
    assert classify(_summary({"type": "system", "subtype": "init"})) == "crashed"


# ---- classify: a final result ----


def test_success_with_outcomes_is_not_an_error():
    assert classify(_summary(_result())) is None


def test_success_without_outcomes_needs_a_human():
    assert classify(_summary(_result()), has_outcomes=False) == "needs_human"


def test_a_successful_result_is_never_classified_by_its_text():
    summary = _summary(_result(result="Fixed the 'Invalid API key' banner; API Error: 529 handled."))
    assert classify(summary) is None


def test_auth_retry_error_is_auth_expired():
    summary = _summary(
        {"type": "system", "subtype": "api_retry", "error": "authentication_failed"},
        _result(is_error=True, subtype="error_during_execution"),
    )
    assert classify(summary) == "auth_expired"


def test_rejected_rate_limit_is_quota_exhausted():
    summary = _summary(
        {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resets_at": 1767272400}},
        _result(is_error=True, subtype="error_during_execution"),
    )
    assert classify(summary) == "quota_exhausted"


def test_max_turns_and_max_budget_are_budget_exceeded():
    for subtype in ("error_max_turns", "error_max_budget_usd"):
        assert classify(_summary(_result(is_error=True, subtype=subtype))) == "budget_exceeded"


def test_structured_output_retries_exhausted_needs_a_human():
    summary = _summary(_result(is_error=True, subtype="error_max_structured_output_retries"))
    assert classify(summary) == "needs_human"


def test_429_is_rate_limited_and_529_is_unavailable():
    rate = _summary(_result(is_error=True, subtype="error_during_execution", result="Request rejected (429)"))
    over = _summary(_result(is_error=True, subtype="error_during_execution", errors=["Repeated 529 Overloaded errors"]))
    assert classify(rate) == "rate_limited"
    assert classify(over) == "unavailable"


def test_model_not_found_is_config_error():
    summary = _summary(
        {"type": "system", "subtype": "api_retry", "error": "model_not_found"},
        _result(is_error=True, subtype="error_during_execution"),
    )
    assert classify(summary) == "config_error"


def test_usage_policy_refusal_is_policy_refusal():
    summary = _summary(_result(is_error=True, subtype="error_during_execution", result="Declined under the Usage Policy"))
    assert classify(summary) == "policy_refusal"


def test_error_with_permission_denials_needs_a_human():
    summary = _summary(_result(is_error=True, subtype="error_during_execution", permission_denials=[{"tool_name": "Bash"}]))
    assert classify(summary) == "needs_human"


def test_any_other_error_is_crashed():
    assert classify(_summary(_result(is_error=True, subtype="error_during_execution"))) == "crashed"


def test_first_match_wins_auth_before_unavailable():
    summary = _summary(_result(is_error=True, subtype="error_during_execution", result="API Error: 401 then 529 Overloaded"))
    assert classify(summary) == "auth_expired"


# ---- delivery failures ----


def test_refused_delivery_is_effect_blocked_and_other_failures_are_not():
    assert classify_delivery_failure("The job was not started because recent account payments have failed") == "effect_blocked"
    assert classify_delivery_failure("You've reached your spending limit for Actions minutes") == "effect_blocked"
    assert classify_delivery_failure("HTTP 403: Resource not accessible") == "effect_blocked"
    assert classify_delivery_failure("npm ERR! test failed in 1403 ms") is None
    assert classify_delivery_failure("") is None


# ---- the action table ----


def test_every_error_class_has_an_action_with_a_real_state():
    assert set(ERROR_ACTIONS) == set(ERROR_CLASSES)
    for error, action in ERROR_ACTIONS.items():
        assert action.state is None or action.state in STATE_LABELS, error


def test_auth_and_config_errors_hold_the_processor_and_blocked_effects_hold_deploys():
    assert ERROR_ACTIONS["auth_expired"].auto_hold == "processor"
    assert ERROR_ACTIONS["config_error"].auto_hold == "processor"
    assert ERROR_ACTIONS["effect_blocked"].auto_hold == "effect:deploy"


def test_crashes_and_timeouts_go_to_the_owner_and_count():
    for error in ("crashed", "timed_out"):
        action = ERROR_ACTIONS[error]
        assert (action.state, action.notify, action.counts) == ("needs-owner", True, True)


def test_quota_and_auth_do_not_count_against_the_daily_cap():
    assert not ERROR_ACTIONS["quota_exhausted"].counts
    assert not ERROR_ACTIONS["auth_expired"].counts


# ---- reset_time and defer_until ----


def test_reset_time_reads_epoch_seconds_milliseconds_and_iso():
    expected = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)
    assert reset_time({"resets_at": 1767272400}) == expected
    assert reset_time({"resets_at": 1767272400000}) == expected
    assert reset_time({"resets_at": "2026-01-01T13:00:00Z"}) == expected
    assert reset_time({"resets_at": "soon"}) is None
    assert reset_time(None) is None


def test_quota_defers_until_the_reset_or_a_fallback():
    reset = {"resets_at": "2026-01-01T13:00:00Z"}
    assert defer_until("quota_exhausted", now=T0, rate_limit=reset) == T0 + timedelta(hours=1)
    assert defer_until("quota_exhausted", now=T0) == T0 + timedelta(minutes=30)


def test_rate_limited_backs_off_exponentially_up_to_a_cap():
    assert defer_until("rate_limited", now=T0, attempt=0) == T0 + timedelta(minutes=2)
    assert defer_until("rate_limited", now=T0, attempt=3) == T0 + timedelta(minutes=16)
    assert defer_until("rate_limited", now=T0, attempt=10) == T0 + timedelta(minutes=30)


def test_fixed_deferrals_and_none():
    assert defer_until("unavailable", now=T0) == T0 + timedelta(minutes=5)
    assert defer_until("workspace_conflict", now=T0) == T0 + timedelta(minutes=10)
    assert defer_until("crashed", now=T0) is None
