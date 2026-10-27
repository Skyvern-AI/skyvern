from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeAlias, cast

from skyvern.config import settings
from skyvern.webeye.browser_object_predicates import is_page_like

if TYPE_CHECKING:
    from playwright.async_api import Frame, Page

    from skyvern.webeye.browser_engine import EngineFrame

    # None is a caller whose scope read itself failed, which classifies as "unknown".
    FrameTarget: TypeAlias = "Page | EngineFrame | None"

# What a timed-out operation hit. "unknown" is an operation whose frame the recording site could not name, and
# "not_a_frame_op" one that targets the page, so a log never confuses either with a missing classification.
FrameKind = Literal["main_frame", "child_frame", "unknown", "not_a_frame_op"]

# /sys/fs/cgroup is this container's own cgroup only in a private cgroup v2 namespace, where /proc/self/cgroup reads
# "0::/". On a host process it is the host's root cgroup, so the container fields are omitted there, as on cgroup v1
# and macOS, which lack the files.
CGROUP_MEMBERSHIP_PATH = Path("/proc/self/cgroup")
CGROUP_CPU_PRESSURE_PATH = Path("/sys/fs/cgroup/cpu.pressure")
CGROUP_CPU_STAT_PATH = Path("/sys/fs/cgroup/cpu.stat")
_PRIVATE_CGROUP_V2_MEMBERSHIP = "0::/"
_CPU_STAT_COUNTERS = frozenset({"usage_usec", "throttled_usec", "nr_throttled"})
# Microsecond counters by the field their delta is logged under; nr_throttled is a count of CFS periods instead.
_CONTAINER_CPU_USEC_FIELDS = {
    "wait_usec": "container_cpu_wait",
    "usage_usec": "container_cpu_usage",
    "throttled_usec": "container_cpu_throttled",
}
# A share over a shorter window mostly measures the few milliseconds of work between strike and trip, not a stall.
MIN_SHARE_WINDOW_SECONDS = 1.0


def _read_container_cpu_counters() -> dict[str, int]:
    """Cumulative cgroup v2 CPU counters: ``wait_usec`` (cpu.pressure ``some``: a task runnable but waiting for a CPU)
    and cpu.stat's ``usage_usec``, ``throttled_usec`` and ``nr_throttled``. Empty outside a private namespace."""
    try:
        if CGROUP_MEMBERSHIP_PATH.read_text().strip() != _PRIVATE_CGROUP_V2_MEMBERSHIP:
            return {}
    except (OSError, ValueError):
        return {}
    counters: dict[str, int] = {}
    with contextlib.suppress(OSError, ValueError):
        for line in CGROUP_CPU_PRESSURE_PATH.read_text().splitlines():
            if line.startswith("some "):
                for token in line.split():
                    if token.startswith("total="):
                        counters["wait_usec"] = int(token.removeprefix("total="))
    with contextlib.suppress(OSError, ValueError):
        for line in CGROUP_CPU_STAT_PATH.read_text().splitlines():
            name, _, value = line.partition(" ")
            if name in _CPU_STAT_COUNTERS:
                counters[name] = int(value)
    return counters


def frame_kind_of(frame: FrameTarget) -> FrameKind:
    """Which frame an operation targeted. Called inside timeout handlers, so it never raises: an object with no
    parent frame to read is ``unknown``."""
    try:
        if is_page_like(frame):
            return "main_frame"
        return "main_frame" if cast("Frame", frame).parent_frame is None else "child_frame"
    except Exception:
        return "unknown"


class BrowserOperation(StrEnum):
    """Browser-protocol operations whose deadline is generous enough that a timeout means the
    browser stopped answering, rather than that one selector or one animation was slow."""

    EVALUATE = "evaluate"
    SCREENSHOT = "screenshot"
    RELOAD = "reload"


_PAGE_LEVEL_OPERATIONS = frozenset({BrowserOperation.SCREENSHOT, BrowserOperation.RELOAD})


# Requiring more than one KIND of stuck operation is what separates a browser that has stopped
# answering from a single deadline being too tight for one heavy page.
MIN_DISTINCT_STUCK_OPERATIONS = 2


@dataclass
class BrowserHealth:
    """Browser-protocol timeouts for one run that no success has interrupted.

    Consecutive rather than cumulative: a browser that answers anything at all is still usable, and
    the retry ladders above are built to ride out an ordinary slow page.
    """

    consecutive_timeouts: int = 0
    stuck_operations: set[BrowserOperation] = field(default_factory=set)
    stalled_frames: set[FrameKind] = field(default_factory=set)
    # Wall, process CPU, and container cgroup clocks at the first strike of the current run of timeouts.
    stall_started_monotonic: float | None = None
    stall_started_process_time: float | None = None
    stall_started_container_cpu: dict[str, int] = field(default_factory=dict)

    def record_success(self) -> None:
        self.consecutive_timeouts = 0
        self.stuck_operations.clear()
        self.stalled_frames.clear()
        self.stall_started_monotonic = None
        self.stall_started_process_time = None
        self.stall_started_container_cpu = {}

    def record_timeout(self, operation: BrowserOperation, *, frame_kind: FrameKind | None = None) -> None:
        if self.consecutive_timeouts == 0:
            self.stalled_frames.clear()
            self.stall_started_monotonic = time.monotonic()
            self.stall_started_process_time = time.process_time()
            self.stall_started_container_cpu = _read_container_cpu_counters()
        self.consecutive_timeouts += 1
        self.stuck_operations.add(operation)
        if frame_kind is None:
            frame_kind = "not_a_frame_op" if operation in _PAGE_LEVEL_OPERATIONS else "unknown"
        self.stalled_frames.add(frame_kind)

    def record_recovery(self, operation: BrowserOperation) -> None:
        """A second route answered the operation that just timed out, so that strike was a tight
        deadline rather than a dead browser; strikes held by other operations stand. A failure that
        never earned a strike (a non-timeout error) has nothing to give back."""
        if operation not in self.stuck_operations:
            return
        self.consecutive_timeouts = max(0, self.consecutive_timeouts - 1)
        self.stuck_operations.discard(operation)

    @property
    def is_degraded(self) -> bool:
        strikes = settings.BROWSER_DEGRADED_TIMEOUT_STRIKES
        if strikes <= 0:
            return False
        return self.consecutive_timeouts >= strikes and len(self.stuck_operations) >= MIN_DISTINCT_STUCK_OPERATIONS

    def describe_stuck_operations(self) -> str:
        return ", ".join(sorted(self.stuck_operations))

    def stall_fields(self) -> dict[str, float | str | None]:
        """The stall window since the first strike, the stalled frame kinds, and CPU across the window that is process-
        and container-wide rather than per run: worker_process_cpu_* sums every thread of the worker process and
        container_* everything in its cgroup, so both include any other run the worker drives at the same time.
        Shares need a window of at least MIN_SHARE_WINDOW_SECONDS."""
        fields: dict[str, float | str | None] = {"stalled_frames": ", ".join(sorted(self.stalled_frames)) or None}
        if self.stall_started_monotonic is None or self.stall_started_process_time is None:
            return fields
        window_seconds = time.monotonic() - self.stall_started_monotonic
        worker_process_cpu_seconds = time.process_time() - self.stall_started_process_time
        fields["stall_window_seconds"] = round(window_seconds, 3)
        fields["worker_process_cpu_seconds"] = round(worker_process_cpu_seconds, 3)
        started, now = self.stall_started_container_cpu, _read_container_cpu_counters()
        cgroup_seconds = {
            name: (now[counter] - started[counter]) / 1_000_000
            for counter, name in _CONTAINER_CPU_USEC_FIELDS.items()
            if counter in started and counter in now
        }
        for name, seconds in cgroup_seconds.items():
            fields[f"{name}_seconds"] = round(seconds, 3)
        if "nr_throttled" in started and "nr_throttled" in now:
            fields["container_cpu_throttled_periods"] = now["nr_throttled"] - started["nr_throttled"]
        if window_seconds >= MIN_SHARE_WINDOW_SECONDS:
            fields["worker_process_cpu_share"] = round(worker_process_cpu_seconds / window_seconds, 3)
            for name, seconds in cgroup_seconds.items():
                fields[f"{name}_share"] = round(seconds / window_seconds, 3)
        return fields
