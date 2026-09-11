"""Tests for liaise.ledger: dedupe, the case counter, the conversation index, transitions,
runs, holds, the unrouted queue, cursors (through correspond's listen), the default
store, and the dry-run overlay."""

from __future__ import annotations

import copy
from collections import ChainMap
from datetime import date, datetime, timedelta, timezone

import pytest
from correspond import listen
from correspond.model import ConversationRef, Event

from liaise.ledger import Ledger, default_ledger_store
from liaise.model import Case, Hold, LedgerEntry, RunRecord

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
REF = "github:example/app#12"
OTHER_REF = "github:example/app#13"
WEB_REF = "webinbox:example-site"
FAKE_REF = "fake:example/demo"
REPO_SCOPE = "repo:example/app"
CHECKOUT_SCOPE = "checkout:~/code/example-app"


@pytest.fixture
def ledger() -> Ledger:
    return Ledger({})


def _message(*, minutes=0, text="also on Safari") -> LedgerEntry:
    return LedgerEntry(at=T0 + timedelta(minutes=minutes), kind="message", actor="pat", text=text)


def _run(run_id="r1", *, status="running") -> RunRecord:
    return RunRecord(
        run_id=run_id, case_id="pat-1", subject="pat", mode="fresh", status=status, started_at=T0
    )


# ---- inbox ----


def test_mark_seen_dedupes_on_delivery_id(ledger):
    assert not ledger.seen("delivery-1")
    ledger.mark_seen("delivery-1", channel="github", kind="message", at=T0)
    assert ledger.seen("delivery-1")
    assert not ledger.seen("delivery-2")
    (key,) = ledger.store
    assert key.startswith("inbox__") and len(key) == len("inbox__") + 16
    assert ledger.store[key] == {
        "delivery_id": "delivery-1",
        "channel": "github",
        "kind": "message",
        "at": T0.isoformat(),
    }


# ---- cases: the counter ----


def test_new_case_ids_count_up(ledger):
    first = ledger.new_case("pat", REF, reporter="pat", at=T0)
    second = ledger.new_case("pat", OTHER_REF, reporter="pat", at=T0)
    third = ledger.new_case("example-site", WEB_REF, reporter="pat", at=T0)
    assert [first.id, second.id, third.id] == ["pat-1", "pat-2", "example-site-3"]
    assert ledger.store["counter__cases"] == 3


def test_new_case_never_overwrites_a_case_when_the_counter_was_lost(ledger):
    ledger.new_case("pat", REF, reporter="pat", at=T0)
    del ledger.store["counter__cases"]
    assert ledger.new_case("pat", OTHER_REF, reporter="pat", at=T0).id == "pat-2"
    assert ledger.get_case("pat-1").conversations == (REF,)


def test_new_case_opens_in_intake_and_is_stored(ledger):
    case = ledger.new_case("pat", REF, reporter="pat", at=T0)
    assert case == Case(
        id="pat-1",
        subject="pat",
        conversations=(REF,),
        reporter="pat",
        state="intake",
        created_at=T0,
        updated_at=T0,
    )
    assert ledger.get_case("pat-1") == case
    assert ledger.get_case("pat-99") is None


# ---- cases: the conversation index ----


def test_new_case_indexes_its_conversation(ledger):
    case = ledger.new_case("pat", REF, reporter="pat", at=T0)
    assert ledger.case_for_conversation(REF) == case
    assert ledger.case_for_conversation(OTHER_REF) is None


def test_new_case_refuses_a_conversation_that_already_has_a_case(ledger):
    ledger.new_case("pat", REF, reporter="pat", at=T0)
    with pytest.raises(ValueError, match="already belongs to case 'pat-1'"):
        ledger.new_case("pat", REF, reporter="pat", at=T0)
    assert ledger.store["counter__cases"] == 1  # no number handed out


def test_add_conversation_indexes_it_and_is_idempotent(ledger):
    ledger.new_case("pat", REF, reporter="pat", at=T0)
    assert ledger.add_conversation("pat-1", WEB_REF).conversations == (REF, WEB_REF)
    assert ledger.add_conversation("pat-1", WEB_REF).conversations == (REF, WEB_REF)
    assert ledger.case_for_conversation(WEB_REF).id == "pat-1"
    assert ledger.get_case("pat-1").conversations == (REF, WEB_REF)


def test_a_conversation_belongs_to_one_case_only(ledger):
    ledger.new_case("pat", REF, reporter="pat", at=T0)
    ledger.new_case("pat", OTHER_REF, reporter="pat", at=T0)
    before = copy.deepcopy(ledger.store)
    with pytest.raises(ValueError, match="belongs to case 'pat-1', not 'pat-2'"):
        ledger.add_conversation("pat-2", REF)
    assert ledger.store == before


# ---- cases: entries and transitions ----


def test_append_adds_an_entry_and_saves_it(ledger):
    ledger.new_case("pat", REF, reporter="pat", at=T0)
    entry = _message(minutes=5)
    assert ledger.append("pat-1", entry).entries == (entry,)
    stored = ledger.get_case("pat-1")
    assert stored.entries == (entry,)
    assert stored.updated_at == T0 + timedelta(minutes=5)


def test_transition_is_saved_with_its_entry(ledger):
    ledger.new_case("pat", REF, reporter="pat", at=T0)
    at = T0 + timedelta(minutes=10)
    ledger.transition("pat-1", "working", at=at, actor="liaise", reason="quiet window elapsed")
    case = ledger.get_case("pat-1")
    assert case.state == "working"
    (entry,) = case.entries
    assert (entry.kind, entry.at, entry.actor) == ("transition", at, "liaise")
    assert entry.detail == {"from": "intake", "to": "working", "reason": "quiet window elapsed"}


def test_transition_to_an_unknown_state_raises_and_writes_nothing(ledger):
    ledger.new_case("pat", REF, reporter="pat", at=T0)
    before = copy.deepcopy(ledger.store)
    with pytest.raises(ValueError, match="case state 'done'"):
        ledger.transition("pat-1", "done", at=T0)
    assert ledger.store == before


@pytest.mark.parametrize(
    "operation",
    [
        lambda lg: lg.append("pat-9", _message()),
        lambda lg: lg.transition("pat-9", "working", at=T0),
        lambda lg: lg.add_conversation("pat-9", REF),
    ],
)
def test_operations_on_an_unknown_case_raise_key_error(ledger, operation):
    with pytest.raises(KeyError, match="no case 'pat-9'"):
        operation(ledger)


def test_cases_filter_by_subject_and_state(ledger):
    ledger.new_case("pat", REF, reporter="pat", at=T0)
    ledger.new_case("pat", OTHER_REF, reporter="pat", at=T0)
    ledger.new_case("example-site", WEB_REF, reporter="pat", at=T0)
    ledger.transition("pat-2", "working", at=T0)
    assert sorted(c.id for c in ledger.cases()) == ["example-site-3", "pat-1", "pat-2"]
    assert sorted(c.id for c in ledger.cases(subject="pat")) == ["pat-1", "pat-2"]
    assert [c.id for c in ledger.cases(state="working")] == ["pat-2"]
    assert list(ledger.cases(subject="example-site", state="working")) == []


def test_cases_refuses_an_unknown_state_filter_at_once(ledger):
    with pytest.raises(ValueError, match="case state 'needs_owner'"):
        ledger.cases(state="needs_owner")


# ---- runs ----


def test_runs_save_get_and_filter_by_status(ledger):
    ledger.save_run(_run("r1"))
    ledger.save_run(_run("r2", status="finished"))
    assert ledger.get_run("r1") == _run("r1")
    assert ledger.get_run("r9") is None
    assert sorted(r.run_id for r in ledger.runs()) == ["r1", "r2"]
    assert [r.run_id for r in ledger.runs(status="finished")] == ["r2"]
    ledger.save_run(_run("r1", status="finished"))
    assert sorted(r.run_id for r in ledger.runs(status="finished")) == ["r1", "r2"]


# ---- holds ----


def test_holds_set_get_and_clear(ledger):
    repo_hold = Hold(scope=REPO_SCOPE, mode="block", reason="maintenance", set_by="operator", set_at=T0)
    checkout_hold = Hold(scope=CHECKOUT_SCOPE, mode="drain")
    ledger.set_hold(repo_hold)
    ledger.set_hold(checkout_hold)
    assert ledger.get_hold(REPO_SCOPE) == repo_hold
    assert sorted(h.scope for h in ledger.holds()) == [CHECKOUT_SCOPE, REPO_SCOPE]
    ledger.clear_hold(REPO_SCOPE)
    assert ledger.get_hold(REPO_SCOPE) is None
    assert list(ledger.holds()) == [checkout_hold]


def test_clearing_a_scope_with_no_hold_changes_nothing(ledger):
    ledger.clear_hold("global")
    assert ledger.store == {}


# ---- the unrouted queue ----


def test_unrouted_queue_is_keyed_and_deduplicated_on_delivery_id(ledger):
    fields = dict(
        delivery_id="delivery-1",
        subject="pat",
        author="github:ada-lovelace",
        grade="platform",
        reason="label claim by an untrusted author",
        url="https://github.com/example/app/issues/12",
        at=T0,
    )
    ledger.add_unrouted(**fields)
    ledger.add_unrouted(**fields)
    (item,) = ledger.unrouted()
    assert item == {**fields, "at": T0.isoformat()}


def test_unrouted_items_are_copies(ledger):
    ledger.add_unrouted(delivery_id="delivery-1", reason="unresolved sender")
    next(ledger.unrouted())["reason"] = "edited"
    assert next(ledger.unrouted())["reason"] == "unresolved sender"


def test_add_unrouted_requires_a_delivery_id(ledger):
    with pytest.raises(ValueError, match="needs a delivery_id"):
        ledger.add_unrouted(subject="pat", reason="unresolved sender")


# ---- daily counters ----


def test_daily_counter_counts_per_subject_and_day(ledger):
    day = date(2026, 1, 1)
    assert ledger.daily_count("pat", day) == 0
    assert ledger.increment_daily("pat", day) == 1
    assert ledger.increment_daily("pat", "2026-01-01") == 2
    assert ledger.daily_count("pat", T0) == 2  # a datetime counts as its date
    assert ledger.daily_count("pat", date(2026, 1, 2)) == 0
    assert ledger.daily_count("example-site", day) == 0
    assert ledger.store["daily__pat__2026-01-01"] == 2  # the 0.0.x key


def test_decrement_daily_takes_one_back_and_never_goes_below_zero(ledger):
    day = date(2026, 1, 1)
    ledger.increment_daily("pat", day)
    ledger.increment_daily("pat", day)
    assert ledger.decrement_daily("pat", day) == 1
    assert ledger.decrement_daily("pat", "2026-01-01") == 0
    assert ledger.decrement_daily("pat", day) == 0
    assert ledger.daily_count("pat", day) == 0
    assert ledger.decrement_daily("example-site", day) == 0
    assert "daily__example-site__2026-01-01" not in ledger.store  # nothing to take back, nothing written


def test_the_daily_cap_notice_is_remembered_per_subject_and_day(ledger):
    assert not ledger.daily_cap_notified("example-app", date(2026, 1, 1))
    ledger.mark_daily_cap_notified("example-app", date(2026, 1, 1), at=T0)
    assert ledger.daily_cap_notified("example-app", "2026-01-01")
    assert ledger.daily_cap_notified("example-app", T0)  # a datetime counts as its date
    assert not ledger.daily_cap_notified("example-app", date(2026, 1, 2))
    assert not ledger.daily_cap_notified("example-site", date(2026, 1, 1))
    assert ledger.store == {"budget_notified__example-app__2026-01-01": T0.isoformat()}  # the 0.0.x key shape


# ---- cursors ----


def test_cursors_are_a_mapping_of_encoded_refs(ledger):
    cursors = ledger.cursors
    assert cursors is ledger.cursors
    assert dict(cursors) == {}
    cursors[REF] = "c1"
    cursors[WEB_REF] = "w1"
    assert cursors[REF] == "c1"
    assert cursors.get(OTHER_REF) is None
    assert dict(cursors) == {REF: "c1", WEB_REF: "w1"}
    assert len(cursors) == 2
    del cursors[REF]
    assert REF not in cursors
    with pytest.raises(KeyError):
        del cursors[REF]
    assert [key for key in ledger.store if not key.startswith("cursor__")] == []


class _Listener:
    """The least a correspond listener needs: events 1 to 3 on any conversation, each with its cursor."""

    name = "fake"

    def parse_ref(self, id):
        return ConversationRef(channel=self.name, id=id)

    def poll(self, ref, *, cursor=None, limit=None):
        for n in range(int(cursor or 0) + 1, 4):
            yield Event(
                kind="message.created",
                channel=self.name,
                delivery_id=f"{ref.encoded}-{n}",
                cursor=str(n),
            )


def test_listen_resumes_from_the_cursor_kept_in_the_ledger(ledger):
    registry = {"fake": _Listener()}
    events = listen(FAKE_REF, cursors=ledger.cursors, registry=registry)
    assert [e.cursor for e in events] == ["1", "2", "3"]
    assert dict(ledger.cursors) == {FAKE_REF: "3"}
    assert list(listen(FAKE_REF, cursors=ledger.cursors, registry=registry)) == []


def test_listen_over_a_dry_run_ledger_leaves_the_real_cursor_alone():
    registry = {"fake": _Listener()}
    base: dict = {}
    Ledger(base).cursors[FAKE_REF] = "1"
    dry = Ledger(ChainMap({}, base))
    events = listen(FAKE_REF, cursors=dry.cursors, registry=registry)
    assert [e.cursor for e in events] == ["2", "3"]
    assert dry.cursors[FAKE_REF] == "3"
    assert Ledger(base).cursors[FAKE_REF] == "1"


# ---- key layout ----


def test_no_store_key_contains_a_slash_or_a_colon(ledger):
    ledger.mark_seen(REF + "/comment-1", channel="github", kind="message", at=T0)
    ledger.new_case("pat", REF, reporter="pat", at=T0)
    ledger.add_conversation("pat-1", WEB_REF)
    ledger.set_hold(Hold(scope=REPO_SCOPE, mode="block"))
    ledger.set_hold(Hold(scope=CHECKOUT_SCOPE, mode="block"))
    ledger.save_run(_run())
    ledger.add_unrouted(delivery_id=REF + "/comment-2", reason="unresolved sender")
    ledger.cursors[REF] = "c1"
    ledger.increment_daily("pat", T0)
    # inbox, case, two conversations, counter, two holds, run, unrouted, cursor, daily
    assert len(ledger.store) == 11
    assert [key for key in ledger.store if "/" in key or ":" in key] == []


# ---- the dry-run overlay ----


def test_dry_run_overlay_writes_land_in_front_and_the_base_is_untouched():
    base: dict = {}
    real = Ledger(base)
    real.new_case("pat", REF, reporter="pat", at=T0)
    real.set_hold(Hold(scope=REPO_SCOPE, mode="block"))
    real.cursors[REF] = "c1"
    real.cursors[WEB_REF] = "w0"
    real.mark_seen("delivery-1", channel="github", kind="message", at=T0)
    snapshot = copy.deepcopy(base)

    overlay: dict = {}
    dry = Ledger(ChainMap(overlay, base))
    # reads see the real ledger
    assert dry.seen("delivery-1")
    assert dry.get_case("pat-1").state == "intake"
    assert dry.cursors[REF] == "c1"
    # writes of every kind, deletes of what only the base holds included
    dry.mark_seen("delivery-2", channel="github", kind="message", at=T0)
    dry.append("pat-1", _message(minutes=5))
    dry.transition("pat-1", "working", at=T0 + timedelta(minutes=10))
    dry.add_conversation("pat-1", OTHER_REF + "-web")
    new = dry.new_case("pat", OTHER_REF, reporter="pat", at=T0)
    dry.clear_hold(REPO_SCOPE)
    dry.set_hold(Hold(scope="global", mode="drain"))
    dry.cursors[REF] = "c2"
    del dry.cursors[WEB_REF]
    dry.save_run(_run())
    dry.add_unrouted(delivery_id="delivery-3", reason="unresolved sender")
    dry.increment_daily("pat", T0)

    # the dry ledger sees its own writes...
    assert dry.get_case("pat-1").state == "working"
    assert len(dry.get_case("pat-1").entries) == 2
    assert dry.case_for_conversation(OTHER_REF + "-web").id == "pat-1"
    assert new.id == "pat-2"
    assert [h.scope for h in dry.holds()] == ["global"]
    assert dry.get_hold(REPO_SCOPE) is None
    assert dict(dry.cursors) == {REF: "c2"}
    assert dry.seen("delivery-2")
    assert overlay
    # ...while the base is exactly as it was
    assert base == snapshot
    after = Ledger(base)
    assert after.get_case("pat-1").state == "intake"
    assert after.get_case("pat-1").entries == ()
    assert [h.scope for h in after.holds()] == [REPO_SCOPE]
    assert dict(after.cursors) == {REF: "c1", WEB_REF: "w0"}
    assert after.get_run("r1") is None
    assert list(after.unrouted()) == []
    assert not after.seen("delivery-2")


# ---- the default store ----


def test_default_ledger_store_is_flat_json_files_under_state_dir(tmp_path):
    state_dir = tmp_path / "state"
    ledger = Ledger(default_ledger_store(state_dir))
    ledger.new_case("pat", REF, reporter="pat", at=T0)
    case = ledger.append("pat-1", _message(minutes=1))
    ledger.set_hold(Hold(scope=REPO_SCOPE, mode="block"))
    ledger.cursors[REF] = "c1"

    root = state_dir / "ledger"
    assert root.is_dir()
    assert all(p.is_file() and p.suffix == ".json" for p in root.iterdir())

    reread = Ledger(default_ledger_store(state_dir))
    assert reread.get_case("pat-1") == case
    assert reread.case_for_conversation(REF) == case
    assert reread.get_hold(REPO_SCOPE) == Hold(scope=REPO_SCOPE, mode="block")
    assert dict(reread.cursors) == {REF: "c1"}


def test_dry_run_over_the_default_store_leaves_its_files_alone(tmp_path):
    real = Ledger(default_ledger_store(tmp_path))
    real.new_case("pat", REF, reporter="pat", at=T0)
    real.set_hold(Hold(scope=REPO_SCOPE, mode="block"))
    root = tmp_path / "ledger"
    before = {p.name: p.read_text() for p in root.iterdir()}

    dry = Ledger(ChainMap({}, default_ledger_store(tmp_path)))
    dry.transition("pat-1", "working", at=T0)
    dry.clear_hold(REPO_SCOPE)
    dry.new_case("pat", OTHER_REF, reporter="pat", at=T0)
    assert dry.get_case("pat-1").state == "working"
    assert list(dry.holds()) == []

    assert {p.name: p.read_text() for p in root.iterdir()} == before
