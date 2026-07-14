from __future__ import annotations

import asyncio
import socket
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

import structlog

from skyvern.config import settings
from skyvern.webeye.async_utils import await_to_terminal_state

LOG = structlog.get_logger()

VNC_STARTUP_TIMEOUT_SECONDS = 3.0
VNC_READINESS_POLL_INTERVAL_SECONDS = 0.05
VNC_CHILD_STABILITY_SECONDS = 0.1
VNC_PROCESS_STOP_TIMEOUT_SECONDS = 2.0
RFB_BASE_PORT = 5900


class VncStartupError(RuntimeError):
    """Raised when a per-session VNC process stack cannot become ready."""


class VncTeardownError(RuntimeError):
    """Raised when one or more VNC children remain alive after teardown."""

    def __init__(
        self,
        session_id: str,
        *,
        survivors: Sequence[str] = (),
        errors: Sequence[str] = (),
    ) -> None:
        details: list[str] = []
        if survivors:
            details.append(f"surviving children: {', '.join(survivors)}")
        if errors:
            details.append(f"errors: {'; '.join(errors)}")
        suffix = f" ({'; '.join(details)})" if details else ""
        super().__init__(f"Failed to tear down VNC stack for {session_id}{suffix}")
        self.session_id = session_id
        self.survivors = tuple(survivors)
        self.errors = tuple(errors)


@dataclass
class VncProcess:
    organization_id: str | None
    display_number: int
    vnc_port: int
    rfb_port: int
    xvfb_process: subprocess.Popen[bytes] | None = None
    x11vnc_process: subprocess.Popen[bytes] | None = None
    websockify_process: subprocess.Popen[bytes] | None = None
    state: Literal["starting", "ready", "stopping", "cleanup_failed"] = "starting"

    def children(self) -> list[subprocess.Popen[bytes]]:
        return [
            process
            for process in (self.xvfb_process, self.x11vnc_process, self.websockify_process)
            if process is not None
        ]


def _is_tcp_port_available(port: int) -> bool:
    """Return whether a TCP port is free across all local interfaces."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("0.0.0.0", port))
    except OSError:
        return False
    return True


def _port_is_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.05):
            return True
    except OSError:
        return False


def _display_is_ready(display_number: int) -> bool:
    return Path(f"/tmp/.X11-unix/X{display_number}").exists()


def _display_is_occupied(display_number: int) -> bool:
    return _display_is_ready(display_number) or Path(f"/tmp/.X{display_number}-lock").exists()


def _process_name(process: subprocess.Popen[bytes]) -> str:
    command = getattr(process, "args", None) or getattr(process, "command", None)
    if isinstance(command, (list, tuple)) and command:
        return str(command[0])
    return type(process).__name__


class VncManager:
    """Own one Xvfb -> x11vnc -> websockify stack per local browser session."""

    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _instances: ClassVar[dict[str, VncProcess]] = {}
    _used_displays: ClassVar[set[int]] = set()
    _used_ports: ClassVar[set[int]] = set()

    @classmethod
    def _allocate_display(cls) -> int:
        display_number = settings.SKYVERN_DEFAULT_DISPLAY + 1
        while display_number in cls._used_displays or _display_is_occupied(display_number):
            display_number += 1
        cls._used_displays.add(display_number)
        return display_number

    @classmethod
    def _allocate_port(cls, base_port: int) -> int:
        port = base_port
        while port <= 65535:
            if port not in cls._used_ports and _is_tcp_port_available(port):
                cls._used_ports.add(port)
                return port
            port += 1
        raise VncStartupError(f"No available TCP port at or above {base_port}")

    @classmethod
    def _release_resources(cls, display_number: int, vnc_port: int, rfb_port: int) -> None:
        cls._used_displays.discard(display_number)
        cls._used_ports.discard(vnc_port)
        cls._used_ports.discard(rfb_port)

    @classmethod
    async def _wait_for_process_ready(
        cls,
        process: subprocess.Popen[bytes],
        readiness_check: Callable[[], bool],
        process_name: str,
    ) -> None:
        del cls
        loop = asyncio.get_running_loop()
        deadline = loop.time() + VNC_STARTUP_TIMEOUT_SECONDS
        while True:
            returncode = process.poll()
            if returncode is not None:
                raise VncStartupError(f"{process_name} exited before readiness (return code {returncode})")
            if readiness_check():
                stability_deadline = loop.time() + VNC_CHILD_STABILITY_SECONDS
                while loop.time() < stability_deadline:
                    await asyncio.sleep(min(VNC_READINESS_POLL_INTERVAL_SECONDS, stability_deadline - loop.time()))
                    returncode = process.poll()
                    if returncode is not None:
                        raise VncStartupError(
                            f"{process_name} exited during post-readiness stability window (return code {returncode})"
                        )
                return
            if loop.time() >= deadline:
                raise VncStartupError(f"{process_name} readiness timed out")
            await asyncio.sleep(VNC_READINESS_POLL_INTERVAL_SECONDS)

    @classmethod
    async def start_vnc_for_session(
        cls,
        session_id: str,
        *,
        organization_id: str | None = None,
    ) -> tuple[int, int]:
        """Start or return the process-local VNC stack owned by ``session_id``."""
        async with cls._lock:
            existing = cls._instances.get(session_id)
            if existing is not None:
                if existing.organization_id != organization_id:
                    raise VncStartupError(f"VNC session {session_id} belongs to another organization")
                if cls._instance_is_healthy(existing):
                    return existing.display_number, existing.vnc_port
                LOG.warning(
                    "Cleaning unhealthy VNC stack before restart",
                    session_id=session_id,
                    organization_id=organization_id,
                    stack_state=existing.state,
                )
                await cls._cleanup_instance_to_completion(session_id, existing)

            display_number = cls._allocate_display()
            vnc_port: int | None = None
            rfb_port: int | None = None
            try:
                vnc_port = cls._allocate_port(settings.SKYVERN_BROWSER_VNC_PORT)
                rfb_port = cls._allocate_port(RFB_BASE_PORT)
            except BaseException:
                cls._used_displays.discard(display_number)
                if vnc_port is not None:
                    cls._used_ports.discard(vnc_port)
                raise

            instance = VncProcess(
                organization_id=organization_id,
                display_number=display_number,
                vnc_port=vnc_port,
                rfb_port=rfb_port,
            )
            # Ownership exists before the first child starts, so a partial stack
            # can never outlive its reservations without remaining retryable.
            cls._instances[session_id] = instance
            try:
                LOG.info(
                    "Starting VNC for session",
                    session_id=session_id,
                    organization_id=organization_id,
                    display_number=display_number,
                    vnc_port=vnc_port,
                    rfb_port=rfb_port,
                )
                instance.xvfb_process = cls._start_process(
                    [
                        "Xvfb",
                        f":{display_number}",
                        "-screen",
                        "0",
                        f"{settings.BROWSER_WIDTH}x{settings.BROWSER_HEIGHT}x24",
                        "-nolisten",
                        "tcp",
                    ]
                )
                await cls._wait_for_process_ready(
                    instance.xvfb_process,
                    lambda: _display_is_ready(display_number),
                    "Xvfb",
                )

                instance.x11vnc_process = cls._start_process(
                    [
                        "x11vnc",
                        "-display",
                        f":{display_number}",
                        "-forever",
                        "-shared",
                        "-rfbport",
                        str(rfb_port),
                        "-xkb",
                        "-nopw",
                        "-noxdamage",
                        "-noshm",
                        "-noxfixes",
                        "-noxrecord",
                        "-listen",
                        "127.0.0.1",
                    ]
                )
                await cls._wait_for_process_ready(
                    instance.x11vnc_process,
                    lambda: _port_is_ready(rfb_port),
                    "x11vnc",
                )

                instance.websockify_process = cls._start_process(
                    [
                        "websockify",
                        f"127.0.0.1:{vnc_port}",
                        f"127.0.0.1:{rfb_port}",
                    ]
                )
                await cls._wait_for_process_ready(
                    instance.websockify_process,
                    lambda: _port_is_ready(vnc_port),
                    "websockify",
                )
                unhealthy = cls._unhealthy_child_names(instance)
                if unhealthy:
                    raise VncStartupError(
                        f"VNC stack failed final health check for {session_id}: {', '.join(unhealthy)}"
                    )
                instance.state = "ready"
                LOG.info(
                    "VNC started for session",
                    session_id=session_id,
                    organization_id=organization_id,
                    display_number=display_number,
                    vnc_port=vnc_port,
                )
                return display_number, vnc_port
            except BaseException as startup_error:
                try:
                    await cls._cleanup_instance_to_completion(session_id, instance)
                except BaseException as cleanup_error:
                    LOG.warning(
                        "VNC startup cleanup did not fully succeed",
                        session_id=session_id,
                        organization_id=organization_id,
                        exc_info=True,
                    )
                    startup_error.add_note(f"VNC startup cleanup error: {cleanup_error!r}")
                raise

    @staticmethod
    def _start_process(command: Sequence[str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @classmethod
    async def _terminate_processes(
        cls,
        session_id: str,
        processes: Sequence[subprocess.Popen[bytes]],
    ) -> None:
        errors: list[str] = []
        for process in reversed(processes):
            try:
                await cls._terminate_process(session_id, process)
            except Exception as error:
                errors.append(f"{_process_name(process)}: {error!r}")
        survivors = [_process_name(process) for process in processes if process.poll() is None]
        if errors or survivors:
            raise VncTeardownError(session_id, survivors=survivors, errors=errors)

    @classmethod
    async def _terminate_processes_to_completion(
        cls,
        session_id: str,
        processes: Sequence[subprocess.Popen[bytes]],
    ) -> None:
        await await_to_terminal_state(cls._terminate_processes(session_id, processes))

    @classmethod
    async def _cleanup_instance_to_completion(cls, session_id: str, instance: VncProcess) -> None:
        instance.state = "stopping"
        cleanup_error: BaseException | None = None
        try:
            await cls._terminate_processes_to_completion(session_id, instance.children())
        except BaseException as error:
            cleanup_error = error

        survivors = cls._surviving_child_names(instance)
        if not survivors:
            cls._release_resources(instance.display_number, instance.vnc_port, instance.rfb_port)
            if cls._instances.get(session_id) is instance:
                cls._instances.pop(session_id, None)
        else:
            instance.state = "cleanup_failed"

        if cleanup_error is not None:
            raise cleanup_error
        if survivors:
            raise VncTeardownError(session_id, survivors=survivors)

    @staticmethod
    def _instance_is_healthy(instance: VncProcess) -> bool:
        return instance.state == "ready" and not VncManager._unhealthy_child_names(instance)

    @staticmethod
    def _unhealthy_child_names(instance: VncProcess) -> list[str]:
        children = {
            "Xvfb": instance.xvfb_process,
            "x11vnc": instance.x11vnc_process,
            "websockify": instance.websockify_process,
        }
        return [name for name, process in children.items() if process is None or process.poll() is not None]

    @staticmethod
    def _surviving_child_names(instance: VncProcess) -> list[str]:
        return [_process_name(process) for process in instance.children() if process.poll() is None]

    @staticmethod
    async def _terminate_process(session_id: str, process: subprocess.Popen[bytes]) -> None:
        del session_id
        if process.poll() is not None:
            return
        errors: list[str] = []
        try:
            process.terminate()
        except Exception as error:
            errors.append(f"terminate: {error!r}")
        if process.poll() is None:
            try:
                await asyncio.to_thread(process.wait, VNC_PROCESS_STOP_TIMEOUT_SECONDS)
            except Exception as error:
                errors.append(f"terminate wait: {error!r}")
        if process.poll() is None:
            try:
                process.kill()
            except Exception as error:
                errors.append(f"kill: {error!r}")
            if process.poll() is None:
                try:
                    await asyncio.to_thread(process.wait, VNC_PROCESS_STOP_TIMEOUT_SECONDS)
                except Exception as error:
                    errors.append(f"kill wait: {error!r}")
        if process.poll() is None:
            raise RuntimeError(
                f"{_process_name(process)} is still alive" + (f"; {'; '.join(errors)}" if errors else "")
            )

    @classmethod
    async def stop_vnc_for_session(
        cls,
        session_id: str,
        *,
        organization_id: str | None = None,
    ) -> None:
        """Stop a session's VNC children and release only after verified death."""
        async with cls._lock:
            instance = cls._instances.get(session_id)
            if instance is None:
                return
            if instance.organization_id != organization_id:
                LOG.warning(
                    "Rejecting VNC stop for organization mismatch",
                    session_id=session_id,
                    organization_id=organization_id,
                    owner_organization_id=instance.organization_id,
                )
                raise VncTeardownError(
                    session_id,
                    survivors=cls._surviving_child_names(instance),
                    errors=("organization does not own this VNC stack",),
                )
            await cls._cleanup_instance_to_completion(session_id, instance)
            LOG.info(
                "VNC stopped for session",
                session_id=session_id,
                organization_id=instance.organization_id,
            )

    @classmethod
    def has_session(cls, session_id: str) -> bool:
        return session_id in cls._instances

    @classmethod
    def owns_ready_stack(
        cls,
        session_id: str,
        *,
        organization_id: str | None,
        display_number: int,
        vnc_port: int,
    ) -> bool:
        """Return whether this process owns the exact healthy VNC stack described.

        Persisted display and port assignments are routing metadata, not proof that
        the process which created them is still alive.  Callers must use this exact
        match before routing an addressless session to localhost.
        """

        instance = cls._instances.get(session_id)
        return bool(
            instance is not None
            and instance.organization_id == organization_id
            and instance.display_number == display_number
            and instance.vnc_port == vnc_port
            and cls._instance_is_healthy(instance)
        )

    @classmethod
    async def stop_all(cls) -> None:
        """Attempt every stack, retaining and reporting only verified survivors."""
        async with cls._lock:
            pending_cancellation: asyncio.CancelledError | None = None
            failures: list[str] = []
            pending_control_flow: BaseException | None = None
            for session_id, instance in list(cls._instances.items()):
                try:
                    await cls._cleanup_instance_to_completion(session_id, instance)
                except asyncio.CancelledError as cancellation:
                    if pending_cancellation is None:
                        pending_cancellation = cancellation
                except Exception as error:
                    failures.append(f"{session_id}: {error}")
                except BaseException as error:
                    if pending_control_flow is None:
                        pending_control_flow = error

            aggregate = VncTeardownError("stop_all", errors=failures) if failures else None
            if pending_cancellation is not None:
                if aggregate is not None:
                    raise pending_cancellation from aggregate
                raise pending_cancellation
            if pending_control_flow is not None:
                if aggregate is not None:
                    raise pending_control_flow from aggregate
                raise pending_control_flow
            if aggregate is not None:
                raise aggregate
