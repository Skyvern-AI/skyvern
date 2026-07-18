"""Regenerate the workload_page_load_*.jsonl fixtures from a real Chrome.

    CHROME=/path/to/chrome python tests/unit/proxy/fixtures/record_workload.py out.jsonl
    THROTTLE=1 CHROME=... python .../record_workload.py out_broadband.jsonl

Not a test (pytest collects test_*.py only) and never run in CI: it needs a local
Chrome. It exists so the recorded fixtures are reproducible evidence rather than a
data blob someone has to take on faith — the policy pack's whole claim is measured
against them, so how they were produced has to be checkable.

Drives Chrome over RAW websockets the way a client library does (flat auto-attach,
then Runtime/Network/Page/Log enable per page session) and records every upstream
frame verbatim. The page is served from a throwaway local HTTP server: no external
host, so the fixture carries no third-party address.

THROTTLE=1 emulates ordinary broadband. This matters more than it looks: over
loopback Chrome takes each response body in a handful of huge reads, so
Network.dataReceived fires ~14x less than it does over a real network. Both
recordings are kept — the throttled one is what a runner crossing a real network
looks like, the loopback one is the floor.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import websockets

SETTLE_SECONDS = 14.0


def build_site(root: Path) -> None:
    """A page shaped like a real one: stylesheet, script, images, an xhr, console."""
    (root / "app.js").write_text("console.log('boot');\n" + "// pad\n" * 8000)
    (root / "styles.css").write_text("body{margin:0}\n" + "/* pad */\n" * 6000)
    for i in range(4):
        # Incompressible, so the transfer size is real rather than gzipped away.
        (root / f"img{i}.bin").write_bytes(os.urandom(220_000))
    (root / "data.json").write_text(json.dumps({"rows": [{"i": i, "v": "x" * 200} for i in range(900)]}))
    (root / "index.html").write_text(
        """<!doctype html><html><head>
<link rel="stylesheet" href="styles.css">
</head><body>
<h1>local test page</h1>
<img src="img0.bin"><img src="img1.bin"><img src="img2.bin"><img src="img3.bin">
<script src="app.js"></script>
<script>
for (let i = 0; i < 5; i++) console.log('render pass', i);
fetch('data.json').then(r => r.json()).then(d => console.log('rows', d.rows.length));
</script>
</body></html>"""
    )


async def main() -> None:
    chrome = os.environ.get("CHROME")
    if not chrome:
        raise SystemExit("set CHROME to a chrome/chromium binary")
    out = Path(sys.argv[1])

    root = Path(tempfile.mkdtemp(prefix="site-"))
    build_site(root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    page_url = f"http://127.0.0.1:{httpd.server_address[1]}/index.html"

    profile = tempfile.mkdtemp(prefix="prof-")
    proc = subprocess.Popen(
        [chrome, "--remote-debugging-port=0", f"--user-data-dir={profile}", "--no-sandbox", "about:blank"],
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    port_file = Path(profile) / "DevToolsActivePort"
    for _ in range(100):
        if port_file.exists() and port_file.read_text().splitlines():
            break
        await asyncio.sleep(0.1)
    port = port_file.read_text().splitlines()[0]
    ws_url = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version").read())["webSocketDebuggerUrl"]

    frames: list[str] = []
    next_id = iter(range(1, 10_000))
    recording = True

    async with websockets.connect(ws_url, max_size=None) as ws:

        async def reader() -> None:
            while recording:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    continue
                frames.append(raw if isinstance(raw, str) else raw.decode())

        task = asyncio.create_task(reader())

        async def send(method: str, params: dict[str, Any] | None = None, session: str | None = None) -> None:
            msg: dict[str, Any] = {"id": next(next_id), "method": method}
            if params:
                msg["params"] = params
            if session:
                msg["sessionId"] = session
            await ws.send(json.dumps(msg))

        # Chrome was launched on about:blank, so flat auto-attach picks up the existing
        # page target (Chrome rejects GET on /json/new).
        await send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        await send("Target.setDiscoverTargets", {"discover": True})
        await asyncio.sleep(2.0)

        session_id = None
        for raw in frames:
            frame = json.loads(raw)
            if frame.get("method") == "Target.attachedToTarget":
                if frame["params"]["targetInfo"].get("type") == "page":
                    session_id = frame["params"]["sessionId"]
        if not session_id:
            raise SystemExit("no page session attached")

        for method in ("Runtime.enable", "Network.enable", "Page.enable", "Log.enable"):
            await send(method, None, session_id)
        if os.environ.get("THROTTLE"):
            await send(
                "Network.emulateNetworkConditions",
                {
                    "offline": False,
                    "latency": 40,
                    "downloadThroughput": 5_000_000 // 8,
                    "uploadThroughput": 1_000_000 // 8,
                },
                session_id,
            )
        await asyncio.sleep(0.3)
        await send("Page.navigate", {"url": page_url}, session_id)
        await asyncio.sleep(SETTLE_SECONDS)

        recording = False
        await task

    proc.terminate()
    httpd.shutdown()
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(profile, ignore_errors=True)
    out.write_text("\n".join(frames) + "\n")
    print(f"recorded {len(frames)} frames -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
