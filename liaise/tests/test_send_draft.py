"""Tests for releasing a case's draft (#29): ``liaise case send-draft`` and ``reject-draft``.

Every channel is a FakeGitHubChannel and every config root is under tmp_path, so nothing is
posted and no real configuration or people record is read. The operator's release is a
fact on the gate's context, not a way around the gate: the tests that release a draft
holding a leak, or one for a person with no handle to mention, are the check that the
release settles draft reply mode and nothing else.
"""

from __future__ import annotations

import copy
import io
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import cw
import pytest
from correspond.errors import ChannelError

from liaise import cases, cli
from liaise.gate import Pass
from liaise.ledger import Ledger
from liaise.model import Approval, Hold, RunRecord
from liaise.outcomes import make_draft
from liaise.subjects import load_subjects
from liaise.testing import FakeGitHubChannel, demo_registry
from liaise.tests.conftest import write_executable_script
from liaise.tick import run_lock, run_lock_path

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)
SLUG = "example-app"
REPO = "example/app"
ISSUE = f"github:{REPO}#1"
CASE = f"{SLUG}-1"
TEXT = "Two of the three date fields stop in October. Is that expected?"
#: Built by concatenation, so the no-personal-data guard does not read it as a real path.
LEAK = "The export is at " + "/Us" + "ers/someone/export.csv"
#: Why the policy holds a message to pat back in draft reply mode.
DRAFT_REASON = "draft reply mode for pat: the operator releases every message"

SUBJECT_TOML = """
bindings = ["github:example/app?labels=partner:pat"]

[policy]
default_reply_mode = "draft"
people = {{ {people} }}
roles = {{ pat = "partner" }}
"""


@pytest.fixture(autouse=True)
def no_real_acquaint(monkeypatch):
    monkeypatch.setitem(sys.modules, "acquaint", None)


class World:
    """A subject in draft reply mode, its issue on a fake GitHub, and its case in needs-owner."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.root = tmp_path / "config"
        (self.root / "subjects").mkdir(parents=True)
        state_dir = (tmp_path / "state").as_posix()
        (self.root / "config.toml").write_text(f'owner_login = "owner"\nstate_dir = "{state_dir}"\n')
        self.write_subject('"github:pat" = "pat"')
        self.github = FakeGitHubChannel(clock=lambda: LATER)
        self.github.add_issue(
            REPO, 1, author="pat", title="Dates", body="The dates look short.", labels=("partner:pat",), created_at=NOW
        )
        self.registry = demo_registry(github=self.github)
        self.previews: list[str] = []
        self.store: dict = {}
        ledger = Ledger(self.store)
        ledger.new_case(SLUG, ISSUE, reporter="pat", at=NOW)
        ledger.transition(CASE, "needs-owner", at=NOW, actor="liaise", reason="seeded")

    def write_subject(self, people: str) -> None:
        (self.root / "subjects" / f"{SLUG}.toml").write_text(SUBJECT_TOML.format(people=people))

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.store)

    def case(self):
        return self.ledger.get_case(CASE)

    def hold_drafts(self, *drafts, state=None) -> None:
        case = replace(self.case(), drafts=tuple(drafts))
        if state is not None:
            case = replace(case, state=state)
        self.ledger.save_case(case)

    def send(self, *args, answer=True, **kwargs) -> str:
        """``liaise case send-draft`` for the case, the operator answering ``answer`` to what it shows."""

        def confirm(preview):
            self.previews.append(preview)
            return answer(self) if callable(answer) else answer

        kwargs = dict(root=str(self.root), registry=self.registry, store=self.store, now=LATER, confirm=confirm, **kwargs)
        return cli.case_send_draft(CASE, *args, **kwargs)

    def reject(self, *args, **kwargs) -> str:
        return cli.case_reject_draft(CASE, *args, root=str(self.root), store=self.store, now=LATER, **kwargs)

    def posted(self) -> list[str]:
        return [draft.text for _, draft in self.github.sent]


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def _draft(text=TEXT, *, outcome="ask", reason="draft reply mode", ref=ISSUE) -> dict:
    return make_draft(at=NOW, outcome=outcome, recipient="pat", ref=ref, text=text, reason=reason)


# ---- sending ----


def test_a_draft_held_by_draft_reply_mode_is_sent_with_the_mention_and_recorded_as_the_operators(world):
    world.hold_drafts(_draft())

    output = world.send()

    assert world.posted() == [f"@pat {TEXT}"]
    lines = output.splitlines()
    assert lines[0].startswith(f"sent draft [0] of {CASE} on {ISSUE} (gate: passed, 5 filters): https://")
    assert f"  note: released by operator at {LATER.isoformat()}, past: reply mode" in lines
    assert "  note: added the mention @pat" in lines
    assert lines[-1] == f"moved {CASE} from needs-owner to needs-partner; its labels follow on the next tick"
    case = world.case()
    assert (case.drafts, case.state) == ((), "needs-partner")
    sent, moved = case.entries[-2:]
    assert (sent.kind, sent.actor, sent.at, sent.text) == ("gate", "operator", LATER, f"@pat {TEXT}")
    assert sent.detail["decision"] == "send" and sent.detail["url"].startswith("https://")
    approval = sent.detail["approval"]
    assert (approval["by"], approval["at"], approval["rules_overridden"]) == ("operator", LATER.isoformat(), ["reply mode"])
    assert sent.detail["approval_bound"] is True
    assert (sent.detail["draft"], sent.detail["held_for"], sent.detail["edited"]) == (0, "draft reply mode", False)
    assert (moved.kind, moved.actor, moved.detail["to"]) == ("transition", "operator", "needs-partner")


def test_a_released_draft_holding_a_leak_is_diverted_and_stays_on_the_case(world):
    world.hold_drafts(_draft(LEAK))

    with pytest.raises(cw.CommandError) as raised:
        world.send()

    refusal = str(raised.value)
    assert refusal.startswith(
        f"draft [0] of {CASE} was not sent: diverted by outbound_policy (refuse): an exfiltration shape (local-path)"
    )
    assert ". It stays on the case with that reason" in refusal
    assert raised.value.code == cli.DIVERTED_EXIT_CODE
    assert world.previews == [] and world.posted() == []  # nothing to confirm: it cannot go
    case = world.case()
    (draft,) = case.drafts
    assert (draft["text"], draft["gate"]["flow"], case.state) == (LEAK, "refuse", "needs-owner")
    assert draft["reason"].startswith("an exfiltration shape (local-path)")
    attempt = case.entries[-1]
    assert (attempt.kind, attempt.actor, attempt.detail["decision"]) == ("gate", "operator", "divert")


def test_a_release_for_a_person_with_no_handle_to_mention_is_diverted(world):
    world.write_subject('"webinbox:pat" = "pat"')
    world.hold_drafts(_draft())

    with pytest.raises(cw.CommandError, match="diverted by notify_recipient \\(approve\\): no handle to notify pat"):
        world.send()
    assert world.posted() == []


def test_the_gate_judges_the_edited_text_and_sends_it(world):
    world.hold_drafts(_draft())
    opened = []

    def editor(text):
        opened.append(text)
        return text.replace("expected?", "expected, or a cut-off?")

    output = world.send(edit=True, editor=editor)

    assert opened == [TEXT]
    assert world.posted() == ["@pat Two of the three date fields stop in October. Is that expected, or a cut-off?"]
    assert output.startswith(f"sent draft [0] of {CASE}")
    assert world.case().entries[-2].detail["edited"] is True


def test_an_edit_that_adds_a_leak_is_diverted_and_kept_for_the_next_edit(world):
    world.hold_drafts(_draft())

    with pytest.raises(cw.CommandError, match="diverted by outbound_policy \\(refuse\\)"):
        world.send(edit=True, editor=lambda text: f"{text}\n{LEAK}")

    assert world.posted() == []
    (kept,) = world.case().drafts
    assert kept["text"] == f"{TEXT}\n{LEAK}"
    opened = []
    world.send(edit=True, editor=lambda text: opened.append(text) or TEXT)
    assert opened == [f"{TEXT}\n{LEAK}"]
    assert world.posted() == [f"@pat {TEXT}"]


def test_a_draft_that_changed_while_it_was_open_in_the_editor_is_not_sent(world):
    world.hold_drafts(_draft())

    def editor(text):
        world.hold_drafts(_draft("Something else entirely."))  # another command, meanwhile
        return text

    with pytest.raises(cw.CommandError, match=f"draft \\[0\\] of {CASE} changed while you had it open"):
        world.send(edit=True, editor=editor)
    assert world.posted() == []


def test_a_dry_run_judges_and_plans_and_changes_nothing(world):
    world.hold_drafts(_draft())
    before = copy.deepcopy(world.store)

    output = world.send(dry_run=True)

    assert output.splitlines()[0] == f"would send draft [0] of {CASE} on {ISSUE} (gate: passed, 5 filters)"
    assert output.splitlines()[-1].startswith(f"would move {CASE} from needs-owner to needs-partner")
    world.hold_drafts(_draft(LEAK))
    leaking = copy.deepcopy(world.store)
    with pytest.raises(cw.CommandError, match=f"draft \\[0\\] of {CASE} would not be sent: diverted by outbound_policy"):
        world.send(dry_run=True)
    assert world.store == leaking and before != leaking
    assert world.posted() == []
    assert not (world.tmp_path / "state").exists()  # no lock taken, no ledger created


@pytest.mark.parametrize(
    "state, outcome, after",
    [
        ("needs-owner", "ask", "needs-partner"),
        ("needs-owner", "reply", "needs-partner"),
        ("needs-owner", "escalate", "needs-owner"),  # it may be a refusal: the operator moves it
        ("needs-owner", "deliver", "needs-owner"),  # a held deploy did not go out with its message
        ("needs-owner", "nudge", "needs-owner"),
        ("needs-partner", "ask", "needs-partner"),  # where the tick leaves a diverted ask
        ("deployed", "nudge", "deployed"),
    ],
)
def test_a_sent_draft_moves_only_a_case_in_needs_owner_and_only_for_a_message_to_the_reporter(
    world, state, outcome, after
):
    world.hold_drafts(_draft(outcome=outcome), state=state)

    output = world.send()

    assert world.case().state == after
    stays = f"{CASE} stays {after}"
    assert (output.splitlines()[-1] == stays) is (state == after)


def test_a_channel_that_refuses_the_message_keeps_the_draft_with_the_failure(world):
    world.github.send_error = ChannelError("issue is locked", kind="permission")
    world.hold_drafts(_draft())

    with pytest.raises(cw.CommandError, match=f"draft \\[0\\] of {CASE} was not sent: send failed: ") as raised:
        world.send()

    assert raised.value.code == 1
    (kept,) = world.case().drafts
    assert kept["reason"].startswith("send failed: ")
    assert kept["text"] == TEXT  # without the mention: the next release adds the handle of that day
    assert world.case().entries[-1].text == f"@pat {TEXT}"  # what was attempted
    assert world.case().state == "needs-owner"
    assert world.case().entries[-1].detail["decision"] == "send" and world.case().entries[-1].detail["error"]


def test_only_the_named_draft_is_sent_and_the_others_stay(world):
    world.hold_drafts(_draft("First."), _draft("Second."), _draft("Third."))

    output = world.send(1)

    assert world.posted() == ["@pat Second."]
    assert [draft["text"] for draft in world.case().drafts] == ["First.", "Third."]
    assert output.splitlines()[-2:] == [f"{CASE} stays needs-owner", f"drafts left on {CASE}: 2"]
    assert world.case().state == "needs-owner"  # two drafts still wait on the operator


def _seed_run(world):
    run = RunRecord(run_id=f"{CASE}-r1", case_id=CASE, subject=SLUG, mode="fresh", status="running", started_at=NOW)
    world.ledger.save_run(run)


@pytest.mark.parametrize(
    "drafts, setup, args, message",
    [
        ((), None, (), f"case {CASE} has no draft waiting for the operator"),
        ((_draft(),), None, (3,), f"case {CASE} has no draft \\[3\\]; its drafts are \\[0\\] "),
        ((_draft(), _draft()), None, (), f"case {CASE} has 2 drafts, \\[0\\] to \\[1\\]: name the one to use"),
        ((_draft(ref=None, reason="no channel to reach pat"),), None, (), "has no destination \\(no channel to reach pat\\)"),
        ((_draft("  ", outcome="escalate"),), None, (), "has no text to send; write it with --edit"),
        ((_draft(),), _seed_run, (), f"case {CASE} has run {CASE}-r1 in flight"),
        (
            (_draft(),),
            lambda world: world.ledger.set_hold(Hold(scope=f"subject:{SLUG}", mode="block")),
            (),
            f"the hold on subject:{SLUG} \\(block\\) keeps these messages waiting",
        ),
        (
            (_draft(),),
            lambda world: world.ledger.set_hold(Hold(scope="person:pat", mode="cancel")),
            (),
            "the hold on person:pat \\(cancel\\)",
        ),
    ],
)
def test_send_draft_refuses_what_it_cannot_do_and_changes_nothing(world, drafts, setup, args, message):
    world.hold_drafts(*drafts)
    if setup is not None:
        setup(world)
    before = copy.deepcopy(world.store)

    with pytest.raises(cw.CommandError, match=message):
        world.send(*args)

    assert world.store == before
    assert world.posted() == []


def test_the_draft_commands_take_one_index_at_most(world):
    world.hold_drafts(_draft("First."), _draft("Second."))
    before = copy.deepcopy(world.store)
    with pytest.raises(cw.CommandError, match=f"name one draft of {CASE} at a time, not 0, 1"):
        world.send(0, 1)
    with pytest.raises(cw.CommandError, match=f"name one draft of {CASE} at a time"):
        world.reject(0, 1, reason="both wrong")
    assert world.store == before and world.posted() == []


def test_send_draft_of_a_case_the_ledger_does_not_hold_is_one_line(world):
    with pytest.raises(cw.CommandError, match=f"no case '{SLUG}-9'"):
        cli.case_send_draft(f"{SLUG}-9", root=str(world.root), registry=world.registry, store=world.store)


def test_a_drain_hold_lets_a_released_draft_go(world):
    world.ledger.set_hold(Hold(scope=f"subject:{SLUG}", mode="drain"))
    world.hold_drafts(_draft())

    world.send()

    assert world.posted() == [f"@pat {TEXT}"]


def test_send_draft_refuses_while_a_tick_holds_the_run_lock(world):
    world.hold_drafts(_draft())
    before = copy.deepcopy(world.store)
    with run_lock(run_lock_path(world.tmp_path / "state")):
        with pytest.raises(cw.CommandError, match=f"a liaise tick is running, so {CASE} was not sent; try again"):
            world.send()
        assert world.store == before and world.posted() == []
    world.send()
    assert world.posted() == [f"@pat {TEXT}"]


def test_the_gate_sees_the_operators_approval_and_the_case(world):
    world.hold_drafts(_draft())
    seen = []

    def spy(outbound, ctx):
        seen.append((outbound.purpose, outbound.text, ctx.case.id, ctx.approval))
        return Pass(outbound)

    release = cases.send_draft(
        world.ledger, load_subjects(world.root), CASE, by="operator", now=LATER, registry=world.registry,
        outbound_filters=(spy,), approve_shown=True,
    )

    assert [entry[:3] for entry in seen] == [("ask", TEXT, CASE)] * 2  # judged, then sent with the approval
    shown, sent_with = seen[0][3], seen[1][3]
    assert shown is None and isinstance(sent_with, Approval)
    assert (sent_with.by, sent_with.at, sent_with.rules_overridden) == ("operator", LATER, ())
    assert sent_with.payload_hash and sent_with.audience_hash and release.approval == sent_with
    assert release.attempt.sent and release.filters == 1
    assert world.posted() == [TEXT]  # no mention: this gate has only the spy


def test_a_release_without_the_operators_approval_settles_nothing(world):
    """A caller that says who releases a draft has not shown them anything: only a caller that
    judged the message and asked (``approve_shown``, as the command does) releases it."""
    world.hold_drafts(_draft())
    subjects = load_subjects(world.root)
    kwargs = dict(by="operator", now=LATER, registry=world.registry)

    unapproved = cases.send_draft(world.ledger, subjects, CASE, **kwargs)

    assert not unapproved.attempt.sent and world.posted() == []
    assert (unapproved.approval, unapproved.attempt.decision.diverted) == (None, DRAFT_REASON)
    assert unapproved.attempt.decision.settled == ()

    released = cases.send_draft(
        world.ledger, subjects, CASE, approve_shown=True, justification="Pat asked on a call", **kwargs
    )

    assert released.attempt.sent and world.posted() == [f"@pat {TEXT}"]
    assert released.approval.rules_overridden == ("reply mode",)
    assert released.approval.justification == "Pat asked on a call"


def test_judging_without_sending_records_a_divert_and_leaves_a_passing_draft_alone(world):
    subjects = load_subjects(world.root)
    world.hold_drafts(_draft())
    before = copy.deepcopy(world.store)

    judged = cases.send_draft(
        world.ledger, subjects, CASE, by="operator", now=LATER, registry=world.registry, send=False, approve_shown=True
    )

    assert judged.attempt.sent and world.store == before and world.posted() == []
    world.hold_drafts(_draft(LEAK))
    judged = cases.send_draft(
        world.ledger, subjects, CASE, by="operator", now=LATER, registry=world.registry, send=False, approve_shown=True
    )
    with pytest.raises(TypeError):
        cases.send_draft(world.ledger, subjects, CASE, now=LATER)  # who releases it must be said
    assert judged.attempt.decision.diverted.startswith("an exfiltration shape (local-path)")
    assert world.case().drafts[0]["reason"].startswith("an exfiltration shape (local-path)") and world.posted() == []


# ---- the operator confirms (review of #29) ----


def test_without_a_terminal_nothing_is_sent(world, monkeypatch):
    world.hold_drafts(_draft())
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))  # an answer piped in is not a person
    before = copy.deepcopy(world.store)

    with pytest.raises(cw.CommandError, match="there is no terminal here, so nothing was sent"):
        cli.case_send_draft(CASE, root=str(world.root), registry=world.registry, store=world.store, now=LATER)

    assert world.store == before and world.posted() == []


def test_the_operator_reads_where_it_goes_the_verdict_and_the_exact_text_and_may_decline(world):
    world.hold_drafts(_draft())
    before = copy.deepcopy(world.store)

    output = world.send(answer=False)

    assert output == f"nothing sent: draft [0] of {CASE} stays on the case"
    assert world.store == before and world.posted() == []
    (preview,) = world.previews
    lines = preview.splitlines()
    assert lines[0] == f"draft [0] of {CASE}: ask to pat on {ISSUE}"
    assert lines[1].startswith("audience: named readers")
    assert lines[2:4] == ["gate: sends once you release it past 1 concern(s):", f"  [approve] reply mode: {DRAFT_REASON}"]
    assert f"then {CASE} moves from needs-owner to needs-partner" in lines
    assert lines[-3:] == ["--- the message, as it would be sent (invisible characters as <U+XXXX>) ---", f"@pat {TEXT}", "---"]


# ---- approvals bound to what the operator saw (#36) ----


def test_a_draft_whose_repository_went_public_after_the_approval_is_not_sent_and_the_new_verdict_shown(world):
    """Acceptance (#36): the operator approves a message to a private repository, which goes
    public before it is sent; the approval's audience hash no longer matches, so nothing goes."""
    world.hold_drafts(_draft())

    def made_public_meanwhile(world):
        world.github.set_visibility(REPO, "public")
        return True

    with pytest.raises(cw.CommandError) as raised:
        world.send(answer=made_public_meanwhile)

    refusal = str(raised.value)
    assert raised.value.code == cli.DIVERTED_EXIT_CODE and world.posted() == []
    assert refusal.startswith(
        f"draft [0] of {CASE} was not sent: diverted by gate (approve): the approval by operator at "
        f"{LATER.isoformat()} is void: its audience changed since it was given"
    )
    assert f"a send to {ISSUE} cannot be withdrawn (world-readable" in refusal  # the verdict the audience now gets
    assert "audience: named readers" in world.previews[0]  # what the operator had approved
    (kept,) = world.case().drafts
    assert (kept["gate"]["flow"], kept["gate"]["audience"].split(";")[0]) == ("approve", "world-readable")
    attempt = world.case().entries[-1]
    assert (attempt.actor, attempt.detail["decision"], attempt.detail["approval_bound"]) == ("operator", "divert", False)
    assert attempt.detail["verdict"]["audience"]["scope"] == "public"


def test_a_released_draft_records_the_approval_its_justification_and_the_rules_it_overrode(world):
    """Acceptance (#36)."""
    subject = SUBJECT_TOML.format(people='"github:pat" = "pat"') + 'leak_terms = ["example-internal"]\n'
    (world.root / "subjects" / f"{SLUG}.toml").write_text(subject)
    world.hold_drafts(_draft("The dates are on the example-internal board now."))

    world.send(justification="  the board went public last week  ")

    (preview,) = world.previews
    assert "gate: sends once you release it past 2 concern(s):" in preview.splitlines()
    sent = world.case().entries[-2]
    approval = sent.detail["approval"]
    assert (approval["by"], approval["justification"]) == ("operator", "the board went public last week")
    assert sorted(approval["rules_overridden"]) == ["no write-down", "reply mode"]
    assert (approval["payload_hash"], approval["audience_hash"]) == (sent.detail["payload_hash"], sent.detail["audience_hash"])
    assert approval["verdict_id"] and sent.detail["approval_bound"] is True
    assert sorted(concern["rule"] for concern in sent.detail["settled"]) == ["no write-down", "reply mode"]
    assert world.posted() == ["@pat The dates are on the example-internal board now."]


def test_a_dry_run_of_a_draft_the_gate_flags_still_shows_it_as_sendable(world):
    """The release judges the draft, shows the operator, and judges it again with their
    approval. A dry run writes no fingerprint key, so both judgements must share the one it
    used once: two keys would flag the same draft differently and void the approval."""
    subject = SUBJECT_TOML.format(people='"github:pat" = "pat"') + 'leak_terms = ["example-internal"]\n'
    (world.root / "subjects" / f"{SLUG}.toml").write_text(subject)
    world.hold_drafts(_draft("The dates are on the example-internal board now."))
    assert not (world.tmp_path / "state").exists()  # a fresh install, where --dry-run is reached for

    output = world.send(dry_run=True)

    assert output.splitlines()[0] == f"would send draft [0] of {CASE} on {ISSUE} (gate: passed, 5 filters)"
    assert "is void" not in output  # not the operator's own approval, judged a second time
    assert world.previews == []  # a dry run asks nobody
    assert world.posted() == [] and not (world.tmp_path / "state").exists()


def test_case_show_renders_invisible_characters_every_url_in_full_and_the_audience_in_words(world):
    """Acceptance (#36): what the operator reads before releasing a draft."""
    hidden = "Dates are fixed" + "​" + "." + "\x1b[2J" + " See [the changelog](https://example.org/notes)"
    world.hold_drafts(_draft(hidden))
    with pytest.raises(cw.CommandError, match="diverted by outbound_policy \\(refuse\\)"):
        world.send()

    shown = cli.case_show(CASE, root=str(world.root), store=world.store).splitlines()

    assert "    Dates are fixed<U+200B>.<U+001B>[2J See [the changelog](https://example.org/notes)" in shown
    assert "    [links, in full:]" in shown and "      https://example.org/notes" in shown
    assert any(line.startswith("    [gate: refuse; audience: named readers") for line in shown)
    assert not any("​" in line or "\x1b" in line for line in shown)


def test_an_edit_that_changed_nothing_says_so_before_anything_goes(world):
    world.hold_drafts(_draft())

    world.send(edit=True, editor=lambda text: text, answer=False)  # a GUI editor that did not wait

    assert "your edit changed nothing: this is the draft as it was" in world.previews[0].splitlines()


def test_a_draft_that_changed_while_the_operator_read_it_is_not_sent(world):
    world.hold_drafts(_draft("Is the October cut-off expected?"))

    def meanwhile(world):
        world.hold_drafts(_draft("Something else entirely."))  # a reject in another terminal, say
        return True

    with pytest.raises(cw.CommandError, match=f"draft \\[0\\] of {CASE} changed while you had it open"):
        world.send(answer=meanwhile)
    assert world.posted() == []


def test_a_delivery_message_a_hold_kept_is_refused_since_the_delivery_never_ran(world):
    world.hold_drafts(_draft("Deployed: try it now.", outcome="deliver", reason="held: effect:deploy"))
    before = copy.deepcopy(world.store)

    with pytest.raises(cw.CommandError, match="a hold kept that delivery from running \\(held: effect:deploy\\)"):
        world.send()

    assert world.store == before and world.posted() == []


def test_an_effect_deploy_hold_keeps_a_delivery_message_and_lets_a_question_go(world):
    world.ledger.set_hold(Hold(scope="effect:deploy", mode="block"))
    world.hold_drafts(_draft("Deployed: try it now.", outcome="deliver", reason="deslop: 1 enforced finding(s)"))

    with pytest.raises(cw.CommandError, match="the hold on effect:deploy \\(block\\) keeps this delivery waiting"):
        world.send()

    world.hold_drafts(_draft())
    world.send()
    assert world.posted() == [f"@pat {TEXT}"]


class _StoreThatFailsWhenTold(dict):
    fail = False

    def __setitem__(self, key, value):
        if self.fail:
            raise OSError("disk full")
        super().__setitem__(key, value)


def test_a_send_the_ledger_cannot_record_says_it_went_out_and_not_to_send_it_again(world):
    world.store = _StoreThatFailsWhenTold(world.store)
    world.hold_drafts(_draft())

    def break_the_store(world):
        world.store.fail = True
        return True

    with pytest.raises(cw.CommandError, match=f"draft \\[0\\] of {CASE} was sent as https://.* do not send it again"):
        world.send(answer=break_the_store)
    assert world.posted() == [f"@pat {TEXT}"]


def test_the_editor_edits_a_file_in_a_directory_removed_with_its_backups(tmp_path, monkeypatch):
    body = (
        "import pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "path.write_text(path.read_text() + ' Edited.')\n"
        "pathlib.Path(str(path) + '~').write_text('a backup')\n"
    )
    script = write_executable_script(tmp_path / "editor", body)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("VISUAL", str(script))

    assert cli.edit_in_editor("Hello.") == "Hello. Edited."
    assert list(scratch.iterdir()) == []


def test_an_editor_that_fails_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("VISUAL", str(write_executable_script(tmp_path / "editor", "import sys\nsys.exit(3)\n")))

    with pytest.raises(ValueError, match="exited with 3, so nothing was sent"):
        cli.edit_in_editor("Hello.")


# ---- rejecting ----


def test_reject_draft_records_the_refusal_with_its_reason_and_leaves_the_state(world):
    world.hold_drafts(_draft("First."), _draft("Second."))

    output = world.reject(0, reason="  the fields are fine, I checked  ")

    assert output.splitlines() == [
        f"rejected draft [0] of {CASE} (ask to {ISSUE}): the fields are fine, I checked",
        f"{CASE} stays needs-owner; move it on with liaise case set-state {CASE} STATE",
    ]
    case = world.case()
    assert [draft["text"] for draft in case.drafts] == ["Second."]
    assert case.state == "needs-owner"
    entry = case.entries[-1]
    assert (entry.kind, entry.actor, entry.at, entry.text) == ("gate", "operator", LATER, "First.")
    assert entry.detail == {
        "decision": "reject",
        "reason": "the fields are fine, I checked",
        "purpose": "ask",
        "ref": ISSUE,
        "draft": 0,
        "held_for": "draft reply mode",
    }
    assert world.posted() == []


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (dict(reason=" "), "a rejected draft needs a reason"),
        (dict(reason="no", dry_run=True), None),
    ],
)
def test_reject_draft_writes_nothing_without_a_reason_or_in_a_dry_run(world, kwargs, message):
    world.hold_drafts(_draft())
    before = copy.deepcopy(world.store)
    if message is None:
        assert world.reject(**kwargs).startswith(f"would reject draft [0] of {CASE}")
    else:
        with pytest.raises(cw.CommandError, match=message):
            world.reject(**kwargs)
    assert world.store == before
