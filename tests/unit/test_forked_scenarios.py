import signal

from tests.unit.forked_scenarios import Scenario, run_forked_scenarios


def test_forked_scenarios_report_a_timeout_an_exit_code_and_a_traceback() -> None:
    results = run_forked_scenarios(
        "",
        {
            "hangs": Scenario("import time\ntime.sleep(60)"),
            "exits": Scenario("import sys\nsys.exit(3)"),
            "raises": Scenario("raise ValueError('boom')"),
        },
        timeout=2,
    )

    assert results["hangs"].returncode == -signal.SIGKILL
    assert "timed out" in results["hangs"].stderr
    assert results["exits"].returncode == 3
    assert results["raises"].returncode == 1
    assert "Traceback" in results["raises"].stderr
    assert "ValueError: boom" in results["raises"].stderr
