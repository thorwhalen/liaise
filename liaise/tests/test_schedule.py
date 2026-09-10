"""Tests for liaise.schedule: launchd/systemd file generation, with an env snapshot.

`load=False` everywhere here — these tests write files under `tmp_path` and never
call the real `launchctl`/`systemctl`, so they stay offline and leave the real
scheduler state untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from liaise.schedule import (
    install_schedule,
    job_environment,
    schedule_status,
    uninstall_schedule,
)


def test_job_environment_carries_path_and_home(monkeypatch):
    monkeypatch.delenv("LIAISE_NTFY_TOPIC", raising=False)
    env = job_environment()
    assert "PATH" in env
    assert env["HOME"]
    assert "LIAISE_NTFY_TOPIC" not in env  # unset in this shell -> omitted


def test_job_environment_snapshots_the_ntfy_topic_when_set(monkeypatch):
    monkeypatch.setenv("LIAISE_NTFY_TOPIC", "some-topic")
    env = job_environment()
    assert env["LIAISE_NTFY_TOPIC"] == "some-topic"


def test_job_environment_snapshots_extra_vars(monkeypatch):
    monkeypatch.setenv("SOME_EXTRA_VAR", "value")
    env = job_environment(extra_env_vars=["SOME_EXTRA_VAR"])
    assert env["SOME_EXTRA_VAR"] == "value"


def test_install_launchd_writes_a_plist_with_environment_snapshot(tmp_path):
    launchd_dir = tmp_path / "LaunchAgents"
    log_dir = tmp_path / "Logs"
    path = install_schedule(
        system="Darwin",
        launchd_dir=launchd_dir,
        launchd_log_dir=log_dir,
        load=False,
    )
    content = Path(path).read_text()
    assert "<key>ProgramArguments</key>" in content
    assert "-m</string>" in content
    assert "liaise</string>" in content
    assert "<key>EnvironmentVariables</key>" in content
    assert "<key>PATH</key>" in content
    assert "<key>StartInterval</key>" in content


def test_install_systemd_writes_service_and_timer_with_environment_snapshot(tmp_path):
    systemd_dir = tmp_path / "systemd" / "user"
    result = install_schedule(system="Linux", systemd_dir=systemd_dir, load=False)
    service_path, timer_path = [Path(p.strip()) for p in result.split(",")]

    service_text = service_path.read_text()
    assert "ExecStart=" in service_text
    assert 'Environment="PATH=' in service_text

    timer_text = timer_path.read_text()
    assert "OnUnitActiveSec=" in timer_text
    assert "[Timer]" in timer_text


def test_install_unknown_platform_raises():
    with pytest.raises(NotImplementedError):
        install_schedule(system="Plan9", load=False)


def test_schedule_status_reports_not_installed_then_installed(tmp_path):
    launchd_dir = tmp_path / "LaunchAgents"
    before = schedule_status(system="Darwin", launchd_dir=launchd_dir)
    assert "not installed" in before

    install_schedule(system="Darwin", launchd_dir=launchd_dir, launchd_log_dir=tmp_path / "Logs", load=False)
    after = schedule_status(system="Darwin", launchd_dir=launchd_dir)
    assert after.startswith("installed:")


def test_uninstall_removes_the_written_files(tmp_path):
    launchd_dir = tmp_path / "LaunchAgents"
    install_schedule(system="Darwin", launchd_dir=launchd_dir, launchd_log_dir=tmp_path / "Logs", load=False)
    assert schedule_status(system="Darwin", launchd_dir=launchd_dir).startswith("installed:")

    uninstall_schedule(system="Darwin", launchd_dir=launchd_dir, load=False)
    assert "not installed" in schedule_status(system="Darwin", launchd_dir=launchd_dir)


def test_uninstall_is_idempotent(tmp_path):
    launchd_dir = tmp_path / "LaunchAgents"
    uninstall_schedule(system="Darwin", launchd_dir=launchd_dir, load=False)  # nothing installed
    uninstall_schedule(system="Darwin", launchd_dir=launchd_dir, load=False)  # still fine
