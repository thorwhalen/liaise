"""A fake `claude` CLI for processor tests: scripted stream-JSON on stdout.

This is not a test module; the leading underscore keeps pytest from collecting it.

:func:`fake_claude` writes an executable script that behaves like
`claude -p --output-format stream-json` for one scenario. It goes through
`conftest.write_executable_script`, so it also runs on Windows. The event shapes match what
:func:`liaise.errors.parse_stream` reads.

- ``success``: init, an assistant turn, then a successful `result` carrying
  `structured_output` with the given outcomes.
- ``auth_expired``: an `api_retry` with `authentication_failed`, then an error result.
- ``quota_exhausted``: a rejected `rate_limit_event`, then an error result.
- ``crash``: init, then exit 137 with no `result` line.
- ``config_error``: a message on stderr and exit 1, before any stream.
- ``hang``: init, then sleep for `sleep_s` seconds. Used for timeouts, cancel and heartbeat.

The script records the argv it received as JSON in `<path>.argv.json`, so tests can assert
on flags: `--json-schema`, `--session-id`, `--resume`, `--permission-mode`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Sequence

from liaise.tests.conftest import write_executable_script

SCENARIOS = (
    "success",
    "auth_expired",
    "quota_exhausted",
    "crash",
    "config_error",
    "hang",
)

_SCRIPT = """
import json, sys, time
CONFIG = json.loads({config!r})
with open({argv_log!r}, "w") as f:
    json.dump(sys.argv[1:], f)

def emit(event):
    sys.stdout.write(json.dumps(event) + "\\n")
    sys.stdout.flush()

scenario = CONFIG["scenario"]
sid = CONFIG["session_id"]
if scenario == "config_error":
    sys.stderr.write("error: unknown option '--bogus'\\n")
    sys.exit(1)
emit({{"type": "system", "subtype": "init", "session_id": sid}})
if scenario == "crash":
    sys.exit(137)
if scenario == "hang":
    time.sleep(CONFIG["sleep_s"])
    sys.exit(0)
if scenario == "success":
    emit({{"type": "assistant", "session_id": sid,
          "message": {{"content": [{{"type": "text", "text": "working"}}]}}}})
    emit({{"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
          "result": "done", "num_turns": 3, "total_cost_usd": 0.0123,
          "usage": {{"input_tokens": 100, "output_tokens": 50}},
          "structured_output": {{"outcomes": CONFIG["outcomes"], "summary": "done"}}}})
    sys.exit(0)
if scenario == "auth_expired":
    emit({{"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 3,
          "error": "authentication_failed"}})
    emit({{"type": "result", "subtype": "error_during_execution", "is_error": True,
          "session_id": sid, "result": "Not logged in - please run /login"}})
    sys.exit(1)
if scenario == "quota_exhausted":
    emit({{"type": "rate_limit_event", "rate_limit_info": {{"status": "rejected",
          "resets_at": CONFIG["resets_at"], "rate_limit_type": "five_hour",
          "utilization": 1.0}}}})
    emit({{"type": "result", "subtype": "error_during_execution", "is_error": True,
          "session_id": sid, "result": "You've hit your limit"}})
    sys.exit(1)
"""


def fake_claude(
    path: Path,
    *,
    scenario: str = "success",
    outcomes: Optional[Sequence[dict]] = None,
    session_id: str = "sess-fake-1",
    sleep_s: float = 30.0,
    resets_at: int = 1767272400,
) -> Path:
    """Write a fake `claude` for `scenario` at `path`; return the runnable path.

    The runnable path can differ from `path` on Windows. Recorded argv goes to
    `argv_log(path)`.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; known: {', '.join(SCENARIOS)}")
    config = json.dumps(
        {
            "scenario": scenario,
            "outcomes": list(
                outcomes if outcomes is not None else [{"kind": "reply", "text": "Done."}]
            ),
            "session_id": session_id,
            "sleep_s": sleep_s,
            "resets_at": resets_at,
        }
    )
    body = _SCRIPT.format(config=config, argv_log=str(argv_log(path)))
    return write_executable_script(path, body)


def argv_log(path: Path) -> Path:
    """Where the fake `claude` written at `path` records the argv it was given."""
    return path.with_name(path.name + ".argv.json")
