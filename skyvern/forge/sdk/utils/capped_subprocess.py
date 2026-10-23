"""Run a function in a fresh Python process under a memory cap and a wall-clock timeout, so work that can allocate
without bound, such as parsing an untrusted file, kills only its child and raises CappedSubprocessError."""

import contextlib
import io
import math
import os
import pickle
import re
import signal
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from typing import IO, Any, Literal, NamedTuple, NoReturn, TypeVar, TypeVarTuple, Unpack, cast

import psutil
import structlog

from skyvern.forge.sdk.utils.capped_subprocess_child import CGROUP_DIR, MEMINFO_PATH, memory_allowance

LOG = structlog.get_logger(__name__)

_T = TypeVar("_T")
_Ts = TypeVarTuple("_Ts")

CappedSubprocessFailure = Literal["error", "out_of_memory", "timeout", "crashed", "result_too_large", "no_headroom"]

# Only Linux enforces RLIMIT_AS; macOS refuses to lower it, so there the memory watchdog is the only memory guard.
MEMORY_CAP_ENFORCED = sys.platform.startswith("linux")
_NO_HEADROOM_MESSAGE = "there was not enough free memory to run it safely"
# The parent holds a result decoded, which takes more than its pickled bytes: word dicts 5.9x for real words and up
# to 6.4x for crafted ones on Python 3.11, text with astral characters up to 6x. A result of mostly bytes passes 1.
DEFAULT_RESULT_MEMORY_FACTOR = 7

# -I keeps the child from running a sitecustomize injected through PYTHONPATH (opentelemetry-instrument does this),
# so the parent's sys.path is handed over explicitly instead.
_CHILD_BOOTSTRAP = (
    "import sys; sys.path[:] = sys.argv[1:]; from skyvern.forge.sdk.utils.capped_subprocess_child import main; main()"
)
# The CPU limit only catches a child whose parent died before killing it: a single-threaded child cannot use more
# CPU time than wall time, so the parent's timer always fires first.
_CPU_LIMIT_MARGIN_SECONDS = 10
_EXIT_GRACE_SECONDS = 5
_WATCHDOG_POLL_SECONDS = 0.25
_STDERR_TAIL_BYTES = 4096
_URL_QUERY = re.compile(r"(\w+://[^\s?#]*)[?#]\S*")


class CappedSubprocessError(Exception):
    def __init__(self, message: str, kind: CappedSubprocessFailure) -> None:
        super().__init__(message)
        self.kind = kind


class _ResultTooLarge(Exception):
    pass


class _NotPlainData(pickle.UnpicklingError):
    pass


class _UnexpectedShape(Exception):
    pass


class _BoundedReader:
    """Hands the unpickler at most limit bytes of the child's stdout. Reads ask the pipe for no more than one byte
    past what is left, and readinto refuses a buffer larger than what is left before reading into it."""

    def __init__(self, stream: io.BufferedReader, limit: int) -> None:
        self._stream = stream
        self._remaining = limit

    def read(self, size: int | None = -1) -> bytes:
        return self._counted(self._stream.read(self._clamped(size)))

    def readline(self, size: int | None = -1) -> bytes:
        return self._counted(self._stream.readline(self._clamped(size)))

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer)
        if view.nbytes > self._remaining:
            raise _ResultTooLarge
        count = self._stream.readinto(view) or 0
        self._remaining -= count
        return count

    def _clamped(self, size: int | None) -> int:
        most = self._remaining + 1
        return most if size is None or size < 0 else min(size, most)

    def _counted(self, data: bytes) -> bytes:
        self._remaining -= len(data)
        if self._remaining < 0:
            raise _ResultTooLarge
        return data


class _PlainDataUnpickler(pickle.Unpickler):
    """Loads the child's output, which is plain builtin data. Unpickling then cannot import or call anything: an
    output that names any class or function is refused."""

    def find_class(self, module_name: str, global_name: str, /) -> Any:
        raise _NotPlainData(f"the child sent {module_name}.{global_name}")


_OUTCOME_STATUSES = ("ok", "error", "out_of_memory", "no_headroom", "result_too_large")
_OUTCOME_INT_FIELDS = ("allowance_bytes", "headroom_bytes")
_OUTCOME_STR_FIELDS = ("error_type", "headroom_source")


def _checked_outcome(loaded: object) -> dict[str, Any]:
    if not isinstance(loaded, dict) or loaded.get("status") not in _OUTCOME_STATUSES:
        raise _UnexpectedShape("the outcome is not a dict with a known status")
    if loaded["status"] == "ok" and "value" not in loaded:
        raise _UnexpectedShape("an ok outcome has no value")
    if loaded["status"] == "error" and not isinstance(loaded.get("message"), str):
        raise _UnexpectedShape("an error outcome has no message")
    for name in _OUTCOME_INT_FIELDS:
        if loaded.get(name) is not None and type(loaded[name]) is not int:
            raise _UnexpectedShape(f"{name} is not an int")
    for name in _OUTCOME_STR_FIELDS:
        if loaded.get(name) is not None and not isinstance(loaded[name], str):
            raise _UnexpectedShape(f"{name} is not a string")
    return loaded


def _checked_ready(loaded: object) -> int | None:
    if not isinstance(loaded, dict) or loaded.get("status") != "ready":
        raise _UnexpectedShape("the first message is not a ready message")
    allowance = loaded.get("allowance_bytes")
    if allowance is not None and type(allowance) is not int:
        raise _UnexpectedShape("the ready message has an allowance that is not an int")
    return allowance


class _MemoryWatchdog:
    """Reads the child's resident memory with psutil and kills it once it has grown past its allowance from the
    baseline taken when it reported ready. RLIMIT_AS bounds a Linux child first; here it also bounds a macOS one."""

    def __init__(self, process: subprocess.Popen[bytes], allowance_bytes: int) -> None:
        self.allowance_bytes = allowance_bytes
        self.baseline_bytes: int | None = None
        self.peak_sampled_bytes: int | None = None
        self.rss_at_kill_bytes: int | None = None
        self.killed = threading.Event()
        self._process = process
        self._child: psutil.Process | None = None
        self._lock = threading.Lock()
        self._finished = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._started = False

    @property
    def growth_bytes(self) -> int | None:
        if self.baseline_bytes is None or self.peak_sampled_bytes is None:
            return None
        return self.peak_sampled_bytes - self.baseline_bytes

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def learn(self, allowance_bytes: int | None) -> None:
        if allowance_bytes is not None:
            self.allowance_bytes = allowance_bytes
        self.baseline_bytes = self.sample()

    def sample(self) -> int | None:
        try:
            if self._child is None:
                self._child = psutil.Process(self._process.pid)
            rss = self._child.memory_info().rss
        except psutil.Error:
            return None
        with self._lock:
            self.peak_sampled_bytes = rss if self.peak_sampled_bytes is None else max(self.peak_sampled_bytes, rss)
        return rss

    def stop(self) -> None:
        self._finished.set()
        if self._started:
            self._thread.join(timeout=2)

    def _watch(self) -> None:
        while not self._finished.wait(_WATCHDOG_POLL_SECONDS):
            baseline = self.baseline_bytes
            if baseline is None:
                continue
            rss = self.sample()
            if rss is None:
                return
            if rss > baseline + self.allowance_bytes:
                self.rss_at_kill_bytes = rss
                self.killed.set()
                self._process.kill()
                return


class _Reservation(NamedTuple):
    allowance: int | None
    held_by_others: int
    headroom: int | None
    headroom_source: str


_reservation_lock = threading.Lock()
_reserved_bytes = 0


@contextlib.contextmanager
def _reserved_memory(ceiling_bytes: int, result_reserve_bytes: int) -> Iterator[_Reservation]:
    """Reserve what a child may grow by and the decoded result the parent may hold, so children running at once in
    this process are sized against each other. The allowance is None when the pod has no room for the call."""
    global _reserved_bytes
    with _reservation_lock:
        held_by_others = _reserved_bytes
        sized = memory_allowance(ceiling_bytes, result_reserve_bytes + held_by_others, CGROUP_DIR, MEMINFO_PATH)
        if sized.allowance is not None:
            _reserved_bytes += sized.allowance + result_reserve_bytes
    try:
        yield _Reservation(sized.allowance, held_by_others, sized.headroom, sized.source)
    finally:
        if sized.allowance is not None:
            with _reservation_lock:
                _reserved_bytes -= sized.allowance + result_reserve_bytes


def run_in_capped_subprocess(
    func: Callable[[Unpack[_Ts]], _T],
    *args: Unpack[_Ts],
    memory_limit_bytes: int,
    max_result_bytes: int,
    timeout_seconds: float,
    result_memory_factor: int = DEFAULT_RESULT_MEMORY_FACTOR,
    log_fields: Mapping[str, object] | None = None,
) -> _T:
    """Call func(*args) in a child that may grow by up to memory_limit_bytes once func's module is imported, by less
    when the pod has less free memory, or not at all (no_headroom). func is pickled by reference, so it must be a
    module-level function taking plain data, and what it returns must be plain builtin data of at most
    max_result_bytes pickled, which takes result_memory_factor times that once decoded in this process."""
    target = f"{func.__module__}.{func.__qualname__}"
    if not MEMORY_CAP_ENFORCED:
        LOG.debug("Capped subprocess cannot set RLIMIT_AS on this platform", platform=sys.platform, target=target)
        return _run_child(
            func,
            args,
            allowance=None,
            reserve_bytes=0,
            memory_limit_bytes=memory_limit_bytes,
            max_result_bytes=max_result_bytes,
            timeout_seconds=timeout_seconds,
            target=target,
            log_fields=log_fields,
        )
    result_reserve_bytes = max_result_bytes * result_memory_factor
    with _reserved_memory(memory_limit_bytes, result_reserve_bytes) as reservation:
        fields = {
            **(log_fields or {}),
            "memory_headroom_bytes": reservation.headroom,
            "memory_headroom_source": reservation.headroom_source,
        }
        if reservation.allowance is None:
            _raise_failure(
                "no_headroom",
                _NO_HEADROOM_MESSAGE,
                target=target,
                memory_ceiling_bytes=memory_limit_bytes,
                reserved_by_other_calls_bytes=reservation.held_by_others,
                **fields,
            )
        return _run_child(
            func,
            args,
            allowance=reservation.allowance,
            reserve_bytes=result_reserve_bytes + reservation.held_by_others,
            memory_limit_bytes=memory_limit_bytes,
            max_result_bytes=max_result_bytes,
            timeout_seconds=timeout_seconds,
            target=target,
            log_fields=fields,
        )


def _run_child(
    func: Callable[[Unpack[_Ts]], _T],
    args: tuple[Unpack[_Ts]],
    *,
    allowance: int | None,
    reserve_bytes: int,
    memory_limit_bytes: int,
    max_result_bytes: int,
    timeout_seconds: float,
    target: str,
    log_fields: Mapping[str, object] | None,
) -> _T:
    request = pickle.dumps(
        {
            "allowance": allowance,
            "reserve_bytes": reserve_bytes,
            "cgroup_dir": os.fspath(CGROUP_DIR),
            "meminfo_path": os.fspath(MEMINFO_PATH),
            "cpu_seconds": math.ceil(timeout_seconds) + _CPU_LIMIT_MARGIN_SECONDS,
            "payload": pickle.dumps((func, args)),
        },
        protocol=pickle.HIGHEST_PROTOCOL,
    )
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", _CHILD_BOOTSTRAP, *sys.path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdin, stdout, stderr = (cast(IO[bytes], stream) for stream in (process.stdin, process.stdout, process.stderr))
    stderr_tail = bytearray()
    timed_out = threading.Event()
    drain: threading.Thread | None = None
    timer: threading.Timer | None = None
    watchdog = _MemoryWatchdog(process, allowance if allowance is not None else memory_limit_bytes)
    outcome: dict[str, Any] | None = None
    result_error: str | None = None
    result_problem: str | None = None
    result_too_large = False

    def _kill_on_timeout() -> None:
        timed_out.set()
        process.kill()

    try:
        draining = threading.Thread(target=_keep_tail, args=(stderr, stderr_tail), daemon=True)
        draining.start()
        drain = draining
        timer = threading.Timer(timeout_seconds, _kill_on_timeout)
        timer.daemon = True
        timer.start()
        watchdog.start()
        # A child that died before reading its request closes the pipe; reading its exit below reports that. Stdin
        # stays open for the ready acknowledgement and until the child's memory has been read.
        with contextlib.suppress(BrokenPipeError):
            stdin.write(struct.pack("<Q", len(request)) + request)
            stdin.flush()
        try:
            reader = _BoundedReader(cast(io.BufferedReader, stdout), max_result_bytes)
            loaded = _PlainDataUnpickler(reader).load()
            if isinstance(loaded, dict) and loaded.get("status") == "ready":
                watchdog.learn(_checked_ready(loaded))
                stdin.write(b"\x00")
                stdin.flush()
                # A new unpickler per message, because one unpickler numbers its memo across the messages it loads.
                loaded = _PlainDataUnpickler(reader).load()
            outcome = _checked_outcome(loaded)
            watchdog.sample()
        except _ResultTooLarge:
            result_too_large = True
        except _NotPlainData as exc:
            result_problem, result_error = "is not plain data", str(exc)[:200]
        except _UnexpectedShape as exc:
            result_problem, result_error = "has an unexpected shape", str(exc)[:200]
        except Exception as exc:
            result_error = type(exc).__name__
    finally:
        if timer is not None:
            timer.cancel()
        watchdog.stop()
        if outcome is None and process.poll() is None:
            process.kill()
        with contextlib.suppress(OSError):
            stdin.close()
        returncode = _wait_for_exit(process)
        if drain is not None:
            drain.join(timeout=5)
        stdout.close()
        stderr.close()

    duration_seconds = round(time.monotonic() - started, 2)
    applied_allowance = outcome.get("allowance_bytes", allowance) if outcome is not None else allowance
    memory_limit_exceeded = watchdog.growth_bytes is not None and watchdog.growth_bytes > watchdog.allowance_bytes
    memory_fields = {
        "rss_baseline_bytes": watchdog.baseline_bytes,
        "rss_peak_sampled_bytes": watchdog.peak_sampled_bytes,
        "rss_growth_sampled_bytes": watchdog.growth_bytes,
        "recheck_headroom_bytes": outcome.get("headroom_bytes") if outcome is not None else None,
        "recheck_headroom_source": outcome.get("headroom_source") if outcome is not None else None,
    }
    if outcome is not None and outcome["status"] == "ok" and not memory_limit_exceeded:
        LOG.info(
            "Capped subprocess finished",
            target=target,
            duration_seconds=duration_seconds,
            memory_budget_bytes=applied_allowance,
            **memory_fields,
            **(log_fields or {}),
        )
        return outcome["value"]

    kind: CappedSubprocessFailure
    status = outcome["status"] if outcome is not None else None
    if result_too_large or status == "result_too_large":
        kind = "result_too_large"
        message = f"returned more than the {_describe_bytes(max_result_bytes)} a result may hold"
    elif watchdog.killed.is_set() or memory_limit_exceeded:
        kind = "out_of_memory"
        message = (
            f"ran out of memory: it needed more than the {watchdog.allowance_bytes // (1024 * 1024)} MiB it may use"
        )
    elif outcome is not None and status == "error":
        kind = "error"
        message = outcome["message"]
    elif status == "out_of_memory":
        kind = "out_of_memory"
        limit_mib = (applied_allowance or memory_limit_bytes) // (1024 * 1024)
        message = f"ran out of memory: it needed more than the {limit_mib} MiB it may use"
    elif status == "no_headroom":
        kind = "no_headroom"
        message = _NO_HEADROOM_MESSAGE
    elif result_problem is not None:
        kind = "crashed"
        message = f"returned a result that {result_problem}"
    elif timed_out.is_set() or (sys.platform != "win32" and returncode == -signal.SIGXCPU):
        kind = "timeout"
        message = f"did not finish within {_describe_seconds(timeout_seconds)} and was stopped"
    else:
        kind = "crashed"
        message = f"its process exited without a result ({_describe_exit(returncode)})"
    _raise_failure(
        kind,
        message,
        target=target,
        error_type=outcome.get("error_type") if outcome is not None else None,
        result_error=result_error,
        returncode=returncode,
        duration_seconds=duration_seconds,
        memory_budget_bytes=applied_allowance,
        memory_ceiling_bytes=memory_limit_bytes,
        timeout_seconds=timeout_seconds,
        stderr_tail=_scrubbed_tail(stderr_tail),
        killed_by_memory_watchdog=watchdog.killed.is_set(),
        rss_at_kill_bytes=watchdog.rss_at_kill_bytes,
        **memory_fields,
        **(log_fields or {}),
    )


def _wait_for_exit(process: subprocess.Popen[bytes]) -> int:
    # A child that has handed over its outcome exits at once; one that does not is not allowed to hold the call.
    try:
        return process.wait(timeout=_EXIT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait()


def _raise_failure(kind: CappedSubprocessFailure, message: str, **log_fields: object) -> NoReturn:
    LOG.warning("Capped subprocess failed", failure=kind, **log_fields)
    raise CappedSubprocessError(message, kind=kind)


def _describe_bytes(count: int) -> str:
    return f"{count // (1024 * 1024)} MiB" if count >= 1024 * 1024 else f"{count // 1024} KiB"


def _keep_tail(stream: IO[bytes], tail: bytearray) -> None:
    while chunk := os.read(stream.fileno(), 65536):
        tail += chunk
        del tail[:-_STDERR_TAIL_BYTES]


def _scrubbed_tail(tail: bytearray) -> str | None:
    text = tail.decode("utf-8", errors="replace")
    if len(tail) >= _STDERR_TAIL_BYTES:
        # The cut can land inside a URL's query string, where the scrub below would no longer recognise it.
        text = text.partition("\n")[2]
    return _URL_QUERY.sub(r"\1", text) or None


def _describe_seconds(seconds: float) -> str:
    rounded = round(seconds)
    return "1 second" if rounded == 1 else f"{rounded} seconds"


def _describe_exit(returncode: int) -> str:
    if returncode >= 0:
        return f"exit status {returncode}"
    number = -returncode
    try:
        name = signal.Signals(number).name
    except ValueError:
        name = f"signal {number}"
    # PDFium aborts a failed allocation through FX_OutOfMemoryTerminate, which dies as SIGTRAP or SIGSEGV, so under
    # the cap a signal death is most likely the memory limit.
    likely_cause = ", likely out of memory" if MEMORY_CAP_ENFORCED else ""
    return f"killed by {name} (signal {number}{likely_cause})"
