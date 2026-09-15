"""Tests for messages outside a case (#28): ``liaise message send``, ``list``, ``show``, ``send-draft``, ``reject-draft``.

A message outside a case goes through the same gate as a case's messages, with no case on
the context, its subject comes only from the bindings, and in 0.1 it waits for the
operator's release whatever the reply mode. Every channel is a FakeGitHubChannel and every
config root is under tmp_path: nothing is posted, and no real configuration or people
record is read. Leak samples and addresses are built by concatenation, so the
no-personal-data guard does not read them as real.
"""

from __future__ import annotations

import copy
import io
import re
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import cw
import pytest
from correspond.errors import ChannelError

from liaise import cli, messages
from liaise.gate import CASELESS_REASON, GateContext, Outbound, run_gate
from liaise.holds import hold
from liaise.ledger import MESSAGE_ID_HEX_DIGITS, Ledger
from liaise.model import Approval, Hold
from liaise.subjects import Policy, Subject, load_subjects, subject_for_ref
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
        """Send a message, which is held, and return its id."""
        with pytest.raises(cw.CommandError) as raised:
            self.send(recipient, **kwargs)
        return re.search(r"is held as (\S+):", str(raised.value)).group(1)

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

    def message(self, message_id):
        return Ledger(self.store).get_message(message_id)

    def posted(self):
        return [(ref.encoded, draft.title, draft.text) for ref, draft in self.github.sent]


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


# ---- sending: always held for the operator in 0.1 ----


@pytest.mark.parametrize("mode", ["draft", "direct"])
def test_a_message_outside_a_case_is_held_recorded_and_the_operator_told_without_its_text(world, mode):
    world.set_mode(mode)

    with pytest.raises(cw.CommandError) as raised:
        world.send()

    assert raised.value.code == cli.DIVERTED_EXIT_CODE
    (message,) = world.messages()
    assert MESSAGE_ID.fullmatch(message.id)
    assert str(raised.value).startswith(
        f"the message to pat on {ISSUE} is held as {message.id}: diverted by reply_mode: {CASELESS_REASON}. "
    )
    assert (message.state, message.reason, message.text, message.purpose) == ("held", CASELESS_REASON, TEXT, "ask")
    (entry,) = message.entries
    assert (entry.kind, entry.actor, entry.detail["decision"]) == ("gate", "agent", "divert")
    ((title, body, _),) = world.notices
    assert title == f"liaise: a message on {SLUG} waits for you"
    assert "cause: reply_mode" in body and "see liaise status" in body
    assert TEXT not in title + body and "pat" not in title + body
    assert world.posted() == []
    assert list(Ledger(world.store).cases()) == []  # a message is not a case


def test_the_operator_is_told_once_while_messages_wait_and_again_after_the_queue_empties(world):
    first, second = world.held(), world.held(text="Also: the totals row is missing.")
    assert len(world.notices) == 1

    world.reject(first, reason="asked on a call")
    world.reject(second, reason="asked on a call")
    world.held(text="A new question.")

    assert len(world.notices) == 2


def test_the_operator_sends_a_held_message_after_reading_it(world):
    message_id = world.held()

    output = world.release(message_id)

    assert output.startswith(f"sent message {message_id} to pat on {ISSUE} (gate: passed, 5 filters): https://")
    assert f"  note: a message outside a case: released by operator at {LATER.isoformat()}" in output.splitlines()
    assert world.posted() == [(ISSUE, None, f"@pat {TEXT}")]
    (preview,) = world.previews
    assert preview.splitlines()[:2] == [f"message {message_id}: ask to pat on {ISSUE}", "gate: passed (5 filters)"]
    message = world.message(message_id)
    held, sent = message.entries
    assert (message.state, held.actor, sent.actor) == ("sent", "agent", "operator")
    assert sent.detail["approval"] == {"by": "operator", "at": LATER.isoformat()}


@pytest.mark.parametrize(
    "recipient, text, filter_name, reason",
    [
        ("pat", LEAK, "leak_scan", "leak scan: local path"),
        ("pat", "It is on the Example-Internal board.", "leak_scan", "leak scan: leak term"),
        ("bram", TEXT, "notify_recipient", "no handle to notify bram"),
    ],
    ids=["a local path", "a leak term", "no handle to mention"],
)
def test_a_released_message_meets_the_same_filters_as_a_cases(world, recipient, text, filter_name, reason):
    message_id = world.held(recipient, text=text)

    with pytest.raises(cw.CommandError, match=f"diverted by {filter_name}: {re.escape(reason)}") as raised:
        world.release(message_id)

    assert raised.value.code == cli.DIVERTED_EXIT_CODE and world.previews == [] and world.posted() == []
    assert f"It stays held with that reason; edit it with liaise message send-draft {message_id} --edit" in str(raised.value)
    message = world.message(message_id)
    assert (message.state, message.reason) == ("held", reason)


def test_a_title_opens_an_issue_the_leak_scan_reads_it_and_an_edit_can_fix_it(world):
    leaky = world.held(ref=f"github:{REPO}", title="On the Example-Internal board")

    with pytest.raises(cw.CommandError, match="diverted by leak_scan: leak scan: leak term") as raised:
        world.release(leaky)
    assert "  note: leak scan: leak term at character 7 of the title" in str(raised.value).splitlines()

    opened = []

    def editor(content):
        opened.append(content)
        return content.replace("Example-Internal", "export")

    world.release(leaky, edit=True, editor=editor)

    assert opened == [f"Title: On the Example-Internal board\n---\n{TEXT}"]
    assert world.posted() == [(f"github:{REPO}", "On the export board", f"@pat {TEXT}")]
    assert world.message(leaky).title == "On the export board"


def test_an_edit_that_breaks_the_title_line_sends_nothing(world):
    message_id = world.held(ref=f"github:{REPO}", title="Q20")

    with pytest.raises(cw.CommandError, match="the edit must keep 'Title: ' and the title on its first line"):
        world.release(message_id, edit=True, editor=lambda content: "just the text")
    assert world.posted() == []


def test_a_channel_that_refuses_a_released_message_keeps_it_held_and_exits_1(world):
    message_id = world.held()
    world.github.send_error = ChannelError("issue is locked", kind="permission")

    with pytest.raises(cw.CommandError, match="send failed: ") as raised:
        world.release(message_id)

    assert raised.value.code == 1
    message = world.message(message_id)
    assert (message.state, message.text) == ("held", TEXT)  # without the mention


def test_a_hold_keeps_a_message_before_the_gate_judges_it(world):
    Ledger(world.store).set_hold(Hold(scope=f"subject:{SLUG}", mode="block"))

    with pytest.raises(cw.CommandError, match=f"the hold on subject:{SLUG} \\(block\\)") as raised:
        world.send()

    assert raised.value.code == cli.DIVERTED_EXIT_CODE
    (message,) = world.messages()
    assert (message.reason, message.notes, message.entries[0].detail["decision"]) == (f"held: subject:{SLUG}", (), "hold")
    assert "cause: subject" in world.notices[0][1] and world.posted() == []


@pytest.mark.parametrize("ref", [f"github:Example/App#12", f"{ISSUE} ", f"{ISSUE}\t", "GitHub:example/APP#12"])
def test_a_repository_hold_cannot_be_dodged_by_how_the_reference_is_written(world, ref):
    hold(Ledger(world.store), "repo:Example/App", mode="block")

    with pytest.raises(cw.CommandError, match="the hold on repo:example/app \\(block\\)"):
        world.send(ref=ref)
    (message,) = world.messages()
    assert message.ref == ISSUE


def test_a_repository_hold_keeps_a_held_message_from_being_released(world):
    message_id = world.held()
    hold(Ledger(world.store), "repo:EXAMPLE/app", mode="cancel")

    with pytest.raises(cw.CommandError, match="the hold on repo:example/app \\(cancel\\) keeps these messages waiting"):
        world.release(message_id)
    assert world.posted() == []


def test_a_repository_hold_keeps_a_message_that_opens_an_issue(world):
    hold(Ledger(world.store), f"repo:{REPO}", mode="cancel")

    with pytest.raises(cw.CommandError, match=f"the hold on repo:{REPO}"):
        world.send(ref=f"github:{REPO}", title="A question")


def test_a_dry_run_judges_and_records_and_tells_nothing(world):
    with pytest.raises(cw.CommandError, match=f"the message to pat on {ISSUE} would be held: diverted by reply_mode"):
        world.send(dry_run=True)
    assert world.store == {} and world.notices == [] and world.posted() == []


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (dict(ref=""), "a message needs --ref"),
        (dict(ref="github:example/other#1"), "no subject binds github:example/other#1"),
        (dict(ref="github:example/app#12abc"), "is not a GitHub issue"),
        (dict(ref="github:example/app#0"), "is not a GitHub issue"),
        (dict(ref="github:example/app#12/"), "is not a GitHub issue"),
        (dict(ref="webinbox:example-site"), "is not a GitHub issue"),
        (dict(ref="email:" + "someone" + "@" + "example.com"), "is not a GitHub issue"),
        (dict(title="Q20"), "a title opens an issue, and github:example/app#12 is one already"),
        (dict(ref=f"github:{REPO}"), "is a repository: a message there opens an issue, which needs a title"),
        (dict(text=""), "exactly one of --text and --text-file"),
        (dict(text_file="note.md"), "exactly one of --text and --text-file"),
        (dict(purpose="decline"), "message purpose 'decline' is not one of: ask, reply, propose"),
    ],
)
def test_message_send_refuses_what_it_cannot_do_and_records_nothing(world, kwargs, message):
    with pytest.raises(cw.CommandError, match=message) as raised:
        world.send(**kwargs)
    assert raised.value.code == 1
    assert world.store == {} and world.notices == [] and world.posted() == []


def test_the_text_can_come_from_a_file_or_standard_input(world, monkeypatch):
    note = world.tmp_path / "q20.md"
    note.write_text("From a file.")
    from_file = world.held(text="", text_file=str(note))
    monkeypatch.setattr(sys, "stdin", io.StringIO("From standard input."))
    from_stdin = world.held(text="", text_file="-")

    assert (world.message(from_file).text, world.message(from_stdin).text) == ("From a file.", "From standard input.")


# ---- the subject comes from the bindings ----


def _subject(slug, *bindings):
    return Subject(slug, tuple(bindings), Policy(people={}, roles={}))


def test_the_closest_binding_names_the_subject_and_a_tie_is_refused():
    app = _subject("app", "github:example/app?labels=partner:pat")
    issue = _subject("issue", "github:example/app#5")
    glob = _subject("glob", "github:example/*")
    site = _subject("site", "webinbox:example-site")
    subjects = {"app": app, "issue": issue, "glob": glob, "site": site}

    assert subject_for_ref(subjects, "github:example/app#5").slug == "issue"
    assert subject_for_ref(subjects, "github:Example/App#6").slug == "app"
    assert subject_for_ref(subjects, "github:example/app").slug == "app"
    with pytest.raises(ValueError, match="no subject binds github:example/other#1"):
        subject_for_ref(subjects, "github:example/other#1")  # a wildcard binding takes in nothing
    with pytest.raises(ValueError, match="no subject binds webinbox:example-site#r1"):
        subject_for_ref(subjects, "webinbox:example-site#r1")  # only GitHub has issues under a binding
    with pytest.raises(ValueError, match="bound equally closely by the subjects app, twin"):
        subject_for_ref({"app": app, "twin": _subject("twin", "github:example/app")}, "github:example/app#9")


# ---- the gate without a case ----


def test_the_gate_holds_a_message_with_no_case_until_the_operator_releases_it():
    subject = Subject(SLUG, ("github:example/app",), Policy(people={"github:pat": "pat"}, roles={"pat": "partner"}, default_reply_mode="direct"))
    outbound = Outbound(ref=ISSUE, channel="github", recipient="pat", purpose="ask", text=TEXT)
    context = GateContext(subject=subject, now=NOW)

    assert context.case is None and outbound.case_id is None
    assert run_gate(outbound, context).diverted == CASELESS_REASON  # direct mode does not let it out
    released = replace(context, approval=Approval(by="operator", at=NOW))
    assert run_gate(outbound, released).send.text == f"@pat {TEXT}"
    title_leak = replace(outbound, ref=f"github:{REPO}", title=LEAK)
    decision = run_gate(title_leak, released)
    assert decision.diverted == "leak scan: local path"
    assert decision.notes[-1] == "leak scan: local path at character 17 of the title"


def test_outbound_and_the_gate_context_take_keywords_only():
    with pytest.raises(TypeError):
        Outbound(ISSUE, "github", "pat", "ask", TEXT)


# ---- releasing ----


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


def test_the_python_api_does_not_release_a_message_for_nobody(world):
    message_id = world.held()

    with pytest.raises(TypeError):
        messages.send_held_message(Ledger(world.store), load_subjects(world.root), message_id, registry=world.registry)
    assert world.posted() == []


def test_a_released_message_is_bound_to_what_the_operator_read(world):
    message_id = world.held()

    def meanwhile(preview):
        ledger = Ledger(world.store)
        ledger.save_message(replace(ledger.get_message(message_id), text="Something else entirely."))
        return True

    with pytest.raises(cw.CommandError, match=f"message {message_id} changed while you had it open"):
        cli.message_send_draft(
            message_id, root=str(world.root), registry=world.registry, store=world.store, now=LATER, confirm=meanwhile
        )
    assert world.posted() == []


def test_only_a_held_message_is_sent_or_rejected(world):
    message_id = world.held()
    world.release(message_id)

    with pytest.raises(cw.CommandError, match=f"message {message_id} is sent, not held: there is nothing to send"):
        world.release(message_id)
    with pytest.raises(cw.CommandError, match=f"message {message_id} is sent, not held: there is nothing to reject"):
        world.reject(message_id, reason="too late")
    with pytest.raises(cw.CommandError, match="no message 'example-app-mffffffff'"):
        world.release("example-app-mffffffff")


def test_rejecting_a_held_message_records_why_and_takes_it_off_the_status(world):
    message_id = world.held()
    status = cli.status(root=str(world.root), store=world.store, now=LATER).splitlines()
    assert "messages outside a case held for the operator: 1" in status
    assert f"  {message_id} ask to {ISSUE}: {CASELESS_REASON}" in status

    output = world.reject(message_id, reason="  asked on a call instead  ")

    assert output == f"rejected message {message_id} (ask to pat on {ISSUE}): asked on a call instead"
    message = world.message(message_id)
    entry = message.entries[-1]
    assert (message.state, entry.actor, entry.detail["decision"], entry.detail["reason"]) == (
        "rejected", "operator", "reject", "asked on a call instead"
    )
    status = cli.status(root=str(world.root), store=world.store, now=LATER).splitlines()
    assert "messages outside a case held for the operator: 0" in status
    with pytest.raises(cw.CommandError, match="needs a reason"):
        world.reject(message_id, reason=" ")


def test_status_survives_a_message_record_it_cannot_read(world):
    world.store["message__example-app-mbroken"] = {"id": "example-app-mbroken", "state": "delayed"}

    status = cli.status(root=str(world.root), store=world.store, now=LATER).splitlines()

    assert any(line.startswith("messages outside a case held for the operator: unreadable (") for line in status)


def test_message_list_and_show_read_the_ledger(world):
    sent_id = world.held()
    world.release(sent_id)
    held_id = world.held(text="A second question.")

    listed = cli.message_list(root=str(world.root), store=world.store).splitlines()
    assert sorted(listed) == sorted([f"{sent_id}\tsent\task to pat on {ISSUE}", f"{held_id}\theld\task to pat on {ISSUE}"])
    assert cli.message_list(state="held", root=str(world.root), store=world.store) == f"{held_id}\theld\task to pat on {ISSUE}"
    shown = cli.message_show(held_id, root=str(world.root), store=world.store).splitlines()
    assert shown[:3] == [f"message: {held_id}", f"  subject: {SLUG}", "  state: held"]
    assert f"held for: {CASELESS_REASON}" in shown and "    A second question." in shown
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


def test_message_ids_are_the_subjects_slug_and_random_hex():
    ledger = Ledger({})
    ids = {ledger.new_message_id(SLUG) for _ in range(20)}
    assert len(ids) == 20 and all(MESSAGE_ID.fullmatch(i) for i in ids)
    with pytest.raises(ValueError, match="message state 'lost'"):
        list(ledger.messages(state="lost"))
    assert messages.MESSAGE_PURPOSES == ("ask", "reply", "propose")
