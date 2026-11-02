import os
import resource
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest
import structlog
from pdfplumber.utils.exceptions import PdfminerException

from skyvern.forge.sdk.utils import capped_subprocess
from skyvern.forge.sdk.utils.capped_subprocess import (
    MEMORY_CAP_ENFORCED,
    CappedSubprocessError,
    run_in_capped_subprocess,
)
from skyvern.forge.sdk.utils.capped_subprocess_child import memory_allowance

MIB = 1024 * 1024


def _raise_memory_error_wrapped_like_pdfplumber() -> None:
    try:
        raise MemoryError
    except MemoryError as exc:
        raise PdfminerException(exc)


def _ignore(*args: object) -> None:
    return None


def _text_of(length: int) -> str:
    return "a" * length


def _address_space_limit() -> tuple[int, int]:
    return resource.getrlimit(resource.RLIMIT_AS)


def _hold_memory_then_wait(megabytes: int) -> int:
    held = b"x" * (megabytes * MIB)
    time.sleep(20)
    return len(held)


def _raise_a_long_error() -> None:
    raise ValueError("x" * 100_000)


_KEPT: list[bytes] = []


def _keep_memory_and_return(megabytes: int) -> int:
    _KEPT.append(b"x" * (megabytes * MIB))
    return len(_KEPT)


class _CountingStream:
    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        self.bytes_read += len(data)
        return data

    def readline(self, size: int = -1) -> bytes:
        data = self._stream.readline(size)
        self.bytes_read += len(data)
        return data

    def readinto(self, buffer: Any) -> int:
        count = self._stream.readinto(buffer)
        self.bytes_read += count or 0
        return count


class _RunsCodeWhenUnpickled:
    def __init__(self, marker: Path) -> None:
        self.marker = marker

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        return Path.write_text, (self.marker, "ran")


def _return_a_value_that_runs_code_when_unpickled(marker: Path) -> _RunsCodeWhenUnpickled:
    return _RunsCodeWhenUnpickled(marker)


def _fake_cgroup(directory: Path, max_mib: int, current_mib: int, inactive_file_mib: int | None = None) -> Path:
    (directory / "memory.max").write_text(f"{max_mib * MIB}\n")
    (directory / "memory.current").write_text(f"{current_mib * MIB}\n")
    if inactive_file_mib is not None:
        (directory / "memory.stat").write_text(f"anon {MIB}\ninactive_file {inactive_file_mib * MIB}\n")
    return directory


@pytest.mark.parametrize(
    ("func", "args", "expected_exit"),
    [
        (os._exit, (3,), "exit status 3"),
        (signal.raise_signal, (signal.SIGKILL,), "killed by SIGKILL"),
    ],
)
def test_a_child_that_dies_without_a_result_fails_the_call(
    func: Callable[..., Any], args: tuple[Any, ...], expected_exit: str
) -> None:
    with pytest.raises(CappedSubprocessError) as excinfo:
        run_in_capped_subprocess(func, *args, memory_limit_bytes=256 * MIB, max_result_bytes=MIB, timeout_seconds=60)

    assert excinfo.value.kind == "crashed"
    assert expected_exit in str(excinfo.value)


def test_a_memory_error_pdfplumber_wrapped_is_reported_as_out_of_memory() -> None:
    with pytest.raises(CappedSubprocessError) as excinfo:
        run_in_capped_subprocess(
            _raise_memory_error_wrapped_like_pdfplumber,
            memory_limit_bytes=256 * MIB,
            max_result_bytes=MIB,
            timeout_seconds=60,
        )

    assert excinfo.value.kind == "out_of_memory"
    assert "ran out of memory" in str(excinfo.value)


@pytest.mark.parametrize(
    ("make_oversized_item", "most_bytes_read"),
    [
        pytest.param(os.urandom, 4096, id="bytes are refused before any of them is read"),
        pytest.param(_text_of, MIB + 1, id="str is read no further than the cap"),
    ],
)
def test_a_result_past_max_result_bytes_is_refused_without_reading_it_in_full(
    monkeypatch: pytest.MonkeyPatch, make_oversized_item: Callable[[int], bytes | str], most_bytes_read: int
) -> None:
    streams: list[_CountingStream] = []
    bounded_reader = capped_subprocess._BoundedReader

    def counting_reader(stream: Any, limit: int) -> Any:
        streams.append(_CountingStream(stream))
        return bounded_reader(streams[-1], limit)

    monkeypatch.setattr(capped_subprocess, "_BoundedReader", counting_reader)

    with pytest.raises(CappedSubprocessError) as excinfo:
        run_in_capped_subprocess(
            make_oversized_item, 4 * MIB, memory_limit_bytes=256 * MIB, max_result_bytes=MIB, timeout_seconds=60
        )

    assert excinfo.value.kind == "result_too_large"
    assert "more than the 1 MiB" in str(excinfo.value)
    assert streams[0].bytes_read <= most_bytes_read


def test_a_child_that_outgrows_its_allowance_is_killed_and_reported_out_of_memory() -> None:
    """RLIMIT_AS stops it on Linux, and the parent's memory watchdog on macOS, where the limit cannot be set."""
    with pytest.raises(CappedSubprocessError) as excinfo:
        run_in_capped_subprocess(
            _hold_memory_then_wait, 400, memory_limit_bytes=64 * MIB, max_result_bytes=MIB, timeout_seconds=60
        )

    assert excinfo.value.kind == "out_of_memory"
    assert "ran out of memory" in str(excinfo.value)


def test_a_child_that_returns_past_its_allowance_is_still_reported_out_of_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capped_subprocess, "MEMORY_CAP_ENFORCED", False)
    monkeypatch.setattr(capped_subprocess, "_WATCHDOG_POLL_SECONDS", 10)

    with pytest.raises(CappedSubprocessError) as excinfo:
        run_in_capped_subprocess(
            _keep_memory_and_return, 50, memory_limit_bytes=40 * MIB, max_result_bytes=MIB, timeout_seconds=60
        )

    assert excinfo.value.kind == "out_of_memory"


def test_the_growth_of_a_child_is_measured_by_the_parent_before_the_child_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample = capped_subprocess._MemoryWatchdog.sample

    def delayed_baseline_sample(watchdog: capped_subprocess._MemoryWatchdog) -> int | None:
        if watchdog.baseline_bytes is None:
            # Reproduce a parent preempted between receiving ready and sampling its baseline.
            time.sleep(0.5)
        return sample(watchdog)

    monkeypatch.setattr(capped_subprocess._MemoryWatchdog, "sample", delayed_baseline_sample)

    with structlog.testing.capture_logs() as logs:
        run_in_capped_subprocess(
            _keep_memory_and_return, 50, memory_limit_bytes=256 * MIB, max_result_bytes=MIB, timeout_seconds=60
        )

    (finished,) = (log for log in logs if log["event"] == "Capped subprocess finished")
    assert 40 * MIB <= finished["rss_growth_sampled_bytes"] <= 120 * MIB


def test_a_child_on_a_platform_without_resource_limits_still_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows has no resource module and no RLIMIT_AS to apply; a call there must not need either."""
    bootstrap = capped_subprocess._CHILD_BOOTSTRAP.replace("; main()", "; sys.platform = 'win32'; main()")
    assert "win32" in bootstrap
    monkeypatch.setattr(capped_subprocess, "_CHILD_BOOTSTRAP", bootstrap)

    assert (
        run_in_capped_subprocess(os.getpid, memory_limit_bytes=256 * MIB, max_result_bytes=MIB, timeout_seconds=60)
        != os.getpid()
    )


def test_a_parent_that_cannot_read_the_childs_memory_still_gets_its_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(pid: int) -> None:
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(capped_subprocess.psutil, "Process", refuse)

    with structlog.testing.capture_logs() as logs:
        result = run_in_capped_subprocess(
            _ignore, memory_limit_bytes=256 * MIB, max_result_bytes=MIB, timeout_seconds=60
        )

    assert result is None
    (finished,) = (log for log in logs if log["event"] == "Capped subprocess finished")
    assert finished["rss_growth_sampled_bytes"] is None


def test_a_long_error_message_is_cut_in_the_child_instead_of_failing_the_result_cap() -> None:
    with pytest.raises(CappedSubprocessError) as excinfo:
        run_in_capped_subprocess(
            _raise_a_long_error, memory_limit_bytes=256 * MIB, max_result_bytes=8 * 1024, timeout_seconds=60
        )

    assert excinfo.value.kind == "error"
    assert 0 < len(str(excinfo.value)) <= 4096


def test_a_result_that_is_not_plain_data_is_refused_not_run(tmp_path: Path) -> None:
    marker = tmp_path / "marker"

    with pytest.raises(CappedSubprocessError) as excinfo:
        run_in_capped_subprocess(
            _return_a_value_that_runs_code_when_unpickled,
            marker,
            memory_limit_bytes=256 * MIB,
            max_result_bytes=MIB,
            timeout_seconds=60,
        )

    assert excinfo.value.kind == "crashed"
    assert "not plain data" in str(excinfo.value)
    assert not marker.exists()


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(["ok"], id="not a dict"),
        pytest.param({"status": "unknown"}, id="unknown status"),
        pytest.param({"status": "ok"}, id="ok without a value"),
        pytest.param({"status": "error", "message": 3}, id="error without a text message"),
        pytest.param({"status": "ok", "value": 1, "allowance_bytes": "big"}, id="a size that is not an int"),
        pytest.param({"status": "ok", "value": 1, "headroom_source": 4}, id="a source that is not text"),
    ],
)
def test_an_outcome_of_the_wrong_shape_fails_the_call_not_the_parent(
    monkeypatch: pytest.MonkeyPatch, outcome: object
) -> None:
    child = f"import pickle, sys; sys.stdout.buffer.write(pickle.dumps({outcome!r})); sys.stdout.flush()"
    monkeypatch.setattr(capped_subprocess, "_CHILD_BOOTSTRAP", child)

    with pytest.raises(CappedSubprocessError) as excinfo:
        run_in_capped_subprocess(_ignore, memory_limit_bytes=256 * MIB, max_result_bytes=MIB, timeout_seconds=60)

    assert excinfo.value.kind == "crashed"
    assert "unexpected shape" in str(excinfo.value)


_V1_UNLIMITED = "9223372036854771712\n"


@pytest.mark.parametrize(
    ("cgroup", "meminfo_mib", "reserve_mib", "expected_mib", "source"),
    [
        pytest.param(None, None, 256, 1536, "none", id="nothing readable"),
        pytest.param(None, 4096, 256, 1536, "meminfo", id="meminfo room"),
        pytest.param(None, 700, 256, 188, "meminfo", id="meminfo tight, reserve subtracted"),
        pytest.param(None, 600, 256, None, "meminfo", id="meminfo under the floor"),
        pytest.param(
            {"memory.max": "max\n", "memory.current": f"{900 * MIB}\n"}, 700, 256, 188, "meminfo", id="v2 no limit"
        ),
        pytest.param({"memory.current": f"{900 * MIB}\n"}, 700, 256, 188, "meminfo", id="v2 limit file missing"),
        pytest.param((6144, 1024), 50, 256, 1536, "cgroup", id="cgroup room, meminfo ignored"),
        pytest.param((6144, 5284), None, 0, 604, "cgroup", id="tight"),
        pytest.param((6144, 5000), None, 256, 632, "cgroup", id="tight with the result reserved"),
        pytest.param((6144, 3000), None, 1000, 1536, "cgroup", id="room despite other calls' reservations"),
        pytest.param((6144, 3000, 0), None, 2760, 128, "cgroup", id="exactly the floor"),
        pytest.param((6144, 3000, 0), None, 2761, None, "cgroup", id="a MiB under the floor"),
        pytest.param((6144, 6000), None, 256, None, "cgroup", id="under the margin and the reserve"),
        pytest.param((6144, 5284, 2000), None, 256, 1536, "cgroup", id="inactive page cache counts as free"),
        pytest.param(
            {"memory.max": f"{6144 * MIB}\n", "memory.current": f"{1024 * MIB}\n", "memory.stat": "inactive_file x\n"},
            4096,
            256,
            1536,
            "cgroup",
            id="a limit with an unparseable memory.stat counts no inactive cache, not meminfo",
        ),
        pytest.param(
            {"memory.max": f"{6144 * MIB}\n"},
            16384,
            0,
            None,
            "cgroup",
            id="a limit with unreadable usage refuses the call, not meminfo",
        ),
        pytest.param(
            {
                "memory/memory.stat": f"hierarchical_memory_limit {8192 * MIB}\ntotal_inactive_file {512 * MIB}\n",
                "memory/memory.limit_in_bytes": f"{2048 * MIB}\n",
                "memory/memory.usage_in_bytes": f"{1536 * MIB}\n",
            },
            50,
            256,
            512,
            "cgroup_v1",
            id="v1 takes the smaller limit and discounts inactive cache",
        ),
        pytest.param(
            {
                "memory/memory.stat": f"hierarchical_memory_limit {2048 * MIB}\n",
                "memory/memory.limit_in_bytes": _V1_UNLIMITED,
                "memory/memory.usage_in_bytes": f"{1024 * MIB}\n",
            },
            16384,
            256,
            512,
            "cgroup_v1",
            id="v1 limit from the hierarchy alone",
        ),
        pytest.param(
            {"memory/memory.limit_in_bytes": f"{2048 * MIB}\n"},
            16384,
            0,
            None,
            "cgroup_v1",
            id="v1 limit with unreadable usage refuses the call",
        ),
        pytest.param(
            {
                "memory/memory.stat": f"hierarchical_memory_limit {_V1_UNLIMITED}",
                "memory/memory.limit_in_bytes": _V1_UNLIMITED,
                "memory/memory.usage_in_bytes": f"{1024 * MIB}\n",
            },
            700,
            256,
            188,
            "meminfo",
            id="v1 without a limit falls through to meminfo",
        ),
    ],
)
def test_memory_allowance_follows_the_pods_free_memory(
    tmp_path: Path,
    cgroup: dict[str, str] | tuple[int, ...] | None,
    meminfo_mib: int | None,
    reserve_mib: int,
    expected_mib: int | None,
    source: str,
) -> None:
    if isinstance(cgroup, tuple):
        _fake_cgroup(tmp_path, *cgroup)
    elif cgroup:
        for name, content in cgroup.items():
            (tmp_path / name).parent.mkdir(exist_ok=True)
            (tmp_path / name).write_text(content)
    if meminfo_mib is not None:
        (tmp_path / "meminfo").write_text(f"MemTotal: 16777216 kB\nMemAvailable: {meminfo_mib * 1024} kB\n")

    sized = memory_allowance(1536 * MIB, reserve_mib * MIB, cgroup_dir=tmp_path, meminfo_path=tmp_path / "meminfo")

    assert (sized.allowance, sized.source) == (None if expected_mib is None else expected_mib * MIB, source)


class _TakesMemoryWhenUnpickled:
    """Stands in for the imports that take memory from the pod while the child unpickles its request."""

    def __init__(self, memory_current: Path) -> None:
        self.memory_current = memory_current

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        return Path.write_text, (self.memory_current, f"{5344 * MIB}\n")


@pytest.mark.parametrize("pod_is_tight_at_the_start", [True, False], ids=["parent refuses", "child refuses"])
def test_a_pod_without_headroom_fails_the_call_instead_of_risking_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pod_is_tight_at_the_start: bool
) -> None:
    cgroup = _fake_cgroup(tmp_path, max_mib=6144, current_mib=6000 if pod_is_tight_at_the_start else 1024)
    monkeypatch.setattr(capped_subprocess, "MEMORY_CAP_ENFORCED", True)
    monkeypatch.setattr(capped_subprocess, "CGROUP_DIR", str(cgroup))
    if pod_is_tight_at_the_start:

        def popen_must_not_run(*args: object, **kwargs: object) -> None:
            raise AssertionError("a child was started in a pod with no headroom")

        monkeypatch.setattr(capped_subprocess.subprocess, "Popen", popen_must_not_run)

    with pytest.raises(CappedSubprocessError) as excinfo:
        run_in_capped_subprocess(
            _ignore,
            _TakesMemoryWhenUnpickled(cgroup / "memory.current"),
            memory_limit_bytes=1536 * MIB,
            max_result_bytes=128 * MIB,
            timeout_seconds=60,
        )

    assert excinfo.value.kind == "no_headroom"
    assert "not enough free memory" in str(excinfo.value)


def test_calls_running_at_once_are_sized_against_each_other(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capped_subprocess, "CGROUP_DIR", str(_fake_cgroup(tmp_path, max_mib=1024, current_mib=0)))
    reserve = capped_subprocess._reserved_memory

    with (
        reserve(512 * MIB, 16 * MIB) as first,
        reserve(512 * MIB, 16 * MIB) as second,
        reserve(512 * MIB, 16 * MIB) as third,
    ):
        sizes = [(r.allowance, r.held_by_others) for r in (first, second, third)]
        assert sizes == [(512 * MIB, 0), (224 * MIB, 528 * MIB), (None, 768 * MIB)]
    with reserve(512 * MIB, 16 * MIB) as after_release:
        assert (after_release.allowance, after_release.held_by_others) == (512 * MIB, 0)


@pytest.mark.parametrize(("memory_factor", "is_refused"), [(6, True), (1, False)])
def test_the_result_is_reserved_at_its_decoded_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_factor: int, is_refused: bool
) -> None:
    class ChildStarted(Exception):
        pass

    def start_child(*args: object, **kwargs: object) -> None:
        raise ChildStarted

    cgroup = _fake_cgroup(tmp_path, max_mib=1024, current_mib=0)
    monkeypatch.setattr(capped_subprocess, "MEMORY_CAP_ENFORCED", True)
    monkeypatch.setattr(capped_subprocess, "CGROUP_DIR", str(cgroup))
    monkeypatch.setattr(capped_subprocess.subprocess, "Popen", start_child)

    def call() -> None:
        run_in_capped_subprocess(
            _ignore,
            memory_limit_bytes=512 * MIB,
            max_result_bytes=128 * MIB,
            result_memory_factor=memory_factor,
            timeout_seconds=60,
        )

    if is_refused:
        with pytest.raises(CappedSubprocessError) as excinfo:
            call()
        assert excinfo.value.kind == "no_headroom"
    else:
        with pytest.raises(ChildStarted):
            call()


def test_a_child_is_killed_when_the_call_cannot_finish_starting_it(monkeypatch: pytest.MonkeyPatch) -> None:
    class ThreadThatCannotStart:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    processes: list[subprocess.Popen[bytes]] = []

    def recording_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        processes.append(subprocess.Popen(*args, **kwargs))
        return processes[-1]

    monkeypatch.setattr(
        capped_subprocess,
        "subprocess",
        SimpleNamespace(Popen=recording_popen, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired),
    )
    monkeypatch.setattr(
        capped_subprocess,
        "threading",
        SimpleNamespace(
            Thread=ThreadThatCannotStart, Timer=threading.Timer, Event=threading.Event, Lock=threading.Lock
        ),
    )

    with pytest.raises(RuntimeError):
        run_in_capped_subprocess(time.sleep, 60, memory_limit_bytes=256 * MIB, max_result_bytes=MIB, timeout_seconds=60)

    assert processes[0].poll() is not None


def test_a_child_that_does_not_exit_after_its_result_is_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capped_subprocess, "_EXIT_GRACE_SECONDS", 0.2)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    assert capped_subprocess._wait_for_exit(process) == -signal.SIGKILL


@pytest.mark.skipif(not MEMORY_CAP_ENFORCED, reason="only Linux enforces RLIMIT_AS; elsewhere the child is uncapped")
def test_the_child_cannot_raise_its_own_memory_cap() -> None:
    soft, hard = run_in_capped_subprocess(
        _address_space_limit, memory_limit_bytes=256 * MIB, max_result_bytes=MIB, timeout_seconds=60
    )

    assert soft == hard != resource.RLIM_INFINITY
