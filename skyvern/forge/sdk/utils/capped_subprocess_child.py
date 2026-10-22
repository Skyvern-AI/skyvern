"""The child side of capped_subprocess. It imports only the stdlib, because every capped call pays for its imports
again in a fresh interpreter. The free-memory arithmetic lives here so the parent and the child apply the same rule."""

import faulthandler
import os
import pickle
import struct
import sys
from typing import Any, NamedTuple

if sys.platform != "win32":
    import resource

CGROUP_DIR = "/sys/fs/cgroup"
MEMINFO_PATH = "/proc/meminfo"
# cgroup v2 with memory.oom.group kills the whole container at memory.max, so a child stays this far below it.
CGROUP_HEADROOM_MARGIN_BYTES = 256 * 1024 * 1024
# Less free memory than this fails the call outright instead of spending the pod's last memory on it.
MIN_MEMORY_BUDGET_BYTES = 128 * 1024 * 1024

# cgroup v1 reports "no limit" as a number just under 2^63.
_CGROUP_V1_NO_LIMIT_BYTES = 1 << 62
_MAX_EXCEPTION_CHAIN = 16
_MAX_MESSAGE_CHARS = 4096


class _NoHeadroom(Exception):
    pass


class ResultTooLarge(Exception):
    """Raised by the work to say that its result cannot fit the size the parent allows."""


def main() -> None:
    # The pickled outcome owns the real stdout; anything the work prints goes to stderr, whose tail the parent logs.
    result_stream = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    # -I ignores PYTHONFAULTHANDLER, and a native crash in PDFium otherwise leaves nothing in that tail.
    faulthandler.enable(file=sys.stderr)
    allowance: int | None = None
    recheck = MemoryAllowance(None, None, "none")
    outcome: dict[str, Any]
    try:
        (request_size,) = struct.unpack("<Q", sys.stdin.buffer.read(8))
        request = pickle.loads(sys.stdin.buffer.read(request_size))
        # Unpickling imports func's module, so a cap set after it leaves the imports out of the call's budget.
        func, args = pickle.loads(request["payload"])
        allowance = request["allowance"]
        if allowance is not None:
            # The parent sized the allowance before this interpreter and func's imports took their share of the pod.
            recheck = memory_allowance(
                allowance, request["reserve_bytes"], request["cgroup_dir"], request["meminfo_path"]
            )
            allowance = recheck.allowance
            if allowance is None:
                raise _NoHeadroom
        _apply_limits(allowance, request["cpu_seconds"])
        # The parent reads this process's resident memory when it gets this message, so growth counts from here.
        pickle.dump({"status": "ready", "allowance_bytes": allowance}, result_stream, protocol=pickle.HIGHEST_PROTOCOL)
        result_stream.flush()
        outcome = {"status": "ok", "value": func(*args)}
    except _NoHeadroom:
        outcome = {"status": "no_headroom"}
    except ResultTooLarge as exc:
        outcome = {"status": "result_too_large", "message": str(exc)[:_MAX_MESSAGE_CHARS]}
    except BaseException as exc:
        if caused_by_memory_error(exc):
            outcome = {"status": "out_of_memory"}
        else:
            outcome = {"status": "error", "error_type": type(exc).__name__, "message": str(exc)[:_MAX_MESSAGE_CHARS]}
    outcome["allowance_bytes"] = allowance
    outcome["headroom_bytes"] = recheck.headroom
    outcome["headroom_source"] = recheck.source
    exit_code = 0
    try:
        pickle.dump(outcome, result_stream, protocol=pickle.HIGHEST_PROTOCOL)
        result_stream.flush()
        # The parent reads this process's memory once it has the outcome and then closes stdin; exiting sooner would
        # leave it nothing to read.
        sys.stdin.buffer.read(1)
    except BaseException:
        exit_code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    # Interpreter teardown after a large parse can take seconds and serves no purpose here.
    os._exit(exit_code)


class MemoryAllowance(NamedTuple):
    allowance: int | None
    headroom: int | None
    source: str


def memory_allowance(
    ceiling_bytes: int,
    reserve_bytes: int,
    cgroup_dir: str | os.PathLike[str] = CGROUP_DIR,
    meminfo_path: str | os.PathLike[str] = MEMINFO_PATH,
) -> MemoryAllowance:
    """Size how far a child may grow: ceiling_bytes, lowered to the free memory less the margin and reserve_bytes.
    The allowance is None when that safe amount is under the floor, since crossing memory.max kills the worker."""
    headroom, source = cgroup_headroom_bytes(cgroup_dir), "cgroup"
    if headroom is None:
        headroom, source = cgroup_v1_headroom_bytes(cgroup_dir), "cgroup_v1"
    if headroom is None:
        # Without a container limit, such as a Fargate task whose limit is set above the container, the kernel's
        # estimate of what can be allocated without swapping is the best bound left.
        headroom, source = meminfo_available_bytes(meminfo_path), "meminfo"
    if headroom is None:
        return MemoryAllowance(ceiling_bytes, None, "none")
    safe_bytes = headroom - CGROUP_HEADROOM_MARGIN_BYTES - reserve_bytes
    return MemoryAllowance(
        min(ceiling_bytes, safe_bytes) if safe_bytes >= MIN_MEMORY_BUDGET_BYTES else None, headroom, source
    )


def cgroup_headroom_bytes(cgroup_dir: str | os.PathLike[str]) -> int | None:
    """Return memory.max less the memory the cgroup holds that cannot be reclaimed, or None when it sets no limit.
    Once there is a limit, anything unreadable counts against the caller, never for it."""
    try:
        limit = _read_file(os.path.join(cgroup_dir, "memory.max"))
        limit_bytes = None if limit == "max" else int(limit)
    except (OSError, ValueError):
        return None
    if limit_bytes is None:
        return None
    try:
        used = int(_read_file(os.path.join(cgroup_dir, "memory.current")))
    except (OSError, ValueError):
        return 0
    # Inactive page cache is reclaimed before the OOM killer runs, so it counts as free, as in kubelet's working set.
    inactive_file = _stat_value(_read_text_or_empty(os.path.join(cgroup_dir, "memory.stat")), "inactive_file")
    return limit_bytes - (used - (inactive_file or 0))


def cgroup_v1_headroom_bytes(cgroup_dir: str | os.PathLike[str]) -> int | None:
    """The same for cgroup v1, where the memory controller has its own directory and files."""
    v1_dir = os.path.join(cgroup_dir, "memory")
    stat = _read_text_or_empty(os.path.join(v1_dir, "memory.stat"))
    candidates = [_stat_value(stat, "hierarchical_memory_limit")]
    try:
        candidates.append(int(_read_file(os.path.join(v1_dir, "memory.limit_in_bytes"))))
    except (OSError, ValueError):
        pass
    limits = [limit for limit in candidates if limit is not None and limit < _CGROUP_V1_NO_LIMIT_BYTES]
    if not limits:
        return None
    try:
        used = int(_read_file(os.path.join(v1_dir, "memory.usage_in_bytes")))
    except (OSError, ValueError):
        return 0
    return min(limits) - (used - (_stat_value(stat, "total_inactive_file") or 0))


def meminfo_available_bytes(meminfo_path: str | os.PathLike[str]) -> int | None:
    try:
        for line in _read_file(meminfo_path).splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def caused_by_memory_error(exc: BaseException) -> bool:
    # pdfplumber re-raises every pdfminer failure as PdfminerException, which keeps the MemoryError only as context.
    current: BaseException | None = exc
    for _ in range(_MAX_EXCEPTION_CHAIN):
        if current is None:
            return False
        if isinstance(current, MemoryError):
            return True
        current = current.__cause__ or current.__context__
    return False


def _read_file(path: str | os.PathLike[str]) -> str:
    with open(path) as file:
        return file.read().strip()


def _read_text_or_empty(path: str | os.PathLike[str]) -> str:
    try:
        return _read_file(path)
    except OSError:
        return ""


def _stat_value(stat: str, name: str) -> int | None:
    for line in stat.splitlines():
        if line.startswith(f"{name} "):
            try:
                return int(line.split()[1])
            except (ValueError, IndexError):
                return None
    return None


def _address_space_bytes() -> int:
    with open("/proc/self/statm") as statm:
        return int(statm.read().split()[0]) * os.sysconf("SC_PAGE_SIZE")


def _apply_limits(memory_budget: int | None, cpu_seconds: int) -> None:
    if sys.platform == "win32":
        return
    _, core_hard = resource.getrlimit(resource.RLIMIT_CORE)
    resource.setrlimit(resource.RLIMIT_CORE, (0, core_hard))
    _, cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)
    if cpu_hard != resource.RLIM_INFINITY:
        cpu_seconds = min(cpu_seconds, cpu_hard)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_hard))
    if memory_budget is not None:
        _, as_hard = resource.getrlimit(resource.RLIMIT_AS)
        as_limit = _address_space_bytes() + memory_budget
        if as_hard != resource.RLIM_INFINITY:
            as_limit = min(as_limit, as_hard)
        # A hard limit equal to the soft one keeps a child that runs foreign code from raising its own cap.
        resource.setrlimit(resource.RLIMIT_AS, (as_limit, as_limit))
