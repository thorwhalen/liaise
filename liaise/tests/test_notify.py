"""Tests for liaise.notify: silent no-op when unconfigured, never raises; bodies carry no case text."""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import pytest

from liaise.notify import (
    NOTICE_DAILY_CAP,
    NOTICE_DEPLOY_FAILED,
    NOTICE_ERROR,
    NOTICE_EVENTS,
    NOTICE_NO_CHANNEL,
    NOTICE_RUN_LOST,
    NOTICE_TITLES,
    notice_body,
    notice_title,
    notify,
)


def test_a_notice_body_names_the_subject_cases_event_and_cause_and_points_at_case_show():
    body = notice_body(
        NOTICE_DEPLOY_FAILED, subject="example-app", case_ids=["example-app-1", "example-app-2"], cause="exit code 2"
    )
    assert body.splitlines() == [
        "subject: example-app",
        "case: example-app-1, example-app-2",
        f"event: {NOTICE_DEPLOY_FAILED}",
        "cause: exit code 2",
        "see liaise case show example-app-1",
        "see liaise case show example-app-2",
    ]


def test_a_notice_body_without_a_case_or_cause_points_at_the_status():
    assert notice_body(NOTICE_RUN_LOST, subject="example-app").splitlines() == [
        "subject: example-app",
        f"event: {NOTICE_RUN_LOST}",
        "see liaise status",
    ]


def test_a_notice_body_refuses_an_event_it_does_not_know():
    """The events are a closed vocabulary: a free-text event could carry what a case holds."""
    assert "the partner's message" not in NOTICE_EVENTS
    with pytest.raises(ValueError, match="notice event"):
        notice_body("the partner's message", subject="example-app")


def test_every_event_has_a_title_of_fixed_text_and_the_case_ids():
    """S9 #2: a title goes out with the body, and is built from nothing a case holds."""
    assert set(NOTICE_TITLES) == set(NOTICE_EVENTS)
    for event in NOTICE_EVENTS:
        title = notice_title(event, subject="example-app", case_ids=["example-app-1"], cause="crashed")
        assert "example-app" in title


def test_a_title_names_a_cause_only_for_an_error_and_only_as_its_class():
    kept_out = "exit code 2"
    deploy = notice_title(NOTICE_DEPLOY_FAILED, subject="example-app", case_ids=["example-app-1"], cause=kept_out)
    assert deploy == "liaise: example-app-1 landed but did not deploy"
    error = notice_title(NOTICE_ERROR, subject="example-app", case_ids=["example-app-1"], cause="timed_out")
    assert error == "liaise: example-app-1 timed_out"
    with pytest.raises(ValueError, match="error class"):
        notice_title(NOTICE_ERROR, subject="example-app", case_ids=["example-app-1"], cause="the partner's message")


def test_a_title_names_the_case_not_its_reporter_and_the_subject_without_a_case():
    no_channel = notice_title(NOTICE_NO_CHANNEL, subject="example-app", case_ids=["example-app-1"])
    assert no_channel == "no channel to reach the reporter of example-app-1"
    assert notice_title(NOTICE_DAILY_CAP, subject="example-app") == "liaise: example-app reached its daily cap"
    with pytest.raises(ValueError, match="notice event"):
        notice_title("the partner's message", subject="example-app")


def test_notify_noops_when_topic_env_unset(monkeypatch):
    monkeypatch.delenv("LIAISE_TEST_TOPIC", raising=False)
    with patch("liaise.notify.urllib.request.urlopen") as urlopen:
        sent = notify("title", "body", topic_env="LIAISE_TEST_TOPIC")
    assert sent is False
    urlopen.assert_not_called()


def test_notify_posts_when_topic_env_set(monkeypatch):
    monkeypatch.setenv("LIAISE_TEST_TOPIC", "some-topic")
    with patch("liaise.notify.urllib.request.urlopen") as urlopen:
        sent = notify("title", "body", topic_env="LIAISE_TEST_TOPIC")
    assert sent is True
    urlopen.assert_called_once()


def test_notify_never_raises_when_the_post_fails(monkeypatch):
    monkeypatch.setenv("LIAISE_TEST_TOPIC", "some-topic")
    with patch(
        "liaise.notify.urllib.request.urlopen",
        side_effect=urllib.error.URLError("unreachable"),
    ):
        sent = notify("title", "body", topic_env="LIAISE_TEST_TOPIC")
    assert sent is False
