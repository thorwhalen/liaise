"""Tests for liaise.notify: silent no-op when unconfigured, never raises."""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

from liaise.notify import notify


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
