"""Tests for messages outside a case (#28): ``liaise message send``, ``list``, ``show``, ``send-draft``, ``reject-draft``.

A message outside a case goes through the same gate as a case's messages, with no case on
the context, and its subject comes only from the bindings. Every channel is a
FakeGitHubChannel and every config root is under tmp_path: nothing is posted, and no real
configuration or people record is read. Leak samples are built by concatenation, so the
no-personal-data guard does not read them as real paths.
"""

from __future__ import annotations

import copy
import io
import re
import sys
from datetime import datetime, timedelta, timezone

import cw
import pytest
from correspond.errors import ChannelError

from liaise import cli, messages
from liaise.gate import GateContext, Outbound, run_gate
from liaise.ledger import MESSAGE_ID_HEX_DIGITS, Ledger
from liaise.model import Hold
from liaise.subjects import Policy, Subject, subject_for_ref
from liaise.testing import FakeGitHubChannel, demo_registry

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)
SLUG = "example-app"
REPO = "example/app"
ISSUE = f"github:{REPO}#12"
TEXT = "Two of the three date fields stop in October. Is that expected?"
LEAK = "The export is at " + "/Us" + "ers/someone/export.csv"
MESSAGE_ID = re.compile(rf"{SLUG}-m[0-9a-f]{{{MESSAGE_ID_HEX_DIGITS}}}")

SUBJECT_TOML = """
bindings = ["github:example/app?labels=partner:pat"]

[policy]
default_reply_mode = "{mode}"
people = {{ "github:pat" = "pat" }}
roles = {{ pat = "partner" }}
leak_terms = ["example-internal"]
"""


@pytest.fixture(autouse=True)
def no_real_acquaint(monkeypatch):
    monkeypatch.setitem(sys.modules, "acquaint", None)


class World:
    """A subject binding example/app, an issue on a fake GitHub, and a recorder for notifications."""

    def __init__(self, tmp_path, *, mode="draft"):
        self.tmp_path = tmp_path
        self.root = tmp_path / "config"
        (self.root / "subjects").mkdir(parents=True)
        state_dir = (tmp_path / "state").as_posix()
        (self.root / "config.toml").write_text(f'owner_login = "owner"\nstate_dir = "{state_dir}"\n')
        self.set_mode(mode)
        self.github = FakeGitHubChannel(clock=lambda: LATER)
        self.github.add_issue(REPO, 12, author="pat", title="Dates", body="The dates look short.", created_at=NOW)
        self.registry = demo_registry(github=self.github)
        self.store: dict = {}
        self.notices: list[tuple[str, str, str]] = []
        self.previews: list[str] = []

    def set_mode(self, mode: str) -> None:
        (self.root / "subjects" / f"{SLUG}.toml").write_text(SUBJECT_TOML.format(mode=mode))

    def notify(self, title, body, *, priority):
        self.notices.append((title, body, priority))

    def send(self, recipient="pat", **kwargs) -> str:
        kwargs = {"ref": ISSUE, "text": TEXT, **kwargs}
        return cli.message_send(
            recipient, root=str(self.root), registry=self.registry, store=self.store, now=NOW, notify_fn=self.notify, **kwargs
        )

    def held(self, recipient="pat", **kwargs) -> str:
        """Send a message the gate or a hold keeps, and return its id."""
        with pytest.raises(cw.CommandError):
            self.send(recipient, **kwargs)
        (message,) = [m for m in self.messages() if m.state == "held"]
        return message.id

    def release(self, message_id, *, answer=True, **kwargs) -> str:
        def confirm(preview):
            self.previews.append(preview)
            return answer

        kwargs = dict(root=str(self.root), registry=self.registry, store=self.store, now=LATER, confirm=confirm, **kwargs)
        return cli.message_send_draft(message_id, **kwargs)

    def reject(self, message_id, **kwargs) -> str:
        return cli.message_reject_draft(message_id, root=str(self.root), store=self.store, now=LATER, **kwargs)

    def messages(self):
        return sorted(Ledger(self.store).messages(), key=lambda m: (m.created_at, m.id))

    def posted(self):
        return [(ref.encoded, draft.title, draft.text) for ref, draft in self.github.sent]


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


# ---- sending ----


def test_in_draft_mode_a_message_is_held_recorded_and_the_operator_told_without_its_text(world):
    with pytest.raises(cw.CommandError) as raised:
        world.send()

    assert raised.value.code == cli.DIVERTED_EXIT_CODE
    (message,) = world.messages()
    assert MESSAGE_ID.fullmatch(message.id)
    assert str(raised.value).startswith(
        f"the message to pat on {ISSUE} is held as {message.id}: diverted by reply_mode: draft reply mode. "
    )
    assert (message.state, message.reason, message.text, message.purpose) == ("held", "draft reply mode", TEXT, "ask")
    (entry,) = message.entries
    assert (entry.kind, entry.actor, entry.detail["decision"]) == ("gate", "agent", "divert")
    (title, body, _),  = world.notices
    assert title == f"liaise: a message on {SLUG} waits for you"
    assert "cause: reply_mode" in body and "see liaise status" in body
    assert TEXT not in title + body and "pat" not in title + body
    assert world.posted() == []
    assert list(Ledger(world.store).cases()) == []  # a message is not a case


def test_in_direct_mode_a_message_goes_out_through_the_gate_with_the_mention_and_is_recorded(world):
    world.set_mode("direct")

    output = world.send()

    (message,) = world.messages()
    assert output.startswith(f"sent message {message.id} to pat on {ISSUE} (gate: passed, 5 filters): https://")
    assert world.posted() == [(ISSUE, None, f"@pat {TEXT}")]
    assert (message.state, message.text, message.reason) == ("sent", f"@pat {TEXT}", None)
    assert message.entries[0].detail["url"].startswith("https://")
    assert world.notices == []


@pytest.mark.parametrize(
    "recipient, text, filter_name, reason",
    [
        ("pat", LEAK, "leak_scan", "leak scan: local path"),
        ("pat", "It is on the Example-Internal board.", "leak_scan", "leak scan: leak term"),
        ("bram", TEXT, "notify_recipient", "no handle to notify bram"),
    ],
    ids=["a local path", "a leak term", "no handle to mention"],
)
def test_a_message_outside_a_case_meets_the_same_filters(world, recipient, text, filter_name, reason):
    world.set_mode("direct")

    with pytest.raises(cw.CommandError, match=f"diverted by {filter_name}: {re.escape(reason)}") as raised:
        world.send(recipient, text=text)

    assert raised.value.code == cli.DIVERTED_EXIT_CODE
    assert world.posted() == []
    (message,) = world.messages()
    assert (message.state, message.reason) == ("held", reason)


def test_a_title_opens_an_issue_and_the_leak_scan_reads_it(world):
    world.set_mode("direct")

    world.send(ref=f"github:{REPO}", title="Q20: the phase dates stop in October")

    assert world.posted() == [(f"github:{REPO}", "Q20: the phase dates stop in October", f"@pat {TEXT}")]
    with pytest.raises(cw.CommandError, match="diverted by leak_scan: leak scan: leak term") as raised:
        world.send(ref=f"github:{REPO}", title="On the Example-Internal board")
    assert "  note: leak scan: leak term at character 7 of the title" in str(raised.value).splitlines()
    assert len(world.posted()) == 1


def test_a_channel_that_refuses_holds_the_message_and_exits_1(world):
    world.set_mode("direct")
    world.github.send_error = ChannelError("issue is locked", kind="permission")

    with pytest.raises(cw.CommandError, match="send failed: ") as raised:
        world.send()

    assert raised.value.code == 1
    (message,) = world.messages()
    assert (message.state, message.text) == ("held", TEXT)  # without the mention
    assert "cause: permission" in world.notices[0][1]


def test_a_hold_keeps_a_message_before_the_gate_judges_it(world):
    world.set_mode("direct")
    Ledger(world.store).set_hold(Hold(scope=f"subject:{SLUG}", mode="block"))

    with pytest.raises(cw.CommandError, match=f"the hold on subject:{SLUG} \\(block\\)") as raised:
        world.send()

    assert raised.value.code == cli.DIVERTED_EXIT_CODE
    (message,) = world.messages()
    assert (message.reason, message.notes, message.entries[0].detail["decision"]) == (f"held: subject:{SLUG}", (), "hold")
    assert "cause: subject" in world.notices[0][1] and world.posted() == []


def test_a_repository_hold_keeps_a_message_that_opens_an_issue(world):
    world.set_mode("direct")
    Ledger(world.store).set_hold(Hold(scope=f"repo:{REPO}", mode="cancel"))

    with pytest.raises(cw.CommandError, match=f"the hold on repo:{REPO}"):
        world.send(ref=f"github:{REPO}", title="A question")
    assert world.posted() == []


def test_a_dry_run_judges_and_records_and_tells_nothing(world):
    world.set_mode("direct")
    planned = world.send(dry_run=True).splitlines()
    assert planned[0] == f"would send a message to pat on {ISSUE} (gate: passed, 5 filters)"
    assert "  note: added the mention @pat" in planned
    world.set_mode("draft")
    with pytest.raises(cw.CommandError, match=f"the message to pat on {ISSUE} would be held: diverted by reply_mode"):
        world.send(dry_run=True)
    assert world.store == {} and world.notices == [] and world.posted() == []


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (dict(ref=""), "a message needs --ref"),
        (dict(ref="github:example/other#1"), "no subject binds github:example/other#1"),
        (dict(text=""), "exactly one of --text and --text-file"),
        (dict(text_file="note.md"), "exactly one of --text and --text-file"),
        (dict(purpose="decline"), "message purpose 'decline' is not one of: ask, reply, propose"),
    ],
)
def test_message_send_refuses_what_it_cannot_do_and_records_nothing(world, kwargs, message):
    with pytest.raises(cw.CommandError, match=message):
        world.send(**kwargs)
    assert world.store == {} and world.notices == [] and world.posted() == []


def test_the_text_can_come_from_a_file_or_standard_input(world, monkeypatch):
    world.set_mode("direct")
    note = world.tmp_path / "q20.md"
    note.write_text("From a file.")
    world.send(text="", text_file=str(note))
    monkeypatch.setattr(sys, "stdin", io.StringIO("From standard input."))
    world.send(text="", text_file="-")

    assert [text for _, _, text in world.posted()] == ["@pat From a file.", "@pat From standard input."]


# ---- the subject comes from the bindings ----


def _subject(slug, *bindings):
    return Subject(slug, tuple(bindings), Policy(people={}, roles={}))


def test_the_closest_binding_names_the_subject_and_a_tie_is_refused():
    app = _subject("app", "github:example/app?labels=partner:pat")
    issue = _subject("issue", "github:example/app#5")
    glob = _subject("glob", "github:example/*")
    subjects = {"app": app, "issue": issue, "glob": glob}

    assert subject_for_ref(subjects, "github:example/app#5").slug == "issue"
    assert subject_for_ref(subjects, "github:Example/App#6").slug == "app"
    assert subject_for_ref(subjects, "github:example/app").slug == "app"
    with pytest.raises(ValueError, match="no subject binds github:example/other#1"):
        subject_for_ref(subjects, "github:example/other#1")  # a wildcard binding takes in nothing
    with pytest.raises(ValueError, match="bound equally closely by the subjects app, twin"):
        subject_for_ref({"app": app, "twin": _subject("twin", "github:example/app")}, "github:example/app#9")


# ---- the gate without a case ----


def test_the_gate_judges_a_message_with_no_case_on_its_context():
    subject = Subject(SLUG, ("github:example/app",), Policy(people={"github:pat": "pat"}, roles={"pat": "partner"}, default_reply_mode="direct"))
    context = GateContext(subject=subject, now=NOW)
    outbound = Outbound(ref=ISSUE, channel="github", recipient="pat", purpose="ask", text=TEXT)

    assert context.case is None and outbound.case_id is None
    assert run_gate(outbound, context).send.text == f"@pat {TEXT}"
    title_leak = Outbound(ref=f"github:{REPO}", channel="github", recipient="pat", purpose="ask", text=TEXT, title=LEAK)
    decision = run_gate(title_leak, context)
    assert decision.diverted == "leak scan: local path"
    assert decision.notes == ("leak scan: local path at character 17 of the title",)


def test_outbound_and_the_gate_context_take_keywords_only():
    with pytest.raises(TypeError):
        Outbound(ISSUE, "github", "pat", "ask", TEXT)


# ---- releasing a held message ----


def test_the_operator_sends_a_held_message_after_reading_it(world):
    message_id = world.held()

    output = world.release(message_id)

    assert output.startswith(f"sent message {message_id} to pat on {ISSUE} (gate: passed, 5 filters): https://")
    assert world.posted() == [(ISSUE, None, f"@pat {TEXT}")]
    (preview,) = world.previews
    assert preview.splitlines()[:2] == [f"message {message_id}: ask to pat on {ISSUE}", "gate: passed (5 filters)"]
    (message,) = world.messages()
    held, sent = message.entries
    assert (message.state, held.actor, sent.actor) == ("sent", "agent", "operator")
    assert sent.detail["approval"] == {"by": "operator", "at": LATER.isoformat()}


def test_a_held_message_that_still_leaks_stays_held_until_its_text_changes(world):
    world.set_mode("direct")
    message_id = world.held(text=LEAK)

    with pytest.raises(cw.CommandError, match=f"message {message_id} was not sent: diverted by leak_scan") as raised:
        world.release(message_id)
    assert raised.value.code == cli.DIVERTED_EXIT_CODE and world.previews == []
    assert f"It stays held with that reason; edit it with liaise message send-draft {message_id} --edit" in str(raised.value)

    world.release(message_id, edit=True, editor=lambda text: TEXT)

    assert world.posted() == [(ISSUE, None, f"@pat {TEXT}")]


def test_a_declined_or_dry_run_release_changes_nothing(world):
    message_id = world.held()
    before = copy.deepcopy(world.store)

    assert world.release(message_id, answer=False) == f"nothing sent: message {message_id} stays held"
    assert world.release(message_id, dry_run=True).startswith(f"would send message {message_id} to pat")

    assert world.store == before and world.posted() == []


def test_without_a_terminal_a_held_message_is_not_sent(world, monkeypatch):
    message_id = world.held()
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))

    with pytest.raises(cw.CommandError, match="there is no terminal here, so nothing was sent"):
        cli.message_send_draft(message_id, root=str(world.root), registry=world.registry, store=world.store, now=LATER)
    assert world.posted() == []


def test_a_released_message_is_bound_to_what_the_operator_read(world):
    message_id = world.held()

    def meanwhile(preview):
        ledger = Ledger(world.store)
        message = ledger.get_message(message_id)
        ledger.save_message(message.__class__(**{**message.__dict__, "text": "Something else entirely."}))
        return True

    with pytest.raises(cw.CommandError, match=f"message {message_id} changed while you had it open"):
        cli.message_send_draft(
            message_id, root=str(world.root), registry=world.registry, store=world.store, now=LATER, confirm=meanwhile
        )
    assert world.posted() == []


def test_only_a_held_message_is_sent_or_rejected(world):
    world.set_mode("direct")
    world.send()
    (message,) = world.messages()

    with pytest.raises(cw.CommandError, match=f"message {message.id} is sent, not held: there is nothing to send"):
        world.release(message.id)
    with pytest.raises(cw.CommandError, match=f"message {message.id} is sent, not held: there is nothing to reject"):
        world.reject(message.id, reason="too late")
    with pytest.raises(cw.CommandError, match="no message 'example-app-mffffffff'"):
        world.release("example-app-mffffffff")


def test_rejecting_a_held_message_records_why_and_takes_it_off_the_status(world):
    message_id = world.held()
    status = cli.status(root=str(world.root), store=world.store, now=LATER).splitlines()
    assert "messages outside a case held for the operator: 1" in status
    assert f"  {message_id} ask to {ISSUE}: draft reply mode" in status

    output = world.reject(message_id, reason="  asked on a call instead  ")

    assert output == f"rejected message {message_id} (ask to pat on {ISSUE}): asked on a call instead"
    (message,) = world.messages()
    entry = message.entries[-1]
    assert (message.state, entry.actor, entry.detail["decision"], entry.detail["reason"]) == (
        "rejected", "operator", "reject", "asked on a call instead"
    )
    status = cli.status(root=str(world.root), store=world.store, now=LATER).splitlines()
    assert "messages outside a case held for the operator: 0" in status
    with pytest.raises(cw.CommandError, match="needs a reason"):
        world.reject(message_id, reason=" ")


def test_message_list_and_show_read_the_ledger(world):
    world.set_mode("direct")
    world.send()
    world.set_mode("draft")
    held_id = world.held()
    sent, held = world.messages()

    assert cli.message_list(root=str(world.root), store=world.store).splitlines() == [
        f"{sent.id}\tsent\task to pat on {ISSUE}",
        f"{held_id}\theld\task to pat on {ISSUE}",
    ]
    assert cli.message_list(state="held", root=str(world.root), store=world.store) == f"{held_id}\theld\task to pat on {ISSUE}"
    shown = cli.message_show(held_id, root=str(world.root), store=world.store).splitlines()
    assert shown[:3] == [f"message: {held_id}", f"  subject: {SLUG}", "  state: held"]
    assert "held for: draft reply mode" in shown and f"    {TEXT}" in shown
    with pytest.raises(cw.CommandError, match="message state 'lost' is not one of"):
        cli.message_list(state="lost", root=str(world.root), store=world.store)


def test_the_message_commands_take_their_flags_and_hide_their_seams():
    parser = cw.mk_parser(cli._dispatch_funcs, config=cli._dispatch_config, prog="liaise")
    sent = parser.parse_args(["message", "send", "pat", "--ref", ISSUE, "--text-file", "-", "--title", "Q20", "--dry-run"])
    assert (sent.recipient, sent.ref, sent.text_file, sent.title, sent.dry_run) == ("pat", ISSUE, "-", "Q20", True)
    released = parser.parse_args(["message", "send-draft", "example-app-m1", "--edit"])
    assert (getattr(released, "message-id"), released.edit) == ("example-app-m1", True)
    for command, positionals, seam in (
        ("send", ["pat"], "--notify-fn"),
        ("send", ["pat"], "--registry"),
        ("send-draft", ["x"], "--confirm"),
        ("send-draft", ["x"], "--editor"),
        ("reject-draft", ["x"], "--store"),
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(["message", command, *positionals, seam, "x"])


def test_message_ids_are_the_subjects_slug_and_random_hex(tmp_path):
    ledger = Ledger({})
    ids = {ledger.new_message_id(SLUG) for _ in range(20)}
    assert len(ids) == 20 and all(MESSAGE_ID.fullmatch(i) for i in ids)
    with pytest.raises(ValueError, match="message state 'lost'"):
        list(ledger.messages(state="lost"))
    assert messages.MESSAGE_PURPOSES == ("ask", "reply", "propose")
