"""Print the suite's metrics (research §9.2) at the end of the run, whatever else ran."""

from __future__ import annotations

import pytest

#: Filled by ``test_scenarios.test_metrics``; printed by the terminal summary below.
REPORT: dict = {}


@pytest.hookimpl(trylast=True)
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not REPORT:
        return
    write = terminalreporter.write_line
    terminalreporter.write_sep("-", "outbound scenario suite")
    write(
        f"severity-weighted miss rate: {REPORT['miss_rate']:.3f} (missed: {', '.join(REPORT['misses']) or 'none'})"
    )
    write(
        f"false-divert rate over the send scenarios: {REPORT['false_divert_rate']:.3f} "
        f"({REPORT['send_scenarios']} scenario(s))"
    )
    delivered = ", ".join(
        f"{k}: {'delivers' if v else 'BLOCKED'}"
        for k, v in REPORT["utility_under_attack"].items()
    )
    write(f"utility under attack: {delivered}")
