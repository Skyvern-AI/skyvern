from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from typing import ClassVar

import structlog

from skyvern.config import settings

LOG = structlog.get_logger()


@dataclass
class VncProcess:
    display_number: int
    vnc_port: int
    xvfb_process: subprocess.Popen
    x11vnc_process: subprocess.Popen | None
    websockify_process: subprocess.Popen | None


class VncManager:
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _instances: ClassVar[dict[str, VncProcess]] = {}
    _used_displays: ClassVar[set[int]] = set()
    _used_ports: ClassVar[set[int]] = set()

    # Start at 100 because :99 is the default display started in entrypoint
    BASE_DISPLAY = 100
    BASE_VNC_PORT = 6080

    @classmethod
    def allocate_display(cls) -> int:
        """Allocate a free display number."""
        display = cls.BASE_DISPLAY
        while display in cls._used_displays:
            display += 1
        cls._used_displays.add(display)
        return display

    @classmethod
    def allocate_port(cls) -> int:
        """Allocate a free VNC port."""
        port = cls.BASE_VNC_PORT
        while port in cls._used_ports:
            port += 1
        cls._used_ports.add(port)
        return port

    @classmethod
    async def start_vnc_for_session(cls, session_id: str) -> tuple[int, int]:
        """Start Xvfb, x11vnc, and websockify for a browser session.

        Returns:
            tuple[int, int]: (display_number, vnc_port)
        """
        async with cls._lock:
            display = cls.allocate_display()
            vnc_port = cls.allocate_port()
        rfb_port = 5900 + (display - cls.BASE_DISPLAY)

        LOG.info(
            "Starting VNC for session",
            session_id=session_id,
            display=display,
            vnc_port=vnc_port,
            rfb_port=rfb_port,
        )

        # Start Xvfb
        xvfb = subprocess.Popen(
            [
                "Xvfb",
                f":{display}",
                "-screen",
                "0",
                f"{settings.BROWSER_WIDTH}x{settings.BROWSER_HEIGHT}x24",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await asyncio.sleep(0.5)
        if xvfb.poll() is not None:
            LOG.error("Xvfb failed to start", display=display, returncode=xvfb.returncode)
            raise RuntimeError(f"Xvfb failed to start on display :{display}")

        # Set DISPLAY environment variable for the browser to use
        # Use the first available display (lowest number) as the default
        if not os.environ.get("DISPLAY") or len(cls._instances) == 0:
            os.environ["DISPLAY"] = f":{display}"
            LOG.info("Set DISPLAY environment variable", display=display)

        # Start x11vnc
        # -noxdamage: Disable X damage extension, force polling
        # -noshm: Disable shared memory (can cause issues with some apps)
        # -noxfixes: Disable XFIXES extension
        # -noxrecord: Disable RECORD extension
        # Note: We don't use -bg flag because Popen already runs in background.
        # Using -bg would fork and create a zombie parent process.
        x11vnc = subprocess.Popen(
            [
                "x11vnc",
                "-display",
                f":{display}",
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
                "0.0.0.0",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await asyncio.sleep(0.3)
        if x11vnc.poll() is not None:
            LOG.error("x11vnc failed to start", display=display, returncode=x11vnc.returncode)
            xvfb.terminate()
            raise RuntimeError(f"x11vnc failed to start on display :{display}")

        # Start websockify
        # Note: We don't use --daemon flag because Popen already runs in background.
        # Using --daemon would fork and create a zombie parent process.
        websockify = subprocess.Popen(
            [
                "websockify",
                "--web=/usr/share/novnc",
                str(vnc_port),
                f"localhost:{rfb_port}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await asyncio.sleep(0.2)
        if websockify.poll() is not None:
            LOG.error("websockify failed to start", vnc_port=vnc_port, returncode=websockify.returncode)
            x11vnc.terminate()
            xvfb.terminate()
            raise RuntimeError(f"websockify failed to start on port {vnc_port}")

        cls._instances[session_id] = VncProcess(
            display_number=display,
            vnc_port=vnc_port,
            xvfb_process=xvfb,
            x11vnc_process=x11vnc,
            websockify_process=websockify,
        )

        LOG.info(
            "VNC started for session",
            session_id=session_id,
            display=display,
            vnc_port=vnc_port,
        )

        return display, vnc_port

    @classmethod
    async def stop_vnc_for_session(cls, session_id: str) -> None:
        """Stop VNC processes for a browser session."""
        if session_id not in cls._instances:
            LOG.debug("No VNC instance found for session", session_id=session_id)
            return

        vnc = cls._instances[session_id]

        LOG.info(
            "Stopping VNC for session",
            session_id=session_id,
            display=vnc.display_number,
            vnc_port=vnc.vnc_port,
        )

        # Terminate processes in reverse order of startup (websockify → x11vnc → Xvfb)
        # This ensures clean teardown: disconnect clients first, then VNC server, then X server
        for proc in [vnc.websockify_process, vnc.x11vnc_process, vnc.xvfb_process]:
            if proc is None:
                continue
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                LOG.exception("Error terminating VNC process", session_id=session_id)

        # Also kill any orphaned processes by display/port
        await cls._kill_orphaned_processes(vnc.display_number, vnc.vnc_port)

        # Release resources
        cls._used_displays.discard(vnc.display_number)
        cls._used_ports.discard(vnc.vnc_port)
        del cls._instances[session_id]

        LOG.info("VNC stopped for session", session_id=session_id)

    @classmethod
    async def _kill_orphaned_processes(cls, display_number: int, vnc_port: int) -> None:
        """Kill any orphaned VNC processes by display/port."""
        try:
            # Kill x11vnc processes using this display
            subprocess.run(
                ["pkill", "-f", f"x11vnc.*:{display_number}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Kill websockify processes using this port
            subprocess.run(
                ["pkill", "-f", f"websockify.* {vnc_port} "],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Kill Xvfb processes using this display
            subprocess.run(
                ["pkill", "-f", f"Xvfb :{display_number}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            LOG.exception("Error killing orphaned VNC processes")

    @classmethod
    def get_display_for_session(cls, session_id: str) -> int | None:
        """Get the display number for a session."""
        if session_id in cls._instances:
            return cls._instances[session_id].display_number
        return None

    @classmethod
    async def stop_all(cls) -> None:
        """Stop all VNC instances."""
        LOG.info("Stopping all VNC instances", count=len(cls._instances))
        session_ids = list(cls._instances.keys())
        for session_id in session_ids:
            await cls.stop_vnc_for_session(session_id)
