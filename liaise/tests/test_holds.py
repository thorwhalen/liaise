"""Tests for liaise.holds: scope forms, setting and lifting holds, what each mode stops, the
most specific hold, the scopes work falls under, automatic holds, and dry runs."""

from __future__ import annotations

import copy
from collections import ChainMap
from datetime import datetime, timedelta, timezone

import pytest

from liaise.holds import (
    BLOCKING_MODES,
    SCOPE_KINDS,
    SCOPE_SPECIFICITY,
    auto_hold,
    blocking_hold,
    canonical_scope,
    hold,
    parse_scope,
    release_auto_holds,
    scopes_for,
    unhold,
)
from liaise.ledger import Ledger
from liaise.model import HOLD_MODES, Hold

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
REPO = "repo:example/app"


@pytest.fixture
def ledger() -> Ledger:
    return Ledger({})


def _work_scopes(**overrides):
    """The scopes a start on the fictional subject falls under."""
    return scopes_for(**{"subject": "example-app", "person": "pat", "repo": "example/app", **overrides})


# ---- scope forms ----

VALID_SCOPES = [
    ("global", ("global", None)),
    ("processor", ("processor", None)),
    ("subject:example-app", ("subject", "example-app")),
    ("person:pat", ("person", "pat")),
    ("repo:example/app", ("repo", "example/app")),
    ("checkout:~/code/example-app", ("checkout", "~/code/example-app")),
    ("checkout:C:/code/example-app", ("checkout", "C:/code/example-app")),
    ("effect:deploy", ("effect", "deploy")),
]


@pytest.mark.parametrize(("scope", "parsed"), VALID_SCOPES)
def test_parse_scope_accepts_every_form(scope, parsed):
    assert parse_scope(scope) == parsed


def test_the_valid_forms_cover_every_scope_kind_and_specificity_orders_each_once():
    assert {parsed[0] for _, parsed in VALID_SCOPES} == set(SCOPE_KINDS)
    assert sorted(SCOPE_SPECIFICITY) == sorted(SCOPE_KINDS)


@pytest.mark.parametrize(
    "scope",
    [
        "",
        "everything",
        "Global",
        "repo",
        "repo:",
        "subject:   ",
        "global:all",
        "processor:claude",
        ":pat",
        "project:example-app",
        None,
        42,
    ],
)
def test_parse_scope_refuses_anything_else_and_names_the_accepted_forms(scope):
    with pytest.raises(ValueError, match="accepted forms") as raised:
        parse_scope(scope)
    for form in ("global", "processor", "subject:<slug>", "checkout:<path>", "effect:<kind>"):
        assert form in str(raised.value)


def test_parse_scope_trims_the_value():
    assert parse_scope("person: pat ") == ("person", "pat")


def test_a_checkout_hold_matches_every_spelling_of_the_path(ledger, tmp_path, monkeypatch):
    checkout = tmp_path / "code" / "example-app"
    checkout.mkdir(parents=True)
    detour = tmp_path / "code" / ".." / "code" / "example-app"
    placed = hold(ledger, f"checkout:{detour}", now=T0)
    assert placed.scope == f"checkout:{checkout.resolve()}"
    assert blocking_hold(ledger, scopes_for(checkout=checkout), for_="start") == placed

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert canonical_scope("checkout:~/code/example-app") == placed.scope
    assert unhold(ledger, "checkout:~/code/example-app") is True
    assert list(ledger.holds()) == []


def test_canonical_scope_leaves_other_values_as_given():
    assert canonical_scope("repo:example/app") == "repo:example/app"
    assert canonical_scope("global") == "global"


# ---- setting and lifting ----


def test_hold_records_its_mode_reason_setter_and_time(ledger):
    placed = hold(ledger, REPO, mode="drain", reason="release freeze", set_by="operator", now=T0)
    assert placed == Hold(scope=REPO, mode="drain", reason="release freeze", set_by="operator", set_at=T0)
    assert ledger.get_hold(REPO) == placed


def test_hold_defaults_to_an_operator_block_stamped_now(ledger):
    before = datetime.now(timezone.utc)
    placed = hold(ledger, "global")
    assert (placed.mode, placed.set_by, placed.reason) == ("block", "operator", "")
    assert before <= placed.set_at <= datetime.now(timezone.utc)


def test_a_second_hold_on_a_scope_replaces_the_first(ledger):
    hold(ledger, REPO, mode="block", reason="first", now=T0)
    second = hold(ledger, REPO, mode="cancel", reason="second", now=T0 + timedelta(minutes=5))
    assert list(ledger.holds()) == [second]


def test_hold_refuses_a_bad_scope_or_mode_and_writes_nothing(ledger):
    with pytest.raises(ValueError, match="accepted forms"):
        hold(ledger, "repo", now=T0)
    with pytest.raises(ValueError, match="hold mode 'pause'"):
        hold(ledger, REPO, mode="pause", now=T0)
    assert ledger.store == {}


def test_unhold_lifts_the_hold_and_says_whether_there_was_one(ledger):
    hold(ledger, REPO, now=T0)
    hold(ledger, "global", now=T0)
    assert unhold(ledger, REPO) is True
    assert unhold(ledger, REPO) is False
    assert [h.scope for h in ledger.holds()] == ["global"]
    with pytest.raises(ValueError, match="accepted forms"):
        unhold(ledger, "repo")


# ---- the scopes work falls under ----


def test_scopes_for_always_includes_global_and_counts_the_processor_by_default(tmp_path):
    assert scopes_for() == ("processor", "global")
    assert scopes_for(processor=False) == ("global",)
    assert scopes_for(effect="deploy", processor=False) == ("effect:deploy", "global")


def test_scopes_for_gives_one_scope_per_argument_most_specific_first(tmp_path):
    checkout = tmp_path / "code" / "example-app"
    checkout.mkdir(parents=True)
    scopes = scopes_for(
        subject="example-app",
        person="pat",
        repo="example/app",
        checkout=tmp_path / "code" / ".." / "code" / "example-app",
        effect="deploy",
    )
    assert scopes == (
        f"checkout:{checkout.resolve()}",
        "repo:example/app",
        "person:pat",
        "subject:example-app",
        "effect:deploy",
        "processor",
        "global",
    )


def test_scopes_for_refuses_an_empty_value():
    with pytest.raises(ValueError, match="accepted forms"):
        scopes_for(repo="")


# ---- what each mode stops ----


@pytest.mark.parametrize(
    ("mode", "for_", "stops"),
    [
        ("block", "start", True),
        ("drain", "start", True),
        ("cancel", "start", True),
        ("block", "effect", True),
        ("drain", "effect", False),
        ("cancel", "effect", True),
        ("block", "running", False),
        ("drain", "running", False),
        ("cancel", "running", True),
    ],
)
def test_what_each_mode_stops(ledger, mode, for_, stops):
    placed = hold(ledger, REPO, mode=mode, now=T0)
    assert blocking_hold(ledger, _work_scopes(), for_=for_) == (placed if stops else None)


def test_the_checks_are_start_effect_and_running_over_known_modes():
    assert set(BLOCKING_MODES) == {"start", "effect", "running"}
    assert all(set(modes) <= set(HOLD_MODES) for modes in BLOCKING_MODES.values())


def test_holds_on_other_scopes_stop_nothing(ledger):
    hold(ledger, "repo:example/other", mode="cancel", now=T0)
    hold(ledger, "person:sam", mode="cancel", now=T0)
    hold(ledger, "subject:example-site", mode="cancel", now=T0)
    hold(ledger, "effect:deploy", mode="cancel", now=T0)
    for for_ in BLOCKING_MODES:
        assert blocking_hold(ledger, _work_scopes(), for_=for_) is None


def test_a_processor_hold_stops_work_that_involves_the_processor_only(ledger):
    placed = hold(ledger, "processor", now=T0)
    assert blocking_hold(ledger, _work_scopes(), for_="start") == placed
    assert blocking_hold(ledger, scopes_for(effect="deploy", processor=False), for_="effect") is None


def test_blocking_hold_refuses_an_unknown_check(ledger):
    with pytest.raises(ValueError, match="hold check 'dispatch'"):
        blocking_hold(ledger, _work_scopes(), for_="dispatch")


# ---- the most specific hold ----


def test_blocking_hold_returns_the_most_specific_hold(ledger, tmp_path):
    scopes = _work_scopes(checkout=tmp_path / "code" / "example-app", effect="deploy")
    assert [parse_scope(scope)[0] for scope in scopes] == list(SCOPE_SPECIFICITY)
    for scope in scopes[::-1]:  # broadest first, so the order they were set in is no answer
        hold(ledger, scope, reason=scope, now=T0)
    found_in_order = []
    while (found := blocking_hold(ledger, scopes[::-1], for_="start")) is not None:
        found_in_order.append(parse_scope(found.scope)[0])
        unhold(ledger, found.scope)
    assert found_in_order == list(SCOPE_SPECIFICITY)


def test_a_narrow_hold_that_does_not_stop_the_check_gives_way_to_a_broader_one(ledger):
    narrow = hold(ledger, REPO, mode="drain", now=T0)
    broad = hold(ledger, "global", mode="block", now=T0)
    assert blocking_hold(ledger, _work_scopes(), for_="start") == narrow
    assert blocking_hold(ledger, _work_scopes(), for_="effect") == broad
    assert blocking_hold(ledger, _work_scopes(), for_="running") is None


# ---- automatic holds ----


def test_auto_hold_is_a_block_recorded_as_set_by_its_error_class(ledger):
    placed = auto_hold(ledger, "effect:deploy", error_class="effect_blocked", now=T0)
    assert (placed.scope, placed.mode, placed.set_by, placed.set_at) == (
        "effect:deploy",
        "block",
        "auto:effect_blocked",
        T0,
    )
    assert "effect_blocked" in placed.reason
    assert ledger.get_hold("effect:deploy") == placed


def test_release_auto_holds_lifts_only_the_ticks_own_hold(ledger):
    auto = auto_hold(ledger, "processor", error_class="auth_expired", now=T0)
    operator = hold(ledger, "effect:deploy", reason="billing review", now=T0)
    assert release_auto_holds(ledger, "processor") == [auto]
    assert release_auto_holds(ledger, "processor") == []
    assert release_auto_holds(ledger, "effect:deploy") == []
    assert list(ledger.holds()) == [operator]


def test_auto_hold_leaves_an_operators_hold_on_the_scope_in_place(ledger):
    operator = hold(ledger, "processor", mode="cancel", reason="switching accounts", now=T0)
    later = T0 + timedelta(minutes=1)
    assert auto_hold(ledger, "processor", error_class="config_error", now=later) == operator
    assert release_auto_holds(ledger, "processor") == []
    assert ledger.get_hold("processor") == operator


def test_an_operator_hold_over_an_auto_hold_is_not_released(ledger):
    auto_hold(ledger, "processor", error_class="auth_expired", now=T0)
    operator = hold(ledger, "processor", reason="keep it off for now", now=T0)
    assert release_auto_holds(ledger, "processor") == []
    assert ledger.get_hold("processor") == operator


def test_a_later_auto_hold_replaces_an_earlier_one(ledger):
    auto_hold(ledger, "processor", error_class="config_error", now=T0)
    later = auto_hold(ledger, "processor", error_class="auth_expired", now=T0 + timedelta(minutes=1))
    assert later.set_by == "auto:auth_expired"
    assert list(ledger.holds()) == [later]


def test_auto_hold_needs_an_error_class(ledger):
    with pytest.raises(ValueError, match="error class"):
        auto_hold(ledger, "processor", error_class="", now=T0)
    assert ledger.store == {}


# ---- dry runs ----


def test_a_dry_run_ledger_holds_and_lifts_without_touching_the_real_one():
    base: dict = {}
    real = Ledger(base)
    hold(real, REPO, reason="release freeze", now=T0)
    auto_hold(real, "processor", error_class="auth_expired", now=T0)
    snapshot = copy.deepcopy(base)

    dry = Ledger(ChainMap({}, base))
    hold(dry, "global", mode="cancel", now=T0)
    assert unhold(dry, REPO) is True
    assert [h.scope for h in release_auto_holds(dry, "processor")] == ["processor"]
    auto_hold(dry, "effect:deploy", error_class="effect_blocked", now=T0)
    # the dry ledger sees its own changes...
    assert sorted(h.scope for h in dry.holds()) == ["effect:deploy", "global"]
    assert blocking_hold(dry, _work_scopes(), for_="running").scope == "global"
    # ...while the real one is exactly as it was
    assert base == snapshot
    assert blocking_hold(real, _work_scopes(), for_="start").scope == REPO
    assert sorted(h.scope for h in real.holds()) == ["processor", REPO]
