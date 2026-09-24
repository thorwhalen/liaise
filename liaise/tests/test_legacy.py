"""Tests for :mod:`liaise.legacy`: the replay of liaise 0.1's gate that shadow mode measures against."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from liaise.legacy import HELD, SENT, flow_class, leak_kinds, legacy_decision
from liaise.policy import FLOWS


def _subject(*, reply_mode="direct", public_channels=("github",), leak_terms=()):
    policy = SimpleNamespace(public_channels=public_channels, leak_terms=leak_terms)
    return SimpleNamespace(policy=policy, reply_mode_for=lambda person: reply_mode)


def _message(text="The export is fixed.", *, channel="github", title=None):
    return SimpleNamespace(text=text, title=title, channel=channel, recipient="pat")


@pytest.mark.parametrize(
    "text, kinds",
    [
        ("see /" + "home/pat/app", ("local path",)),
        ("C:" + "\\\\Users\\\\pat", ("local path",)),
        ("the file at ./app/.env", ("env file",)),
        ("-----BEGIN RSA PRIVATE KEY-----", ("private key",)),
        ("AKIA" + "A" * 16, ("token",)),
        ("nothing here", ()),
        ("xghp_" + "a" * 30, ()),  # glued to a word: not a token
    ],
)
def test_the_0_1_leak_scan_kinds(text, kinds):
    assert leak_kinds(text) == kinds


def test_leak_terms_are_whole_words_in_any_case():
    assert leak_kinds("ORCHID ships", leak_terms=["orchid"]) == ("leak term",)
    assert leak_kinds("orchids ship", leak_terms=["orchid"]) == ()
    assert leak_kinds("orchid", leak_terms=[""]) == ()


def test_the_decision_names_every_reason_in_0_1_order():
    decision = legacy_decision(
        _message("mail pat" + "@example.com", title="/" + "Users" + "/pat"),
        _subject(reply_mode="draft", leak_terms=["heron"]),
        in_case=True,
        shared_diverts=["deslop", "deslop", "notify_recipient"],
    )
    assert decision.flow == "approve"
    assert decision.reasons == ("draft reply mode", "leak scan: email, local path", "deslop", "notify_recipient")


def test_a_clean_message_in_a_case_in_direct_mode_sends():
    assert legacy_decision(_message(), _subject(), in_case=True).flow == "send"
    assert legacy_decision(_message(), _subject(), in_case=False).reasons == ("outside a case",)
    assert legacy_decision(_message(), _subject(), in_case=False, approved=True).flow == "send"


def test_every_policy_flow_has_a_class():
    assert {flow: flow_class(flow) for flow in FLOWS}["send"] == SENT
    assert all(flow_class(flow) == HELD for flow in FLOWS if flow != "send")
    assert flow_class(None) == HELD


def test_the_replay_outlives_public_channels():
    policy = SimpleNamespace(leak_terms=())
    subject = SimpleNamespace(policy=policy, reply_mode_for=lambda person: "direct")
    token = "ghp_" + "a" * 36
    assert legacy_decision(_message(f"use {token}"), subject, in_case=True).flow == "approve"
