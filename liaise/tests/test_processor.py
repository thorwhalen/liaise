"""Tests for liaise.processor: ClaudeHeadless against a fake `claude`, and EchoProcessor.

Every run here is a real detached process running a fake (see `_fake_claude`), never the
real `claude`. Waits poll `status` in a bounded loop, so a broken run fails the test
instead of hanging it. Only the graceful-cancel tests need POSIX signals.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from liaise.model import Health, Outcome, RunRecord, RunResult
from liaise.processor import (
    CHILD_ENV_OVERRIDES,
    PROMPT_FILE,
    RECORD_FILE,
    STREAM_FILE,
    ClaudeHeadless,
    EchoProcessor,
    Job,
    Processor,
    child_env,
)
from liaise.tests._fake_claude import argv_log, fake_claude
from liaise.tests.conftest import write_executable_script

#: Free of the characters cmd.exe re-parses (& | < > ^), so the Windows `.bat` fake
#: receives it intact.
SCHEMA = {
    "type": "object",
    "properties": {"outcomes": {"type": "array"}, "summary": {"type": "string"}},
    "required": ["outcomes"],
}
WAIT_S = 10.0
POLL_S = 0.05
HANG_S = 60.0
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

_IGNORES_SIGINT = """
import json, signal, sys, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
sys.stdout.write(json.dumps({"type": "system", "subtype": "init"}) + "\\n")
sys.stdout.flush()
time.sleep(60)
"""
_GARBAGE = "import sys\nsys.stdout.write('not json\\n{\"type\": \"resu\\n')\n"


def _job(tmp_path, **overrides) -> Job:
    fields = dict(
        run_id="r1",
        case_id="pat-1",
        subject="pat",
        prompt="Fix the export for pat.",
        cwd=str(tmp_path),
        permission_mode="auto",
        timeout_minutes=60,
        json_schema=SCHEMA,
    )
    fields.update(overrides)
    return Job(**fields)


def _processor(tmp_path, claude_bin, **kwargs) -> ClaudeHeadless:
    return ClaudeHeadless(claude_bin=claude_bin, runs_dir=tmp_path / "runs", **kwargs)


def _script(tmp_path, body: str) -> Path:
    return write_executable_script(tmp_path / "claude", body)


def _wait_until(predicate, *, what: str):
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(POLL_S)
    raise AssertionError(f"gave up after {WAIT_S}s waiting for {what}")


def _wait_finished(processor, record: RunRecord) -> RunRecord:
    def finished():
        current = processor.status(record)
        return current if current.status == "finished" else None

    return _wait_until(finished, what=f"run {record.run_id} to finish")


def _run_to_the_end(processor, tmp_path) -> RunResult:
    return processor.collect(_wait_finished(processor, processor.start(_job(tmp_path))))


def _raw_record(processor, run_id="r1") -> dict:
    return json.loads((processor.run_dir(run_id) / RECORD_FILE).read_text())


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


# ---- ClaudeHeadless: a run from start to collect ----


def test_a_successful_run_starts_detached_and_collects_its_outcomes(tmp_path):
    outcomes = [
        {
            "kind": "ask",
            "text": "Two things first.",
            "questions": ["Which browsers? (default: all current ones)"],
        },
        {"kind": "note", "text": "The export test is flaky."},
    ]
    path = tmp_path / "claude"
    processor = _processor(tmp_path, fake_claude(path, outcomes=outcomes))
    job = _job(tmp_path)

    record = processor.start(job)

    assert (record.status, record.mode, record.case_id) == ("running", "fresh", "pat-1")
    assert record.pid is not None
    run_dir = processor.run_dir("r1")
    assert (run_dir / PROMPT_FILE).read_text(encoding="utf-8") == job.prompt
    assert record.stream_path == str(run_dir / STREAM_FILE)

    finished = _wait_finished(processor, record)
    result = processor.collect(finished)

    assert finished.ended_at is not None
    assert result.error is None
    assert result.outcomes == (
        Outcome(
            kind="ask",
            text="Two things first.",
            questions=("Which browsers? (default: all current ones)",),
        ),
        Outcome(kind="note", text="The export test is flaky."),
    )
    assert result.cost_usd == pytest.approx(0.0123)
    assert result.usage == {"input_tokens": 100, "output_tokens": 50}
    assert result.session_id == "sess-fake-1"
    assert result.summary == "done"
    assert RunRecord.from_dict(_raw_record(processor)) == finished

    argv = json.loads(argv_log(path).read_text())
    assert _flag(argv, "-p") == (
        f"Read and follow the instructions in {run_dir / PROMPT_FILE}"
    )
    assert _flag(argv, "--output-format") == "stream-json"
    assert "--verbose" in argv
    assert _flag(argv, "--permission-mode") == "auto"
    assert json.loads(_flag(argv, "--json-schema")) == SCHEMA
    assert str(uuid.UUID(_flag(argv, "--session-id"))) == record.session_id
    assert "--resume" not in argv
    assert "--max-turns" not in argv


def test_resume_continues_the_given_session(tmp_path):
    path = tmp_path / "claude"
    processor = _processor(tmp_path, fake_claude(path))

    record = processor.resume("sess-previous", _job(tmp_path, session_id="sess-previous"))
    _wait_finished(processor, record)

    assert (record.mode, record.session_id) == ("resume", "sess-previous")
    argv = json.loads(argv_log(path).read_text())
    assert _flag(argv, "--resume") == "sess-previous"
    assert "--session-id" not in argv
    assert _flag(argv, "--permission-mode") == "auto"
    assert processor.collect(record).error is None


@pytest.mark.parametrize(
    "scenario, error",
    [
        ("auth_expired", "auth_expired"),
        ("quota_exhausted", "quota_exhausted"),
        ("crash", "crashed"),
        ("config_error", "config_error"),
    ],
)
def test_failed_runs_are_classified(tmp_path, scenario, error):
    processor = _processor(tmp_path, fake_claude(tmp_path / "claude", scenario=scenario))
    result = _run_to_the_end(processor, tmp_path)
    assert result.error == error
    assert result.outcomes == ()


def test_a_rejected_quota_keeps_its_rate_limit_info(tmp_path):
    claude = fake_claude(
        tmp_path / "claude", scenario="quota_exhausted", resets_at=1767272400
    )
    result = _run_to_the_end(_processor(tmp_path, claude), tmp_path)
    assert result.rate_limit["status"] == "rejected"
    assert result.rate_limit["resets_at"] == 1767272400


def test_a_success_with_a_malformed_outcome_has_no_outcomes(tmp_path):
    bad = [{"kind": "reply", "text": "Done."}, {"kind": "celebrate"}]
    processor = _processor(tmp_path, fake_claude(tmp_path / "claude", outcomes=bad))
    result = _run_to_the_end(processor, tmp_path)
    assert (result.error, result.outcomes) == ("needs_human", ())


def test_a_garbage_stream_is_crashed(tmp_path):
    result = _run_to_the_end(_processor(tmp_path, _script(tmp_path, _GARBAGE)), tmp_path)
    assert (result.error, result.outcomes) == ("crashed", ())


def test_collect_never_raises_on_a_partial_stream_left_by_another_tick(tmp_path):
    processor = ClaudeHeadless(runs_dir=tmp_path / "runs")
    run_dir = processor.run_dir("r9")
    run_dir.mkdir(parents=True)
    (run_dir / STREAM_FILE).write_text(
        '{"type": "system", "subtype": "init"}\n{"type": "result", "is_err'
    )
    record = RunRecord(
        run_id="r9",
        case_id="pat-1",
        subject="pat",
        mode="fresh",
        status="running",
        started_at=T0,
    )
    assert processor.collect(record).error == "crashed"


# ---- ClaudeHeadless: status, cancel, idempotence ----


def test_status_never_blocks_and_sets_the_heartbeat(tmp_path):
    claude = fake_claude(tmp_path / "claude", scenario="hang", sleep_s=HANG_S)
    processor = _processor(tmp_path, claude)
    record = processor.start(_job(tmp_path))
    try:
        began = time.monotonic()
        current = processor.status(record)
        assert time.monotonic() - began < 1.0
        assert (current.status, current.ended_at) == ("running", None)
        assert current.heartbeat_at is not None
        assert RunRecord.from_dict(_raw_record(processor)) == current
    finally:
        processor.cancel(record, mode="now")
    _wait_finished(processor, record)


def test_a_hung_run_cancelled_now_finishes_and_collects_as_timed_out(tmp_path):
    claude = fake_claude(tmp_path / "claude", scenario="hang", sleep_s=HANG_S)
    processor = _processor(tmp_path, claude)
    record = processor.start(_job(tmp_path))
    try:
        assert processor.collect(record) is None
    finally:
        processor.cancel(record, mode="now")

    assert "cancel_requested_at" in _raw_record(processor)
    finished = _wait_finished(processor, record)
    result = processor.collect(finished, timed_out=True)
    assert result.error == "timed_out"
    assert result.session_id == record.session_id


def _stubborn_run(tmp_path, **kwargs) -> tuple[ClaudeHeadless, RunRecord]:
    processor = _processor(tmp_path, _script(tmp_path, _IGNORES_SIGINT), **kwargs)
    record = processor.start(_job(tmp_path))
    stream = Path(record.stream_path)
    _wait_until(lambda: stream.stat().st_size > 0, what="SIGINT to be ignored")
    return processor, record


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_graceful_cancel_interrupts_then_terminates_once_the_grace_is_over(tmp_path):
    processor, record = _stubborn_run(tmp_path, grace_s=0.0)
    try:
        processor.cancel(record)
        first = _raw_record(processor)
        assert first["cancel_signal"] == "SIGINT"
        time.sleep(0.3)
        assert processor.status(record).status == "running"  # it ignored the interrupt

        processor.cancel(record)
        second = _raw_record(processor)
        assert second["cancel_signal"] == "SIGTERM"
        assert second["cancel_requested_at"] == first["cancel_requested_at"]
    finally:
        processor.cancel(record, mode="now")
    _wait_finished(processor, record)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_graceful_cancel_within_the_grace_period_sends_nothing_more(tmp_path):
    processor, record = _stubborn_run(tmp_path, grace_s=HANG_S)
    try:
        processor.cancel(record)
        processor.cancel(record)
        assert _raw_record(processor)["cancel_signal"] == "SIGINT"
        assert processor.status(record).status == "running"
    finally:
        processor.cancel(record, mode="now")
    _wait_finished(processor, record)


def _counting_body(count_path: Path) -> str:
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": {"outcomes": [{"kind": "note"}]},
    }
    return (
        "import json\n"
        f"with open({str(count_path)!r}, 'a') as f:\n"
        "    f.write('run\\n')\n"
        f"print(json.dumps({result!r}))\n"
    )


def test_start_is_idempotent_on_the_run_id(tmp_path):
    count = tmp_path / "spawns.txt"
    claude = _script(tmp_path, _counting_body(count))
    processor = _processor(tmp_path, claude)
    job = _job(tmp_path)

    first = processor.start(job)
    assert processor.start(job) == first
    _wait_finished(processor, first)

    assert processor.start(job).pid == first.pid
    assert _processor(tmp_path, claude).start(job).pid == first.pid  # the next tick
    assert count.read_text().splitlines() == ["run"]


def test_a_run_started_by_another_process_is_checked_by_its_pid(tmp_path):
    processor = ClaudeHeadless(runs_dir=tmp_path / "runs")
    alive = RunRecord(
        run_id="r-alive",
        case_id="pat-1",
        subject="pat",
        mode="fresh",
        status="running",
        started_at=T0,
        pid=os.getpid(),
    )
    exited = subprocess.Popen([sys.executable, "-c", "pass"])
    exited.wait()  # reaped, so its pid is gone
    dead = replace(alive, run_id="r-dead", pid=exited.pid)

    assert processor.status(alive).status == "running"
    assert processor.status(dead).status == "finished"


# ---- ClaudeHeadless: environment, preflight, configuration ----


def _env_dumping_body(out: Path) -> str:
    return (
        "import json, os\n"
        f"with open({str(out)!r}, 'w') as f:\n"
        "    json.dump(dict(os.environ), f)\n"
    )


def test_the_run_does_not_inherit_anthropic_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "fake-token-for-tests")
    monkeypatch.setenv("LIAISE_TEST_MARKER", "kept")
    dump = tmp_path / "env.json"
    processor = _processor(tmp_path, _script(tmp_path, _env_dumping_body(dump)))

    _wait_finished(processor, processor.start(_job(tmp_path)))

    env = json.loads(dump.read_text())
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["LIAISE_TEST_MARKER"] == "kept"
    assert env["CLAUDE_CODE_MAX_RETRIES"] == "3"


def test_child_env_drops_the_credentials_whatever_their_case():
    environ = {"PATH": "/bin", "ANTHROPIC_API_KEY": "k", "anthropic_auth_token": "t"}
    assert child_env(environ) == {"PATH": "/bin", **CHILD_ENV_OVERRIDES}


def test_preflight_passes_with_a_runnable_claude_and_an_existing_cwd(tmp_path):
    path = tmp_path / "claude"
    processor = _processor(tmp_path, fake_claude(path))
    assert processor.preflight(_job(tmp_path)) == Health(ok=True)
    assert json.loads(argv_log(path).read_text()) == ["auth", "status"]
    assert not processor.run_dir("r1").exists()  # it started nothing


def test_preflight_reports_an_expired_login_as_auth_expired(tmp_path):
    claude = fake_claude(tmp_path / "claude", auth="auth_expired")
    assert _processor(tmp_path, claude).preflight(_job(tmp_path)) == Health(
        ok=False, error="auth_expired"
    )
    skipping = _processor(tmp_path, claude, auth_check=None)
    assert skipping.preflight(_job(tmp_path)) == Health(ok=True)


def test_a_login_check_that_does_not_answer_in_time_tells_nothing(tmp_path):
    processor = _processor(
        tmp_path, _script(tmp_path, "import time\ntime.sleep(5)\n"), auth_timeout_s=0.5
    )
    began = time.monotonic()
    assert processor.preflight(_job(tmp_path)) == Health(ok=True)
    assert time.monotonic() - began < WAIT_S


def test_preflight_reports_a_missing_binary_as_config_error(tmp_path):
    processor = _processor(tmp_path, tmp_path / "no-such-claude")
    assert processor.preflight(_job(tmp_path)) == Health(ok=False, error="config_error")


def test_preflight_reports_a_missing_cwd_or_runs_dir_as_config_error(tmp_path):
    claude = fake_claude(tmp_path / "claude")
    missing_cwd = _job(tmp_path, cwd=str(tmp_path / "gone"))
    assert _processor(tmp_path, claude).preflight(missing_cwd).error == "config_error"
    no_runs_dir = ClaudeHeadless(claude_bin=claude)
    assert no_runs_dir.preflight(_job(tmp_path)).error == "config_error"


def test_starting_a_missing_binary_records_a_finished_config_error(tmp_path):
    processor = _processor(tmp_path, tmp_path / "no-such-claude")
    record = processor.start(_job(tmp_path))
    assert (record.status, record.pid) == ("finished", None)
    assert processor.collect(record).error == "config_error"


def test_starting_without_runs_dir_says_what_is_missing(tmp_path):
    with pytest.raises(ValueError, match="runs_dir="):
        ClaudeHeadless().start(_job(tmp_path))


@pytest.mark.parametrize("run_id", ["", "..", "a/b", "a\\b"])
def test_a_run_id_must_be_one_path_segment(tmp_path, run_id):
    with pytest.raises(ValueError, match="one path segment"):
        ClaudeHeadless(runs_dir=tmp_path / "runs").run_dir(run_id)


def test_both_processors_satisfy_the_protocol(tmp_path):
    assert isinstance(ClaudeHeadless(runs_dir=tmp_path), Processor)
    assert isinstance(EchoProcessor(), Processor)


# ---- EchoProcessor ----


def test_echo_processor_returns_scripted_results_by_case_then_the_default(tmp_path):
    scripted = RunResult(
        run_id="ignored", outcomes=(Outcome(kind="deliver", text="It's live."),)
    )
    default = RunResult(run_id="ignored", outcomes=(Outcome(kind="reply", text="On it."),))
    echo = EchoProcessor(results={"pat-1": scripted}, default=default)

    first = echo.start(_job(tmp_path))
    other = echo.resume("sess-7", _job(tmp_path, run_id="r2", case_id="pat-2"))

    assert (first.status, first.mode) == ("finished", "fresh")
    assert (other.status, other.mode, other.session_id) == ("finished", "resume", "sess-7")
    assert echo.collect(first) == replace(
        scripted, run_id="r1", session_id=first.session_id
    )
    assert echo.collect(other).outcomes == default.outcomes
    assert echo.collect(other).session_id == "sess-7"
    assert [job.run_id for job in echo.jobs] == ["r1", "r2"]


def test_echo_processor_without_a_script_reports_a_note_and_is_idempotent(tmp_path):
    echo = EchoProcessor()
    record = echo.start(_job(tmp_path))

    assert echo.start(_job(tmp_path)) == record
    assert len(echo.jobs) == 1
    (outcome,) = echo.collect(record).outcomes
    assert outcome.kind == "note"
    assert echo.collect(record).error is None
    assert echo.collect(record, timed_out=True).error == "timed_out"
    assert echo.status(record) == record
    assert echo.cancel(record, mode="now") == record
    assert echo.cancels == [("r1", "now")]


def test_echo_processor_preflight_returns_the_given_health_and_starts_nothing(tmp_path):
    held = Health(ok=False, error="auth_expired")
    echo = EchoProcessor(health=held)
    assert echo.preflight(_job(tmp_path)) == held
    assert (echo.jobs, len(echo.preflights)) == ([], 1)
    assert EchoProcessor().preflight(_job(tmp_path)) == Health(ok=True)
