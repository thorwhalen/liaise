"""Tests for ``liaise vet`` and ``liaise.vet.before_send`` (liaise #37, slice L4).

Every channel is a FakeGitHubChannel, every disclosure the invented one of the outbound
suite (Ada, Bram, Heron), and every config root is under tmp_path: nothing is sent, and no
real configuration or people record is read. The acceptance line of the issue is
:func:`test_the_acceptance_line`.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone

import pytest
from correspond.errors import NeedsApproval, Refused
from correspond.model import ConversationRef, Draft

from liaise import cli, vet as vetting
from liaise.__main__ import main
from liaise.gate import outbound_policy
from liaise.subjects import Policy, Subject
from liaise.testing import FakeGitHubChannel, demo_registry
from liaise.tests.outbound import fixtures

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
REF = fixtures.PUBLIC_ISSUE
EXPORT = "The export fix is in."
#: An issue of a private repository a user owns: its readers are named, so a post there is
#: neither organisation-wide nor public.
PRIVATE = "github:example/solo#3"
#: An issue of a private repository an organisation owns.
ORG = "github:example/app-internal#3"
HERON = "The export fix is in. Heron slips to October."


@pytest.fixture(autouse=True)
def no_real_acquaint(monkeypatch):
    monkeypatch.setitem(sys.modules, "acquaint", None)


def fixture_disclosure(people, *, projects=(), audience=None, today=None):
    return fixtures.disclosure_for(people, audience=audience)


@pytest.fixture
def registry():
    github = FakeGitHubChannel(clock=lambda: NOW, visibility="public")
    github.add_issue("example/app", 12, author="ada-lorne", title="Export", body="The export drops a row.", created_at=NOW)
    github.set_visibility("example/app-internal", "private", owner_type="Organization")
    github.set_visibility("example/solo", "private", owner_type="User")
    return demo_registry(github=github)


#: The fixture subject: it binds example/app, answers Ada directly, and knows her handle.
SUBJECT_TOML = """
bindings = ["github:example/app?labels=partner:ada", "github:example/solo?labels=partner:ada"]

[policy]
default_reply_mode = "direct"
people = { "github:ada-lorne" = "ada" }
roles = { ada = "partner" }
"""


@pytest.fixture
def root(tmp_path):
    config = tmp_path / "config"
    (config / "subjects").mkdir(parents=True)
    (config / "subjects" / "app.toml").write_text(SUBJECT_TOML)
    return str(config)


def run_cli(argv, stdin, monkeypatch, capsys):
    """``liaise`` as a console script: the exit code and what it printed."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    monkeypatch.setattr(sys, "argv", ["liaise", *argv])
    with pytest.raises(SystemExit) as raised:
        main()
    out = capsys.readouterr()
    return raised.value.code, out.out, out.err


def vet_cli(text, *args, registry, root, monkeypatch, capsys, **kwargs):
    """``liaise vet`` through the CLI function, with the fixture disclosure: (exit code, output)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    call = dict(ref=REF, to=("ada",), root=root, registry=registry, now=NOW, disclosure=fixture_disclosure)
    call.update(kwargs)
    try:
        shown = cli.vet(**call)
        return 0, shown
    except SystemExit as exit_:
        return exit_.code, capsys.readouterr().out


def direct_subject(**policy):
    """A subject binding example/app that answers directly: only the policy table holds a draft back."""
    return Subject("app", ("github:example/app", "github:example/solo", "github:example/app-internal"), Policy(people={}, roles={}, default_reply_mode="direct", **policy))


def vet_record(text, *, registry, **kwargs):
    policy = __import__("functools").partial(outbound_policy, disclosure=fixture_disclosure)
    filters = tuple(policy if f is outbound_policy else f for f in vetting.VET_FILTERS)
    call = dict(ref=REF, to="ada", registry=registry, now=NOW, subjects={"app": direct_subject()}, outbound_filters=filters)
    call.update(kwargs)
    return vetting.vet(text, **call)


# ---- the acceptance line ----


def test_the_acceptance_line(registry, root, monkeypatch, capsys):
    # Heron to a public issue: a draft for the operator, with the reason and the audience.
    code, shown = vet_cli(HERON, registry=registry, root=root, monkeypatch=monkeypatch, capsys=capsys, untainted=True)
    assert code == 2
    assert "'project:heron' is amber" in shown
    assert "audience: world-readable" in shown
    # With the provenance unknown, private content to a public audience is refused outright
    # (the taint rule of liaise #36, stricter than the issue's acceptance line predates).
    code, shown = vet_cli(HERON, registry=registry, root=root, monkeypatch=monkeypatch, capsys=capsys)
    assert code == 3 and "never released as written" in shown

    # The export sentence alone: 0 where a post can be withdrawn; on a public issue, which
    # cannot, a delay, which is 2, since whoever vets it posts it at once.
    code, shown = vet_cli(EXPORT, registry=registry, root=root, monkeypatch=monkeypatch, capsys=capsys, untainted=True, ref=PRIVATE)
    assert code == 0, shown
    code, shown = vet_cli(EXPORT, registry=registry, root=root, monkeypatch=monkeypatch, capsys=capsys, untainted=True)
    assert code == 2 and shown.startswith("send (delay)") and "cannot be withdrawn" in shown

    code, shown = vet_cli(EXPORT, registry=registry, root=root, monkeypatch=monkeypatch, capsys=capsys)
    assert code == 2
    assert "provenance is unknown, which counts as tainted" in shown

    code, shown = vet_cli(
        f"Use {fixtures.TOKEN} to log in.", registry=registry, root=root, monkeypatch=monkeypatch, capsys=capsys
    )
    assert code == 3

    code, shown = vet_cli(HERON, registry=registry, root=root, monkeypatch=monkeypatch, capsys=capsys, json=True, untainted=True)
    found = json.loads(shown)
    assert code == 2 and found["route"] == "draft" and found["exit_code"] == 2
    assert {"flow", "reasons", "audience", "readers", "record", "payload_hash"} <= set(found)


def test_the_verdict_never_holds_the_text_or_the_value_found(registry):
    found = vet_record(f"Heron ships; token {fixtures.TOKEN}", registry=registry)
    dumped = json.dumps(found, default=str)
    assert fixtures.TOKEN not in dumped
    assert "Heron ships" not in dumped


# ---- the record ----


def test_a_clean_draft_to_a_public_issue_is_a_delay_which_exits_2(registry):
    found = vet_record(EXPORT, registry=registry, tainted=False)
    assert found["flow"] == "delay" and found["route"] == "send" and found["exit_code"] == 2
    assert vetting.immediate_route(found["flow"]) == "draft"


def test_unknown_provenance_counts_as_tainted_and_tainted_says_so(registry):
    unknown = vet_record(EXPORT, registry=registry)
    tainted = vet_record(EXPORT, registry=registry, tainted=True)
    for found in (unknown, tainted):
        assert found["route"] == "draft"
        assert "taint" in found["rules"]


def test_a_private_repository_is_judged_by_its_own_audience(registry):
    org = vet_record(EXPORT, registry=registry, ref=ORG, tainted=False)
    assert org["record"]["verdict"]["audience"]["scope"] == "org"
    assert org["audience"].startswith("organisation-wide")
    assert org["flow"] == "delay" and org["exit_code"] == 2
    solo = vet_record(EXPORT, registry=registry, ref=PRIVATE, tainted=False)
    assert solo["record"]["verdict"]["audience"]["scope"] not in ("org", "public")
    assert solo["flow"] == "send" and solo["exit_code"] == 0


def test_a_second_person_in_to_counts_as_a_reader(registry):
    found = vet_record(HERON, registry=registry, ref="email:ada" + fixtures.AT + "example.org", to=("ada", "bram"), tainted=False)
    assert found["route"] == "block"  # Heron is sealed from Bram


def test_the_subject_that_binds_the_reference_judges_it(registry):
    subject = direct_subject(tainted_runs="send")
    found = vet_record(EXPORT, registry=registry, subjects={"app": subject})
    assert found["subject"] == "app" and "taint" not in found["rules"]
    unbound = vet_record(EXPORT, registry=registry, subjects={})
    assert unbound["subject"] == "(unbound)" and "taint" in unbound["rules"]


def test_vet_is_the_gate_without_the_hold_and_the_mention():
    names = [f.__name__ for f in vetting.VET_FILTERS]
    assert "outside_a_case" not in names and "notify_recipient" not in names
    assert names[0] == "outbound_policy"


def test_an_audience_nobody_can_compute_is_public(registry):
    found = vet_record(EXPORT, registry={}, tainted=False)
    assert "world-readable (assumed" in found["audience"]


# ---- the command line ----


def test_repeated_readers_are_all_kept(root, monkeypatch, capsys):
    import liaise.release

    monkeypatch.setattr(liaise.release.correspond, "audience", lambda *a, **k: 1 / 0)
    code, out, _ = run_cli(
        ["vet", "--ref", REF, "--to", "ada", "--to", "bram", "--cc", "cy", "--root", root, "--json"], EXPORT, monkeypatch, capsys
    )
    assert set(json.loads(out)["readers"]) >= {"ada", "bram", "cy"}


def test_the_console_script_reads_stdin_and_exits_with_the_route(root, monkeypatch, capsys):
    # No registry reaches the console script: correspond is not asked (nothing leaves the
    # test), so the audience is unknown, hence public.
    import liaise.release

    def offline(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(liaise.release.correspond, "audience", offline)
    code, out, _ = run_cli(["vet", "--ref", REF, "--to", "ada", "--root", root], HERON, monkeypatch, capsys)
    assert code == 2 and "draft" in out
    code, out, _ = run_cli(["vet", "--ref", REF, "--root", root, "--json"], f"key {fixtures.TOKEN}", monkeypatch, capsys)
    assert code == 3 and json.loads(out)["route"] == "block"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (dict(ref=""), "needs --ref"),
        (dict(ref="nonsense"), "not a conversation reference"),
        (dict(tainted=True, untainted=True), "at most one"),
        (dict(text="  "), "empty"),
    ],
)
def test_what_cannot_be_vetted_is_one_line_and_exit_1(kwargs, message, registry, root):
    call = dict(ref=REF, text="hello", root=root, registry=registry, now=NOW)
    call.update(kwargs)
    with pytest.raises(Exception) as raised:
        cli.vet(**call)
    assert getattr(raised.value, "code", None) == 1
    assert message in str(raised.value)


# ---- before_send ----


def _draft_ref():
    return ConversationRef.parse(REF)


def _audience(registry):
    from liaise.gate import Outbound
    from liaise.release import audience_of

    return audience_of(Outbound(ref=REF, channel="github", recipient="", purpose="reply", text="x"), registry=registry)


def test_before_send_raises_needs_approval_for_a_draft_and_refused_for_a_block(registry, monkeypatch):
    policy = __import__("functools").partial(outbound_policy, disclosure=fixture_disclosure)
    filters = tuple(policy if f is outbound_policy else f for f in vetting.VET_FILTERS)
    subjects = {"app": direct_subject()}
    monkeypatch.setattr(vetting, "vet", _with_defaults(vetting.vet, subjects=subjects, outbound_filters=filters, now=NOW))
    audience = _audience(registry)
    with pytest.raises(NeedsApproval) as held:  # provenance unknown: the taint rule
        vetting.before_send(_draft_ref(), Draft(text=EXPORT), audience, operation="send", dry_run=True, message_id=None)
    assert held.value.details["flow"] == "approve" and "taint" in held.value.details["rules"]
    assert "provenance is unknown" in held.value.reason
    for text in (HERON, f"key {fixtures.TOKEN}"):
        with pytest.raises(Refused) as refused:
            vetting.before_send(_draft_ref(), Draft(text=text), audience, operation="send", dry_run=True, message_id=None)
        assert fixtures.TOKEN not in refused.value.reason


def test_before_send_holds_what_it_cannot_vet(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("no config")

    monkeypatch.setattr(vetting, "vet", broken)
    with pytest.raises(NeedsApproval) as held:
        vetting.before_send(_draft_ref(), Draft(text=EXPORT), None, operation="send", dry_run=False, message_id=None)
    assert "could not vet" in held.value.reason


def test_before_send_holds_a_delay_since_the_write_happens_at_once(registry, monkeypatch):
    subject = direct_subject(tainted_runs="send")
    monkeypatch.setattr(vetting, "vet", _with_defaults(vetting.vet, subjects={"app": subject}, now=NOW))
    audience = _audience(registry)
    with pytest.raises(NeedsApproval) as held:
        vetting.before_send(_draft_ref(), Draft(text=EXPORT), audience, operation="send", dry_run=False, message_id=None)
    assert held.value.details["flow"] == "delay"
    assert "at once" in held.value.reason


def test_before_send_lets_a_send_go(monkeypatch):
    subject = direct_subject(tainted_runs="send")
    monkeypatch.setattr(vetting, "vet", _with_defaults(vetting.vet, subjects={"app": subject}, now=NOW))
    private = {"ref": REF, "scope": "named", "retractable": True, "external": False, "readers": []}
    assert vetting.before_send(_draft_ref(), Draft(text=EXPORT), private, operation="send", dry_run=False, message_id=None) is None


def test_before_send_is_what_correspond_calls(registry, monkeypatch):
    import correspond

    subject = direct_subject()
    monkeypatch.setattr(vetting, "vet", _with_defaults(vetting.vet, subjects={"app": subject}, now=NOW))
    result = correspond.send(REF, f"key {fixtures.TOKEN}", registry=registry, before_send=vetting.before_send)
    assert not result.ok and result.error_kind == "refused"
    github = registry["github"]
    assert github.sent == []


def _with_defaults(function, **defaults):
    def call(*args, **kwargs):
        return function(*args, **{**defaults, **kwargs})

    return call


def test_a_dash_after_to_is_standard_input_as_acquaint_write_runs_it(root, monkeypatch, capsys):
    import liaise.release

    monkeypatch.setattr(liaise.release.correspond, "audience", lambda *a, **k: 1 / 0)
    code, out, _ = run_cli(["vet", "--ref", REF, "--to", "ada", "-", "--root", root, "--json"], EXPORT, monkeypatch, capsys)
    assert list(json.loads(out)["readers"]) == ["ada"]
