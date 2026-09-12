"""Tests for liaise.readiness: the 0.0.x quiet-window and marker arithmetic, on a case.

Ported from the readiness half of test_intake.py, one test per case covered there, plus
the checks that come with reading entries: only ``message`` entries count, and markers
are ordered by time rather than by entry order.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from liaise.config import Markers
from liaise.model import Case, LedgerEntry
from liaise.readiness import Readiness, compute_readiness, last_partner_activity

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
PARTNERS = ("pat",)
MARKERS = Markers(go="#startwork#", wait="#wait#")
#: Someone other than the partner (the operator, say).
OTHER = "ada-lovelace"


def _message(actor, text, *, minutes=0, kind="message") -> LedgerEntry:
    return LedgerEntry(at=T0 + timedelta(minutes=minutes), kind=kind, actor=actor, text=text)


def _case(*entries, body="something is broken", updated_at=T0) -> Case:
    """A case pat opened at T0 with ``body``, followed by ``entries``."""
    return Case(
        id="pat-1",
        subject="pat",
        conversations=("github:example/app#1",),
        reporter="pat",
        state="intake",
        created_at=T0,
        updated_at=updated_at,
        entries=(_message("pat", body), *entries),
    )


def _readiness(case, *, after, quiet_minutes=10, go_minutes=2) -> Readiness:
    return compute_readiness(
        case,
        quiet_minutes=quiet_minutes,
        go_minutes=go_minutes,
        markers=MARKERS,
        partner_persons=PARTNERS,
        now=T0 + after,
    )


# ---- last partner activity: the quiet-window clock ----


def test_last_activity_is_case_creation_when_no_further_activity():
    assert last_partner_activity(_case(), partner_persons=PARTNERS) == T0


def test_last_activity_moves_with_a_partner_message():
    case = _case(_message("pat", "also this", minutes=5))
    assert last_partner_activity(case, partner_persons=PARTNERS) == T0 + timedelta(minutes=5)


def test_non_partner_message_does_not_move_the_clock():
    case = _case(_message(OTHER, "looking into it", minutes=60))
    assert last_partner_activity(case, partner_persons=PARTNERS) == T0


def test_case_updated_at_moved_by_anyone_does_not_move_the_clock():
    """H-2, ported: `updated_at` moves with every entry, anyone's. Reading it as
    partner activity would let the operator's comment reset the partner's quiet window.
    """
    case = _case(_message(OTHER, "hi", minutes=30), updated_at=T0 + timedelta(minutes=30))
    assert last_partner_activity(case, partner_persons=PARTNERS) == T0


def test_liaise_own_entries_do_not_move_the_clock():
    """H-2, the other half: liaise's own transitions and label projections."""
    later = T0 + timedelta(hours=2)
    case = _case(
        LedgerEntry(at=later, kind="transition", detail={"from": "intake", "to": "paused"}),
        LedgerEntry(at=later, kind="projection"),
        updated_at=later,
    )
    assert last_partner_activity(case, partner_persons=PARTNERS) == T0


def test_only_message_entries_count_whoever_the_actor():
    case = _case(_message("pat", "#startwork#", minutes=5, kind="outcome"))
    assert last_partner_activity(case, partner_persons=PARTNERS) == T0
    assert not _readiness(case, quiet_minutes=60, after=timedelta(minutes=8)).ready


def test_non_partner_message_does_not_make_the_case_ready_early():
    case = _case(_message(OTHER, "looking into it", minutes=9))
    # 11 minutes after creation: ready by the quiet window, even though someone
    # else wrote at +9m, which would otherwise still be inside a 10-minute window.
    assert _readiness(case, after=timedelta(minutes=11)).ready


def test_partner_persons_must_be_a_collection_not_one_string():
    with pytest.raises(TypeError, match="collection of person ids"):
        last_partner_activity(_case(), partner_persons="pat")


def test_message_without_text_moves_the_clock_but_is_no_marker():
    case = _case(_message("pat", None, minutes=5))
    readiness = _readiness(case, after=timedelta(minutes=6))
    assert not readiness.ready
    assert readiness.last_activity == T0 + timedelta(minutes=5)


# ---- quiet window ----


def test_not_ready_before_quiet_window_elapses():
    readiness = _readiness(_case(), after=timedelta(minutes=5))
    assert not readiness.ready
    assert readiness.countdown == timedelta(minutes=5)
    assert readiness.reason == "waiting"


def test_ready_once_quiet_window_elapses():
    readiness = _readiness(_case(), after=timedelta(minutes=10))
    assert readiness.ready
    assert readiness.countdown == timedelta(0)
    assert readiness.reason == "quiet window elapsed"


# ---- go marker ----


def test_go_marker_in_opening_message_shortens_wait():
    case = _case(body="please fix this #startwork#")
    # far short of the 60-minute quiet window, but past the 2-minute go window
    readiness = _readiness(case, quiet_minutes=60, after=timedelta(minutes=3))
    assert readiness.ready
    assert readiness.reason == "go marker"


def test_go_marker_not_ready_before_go_minutes_elapses():
    """M-5 boundary regression, ported: the go marker must wait the full
    `go_minutes`, not fire the moment it exists. Checked one second before the deadline.
    """
    case = _case(body="please fix this #startwork#")
    after = timedelta(minutes=2) - timedelta(seconds=1)
    assert not _readiness(case, quiet_minutes=60, after=after).ready


def test_go_marker_ready_exactly_at_go_minutes():
    case = _case(body="please fix this #startwork#")
    readiness = _readiness(case, quiet_minutes=60, after=timedelta(minutes=2))
    assert readiness.ready
    assert readiness.reason == "go marker"


def test_countdown_runs_to_the_earlier_deadline():
    case = _case(body="#startwork#")
    readiness = _readiness(case, quiet_minutes=60, after=timedelta(minutes=1))
    assert not readiness.ready
    assert readiness.countdown == timedelta(minutes=1)


def test_go_marker_in_a_later_partner_message():
    case = _case(_message("pat", "ok #startwork#", minutes=5))
    assert _readiness(case, quiet_minutes=60, after=timedelta(minutes=8)).ready


def test_go_marker_in_non_partner_message_is_ignored():
    case = _case(_message(OTHER, "#startwork#", minutes=5))
    assert not _readiness(case, quiet_minutes=60, after=timedelta(minutes=8)).ready


def test_marker_matched_case_insensitively():
    case = _case(body="Ready now #STARTWORK#")
    assert _readiness(case, quiet_minutes=60, after=timedelta(minutes=3)).ready


# ---- wait marker pauses ----


def test_wait_marker_pauses_even_past_quiet_window():
    readiness = _readiness(_case(body="not yet #wait#"), after=timedelta(hours=5))
    assert readiness.paused
    assert not readiness.ready
    assert readiness.countdown == timedelta(0)
    assert readiness.reason == "partner asked to wait"


def test_go_after_wait_unpauses():
    case = _case(
        _message("pat", "#wait#", minutes=1),
        _message("pat", "#startwork#", minutes=10),
    )
    readiness = _readiness(case, quiet_minutes=60, after=timedelta(minutes=13))
    assert readiness.ready
    assert not readiness.paused


def test_wait_after_go_stays_paused():
    case = _case(
        _message("pat", "#startwork#", minutes=1),
        _message("pat", "#wait#", minutes=10),
    )
    assert _readiness(case, quiet_minutes=60, after=timedelta(minutes=13)).paused


def test_markers_are_ordered_by_time_not_by_entry_order():
    case = _case(
        _message("pat", "#wait#", minutes=10),
        _message("pat", "#startwork#", minutes=1),
    )
    assert _readiness(case, quiet_minutes=60, after=timedelta(minutes=13)).paused
