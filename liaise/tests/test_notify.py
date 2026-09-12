"""Tests for liaise.notify: silent no-op when unconfigured, never raises; bodies carry no case text."""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import pytest

from liaise.notify import (
    NOTICE_DEPLOY_FAILED,
    NOTICE_EVENTS,
    NOTICE_RUN_LOST,
    notice_body,
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
