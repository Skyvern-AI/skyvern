"""Run fresh-interpreter scenarios as fork children of one interpreter that ran only their shared prelude.

A scenario starts from the prelude's state, so the prelude must hold only what every scenario would itself
run first (typically imports); anything a scenario's claim depends on happening later belongs in the scenario,
as does any import that reads environment or settings the scenarios vary.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_DRIVER = """
import json, os, signal, sys, time
with open(sys.argv[1]) as spec_file:
    spec = json.load(spec_file)
exec(compile(spec["prelude"], "<prelude>", "exec"), {"__name__": "__main__"})
for name, scenario in spec["scenarios"].items():
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            for fd, suffix in ((1, "stdout"), (2, "stderr")):
                os.dup2(os.open(os.path.join(spec["out"], f"{name}.{suffix}"), os.O_WRONLY | os.O_CREAT), fd)
            for key, value in scenario["env"].items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if scenario["cwd"]:
                os.chdir(scenario["cwd"])
            sys.argv = ["-c", *scenario["argv"]]
            exec(compile(scenario["source"], "<string>", "exec"), {"__name__": "__main__"})
            code = 0
        except SystemExit as exc:
            if exc.code is None or isinstance(exc.code, int):
                code = exc.code or 0
            else:
                print(exc.code, file=sys.stderr)
        except BaseException:
            import traceback
            traceback.print_exc()
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code)
    deadline = time.monotonic() + spec["timeout"]
    done, status = os.waitpid(pid, os.WNOHANG)
    while not done and time.monotonic() < deadline:
        time.sleep(0.02)
        done, status = os.waitpid(pid, os.WNOHANG)
    if not done:
        os.kill(pid, signal.SIGKILL)
        status = os.waitpid(pid, 0)[1]
    with open(os.path.join(spec["out"], "results.jsonl"), "a") as results_file:
        result = {"name": name, "returncode": os.waitstatus_to_exitcode(status), "timed_out": not done}
        print(json.dumps(result), file=results_file)
"""


@dataclass(frozen=True)
class Scenario:
    source: str
    argv: tuple[str, ...] = ()
    env: Mapping[str, str | None] = field(default_factory=dict)
    cwd: Path | None = None


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def run_forked_scenarios(
    prelude: str,
    scenarios: Mapping[str, Scenario],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float = 300,
) -> dict[str, subprocess.CompletedProcess[str]]:
    """Each result reads like ``subprocess.run([sys.executable, "-c", source, *argv])`` of that scenario alone, except
    that atexit handlers never run and ``sys.modules["__main__"]`` is the driver's module."""
    if not hasattr(os, "fork"):
        pytest.skip("forked scenarios need os.fork, which this platform lacks")
    driver_env = dict(os.environ if env is None else env)
    if sys.platform == "darwin":
        # Otherwise a child forked while a prelude thread runs aborts on its first CoreFoundation call (proxy lookup).
        driver_env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
    with tempfile.TemporaryDirectory() as out:
        spec_path = Path(out, "spec.json")
        spec_path.write_text(
            json.dumps(
                {
                    "prelude": prelude,
                    "out": out,
                    "timeout": timeout,
                    "scenarios": {
                        name: {
                            "source": scenario.source,
                            "argv": list(scenario.argv),
                            "env": dict(scenario.env),
                            "cwd": str(scenario.cwd) if scenario.cwd else None,
                        }
                        for name, scenario in scenarios.items()
                    },
                }
            )
        )
        with subprocess.Popen(
            [sys.executable, "-c", _DRIVER, str(spec_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=driver_env,
            cwd=cwd,
            start_new_session=True,
        ) as driver:
            driver_timeout = timeout * (len(scenarios) + 1)
            try:
                stdout, stderr = driver.communicate(timeout=driver_timeout)
                outcome = f"the driver exited with {driver.returncode}"
            except BaseException as exc:
                # Killing only the driver would orphan a scenario that is still running.
                os.killpg(driver.pid, signal.SIGKILL)
                stdout, stderr = driver.communicate()
                if not isinstance(exc, subprocess.TimeoutExpired):
                    raise
                outcome = f"the driver timed out after {driver_timeout}s"
        results_path = Path(out, "results.jsonl")
        finished = {row["name"]: row for row in map(json.loads, _read(results_path).splitlines())}
        results: dict[str, subprocess.CompletedProcess[str]] = {}
        for name, scenario in scenarios.items():
            row = finished.get(name)
            child_stderr = _read(Path(out, f"{name}.stderr"))
            if row is None:
                returncode = 1
                child_stderr += f"{stdout[-2000:]}\n{stderr[-4000:]}\nscenario {name!r} did not finish: {outcome}\n"
            else:
                returncode = row["returncode"]
                if row["timed_out"]:
                    child_stderr += f"\nscenario {name!r} timed out after {timeout}s and was killed\n"
            results[name] = subprocess.CompletedProcess(
                [sys.executable, "-c", scenario.source, *scenario.argv],
                returncode,
                _read(Path(out, f"{name}.stdout")),
                child_stderr,
            )
        return results
