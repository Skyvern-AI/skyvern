from __future__ import annotations

import argparse
import asyncio

from skyvern.browser_extension.broker_server import run_broker_daemon
from skyvern.browser_extension.errors import BrowserExtensionBrokerError


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, type=_port)
    args = parser.parse_args()
    try:
        asyncio.run(run_broker_daemon(args.port))
    except BrowserExtensionBrokerError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
