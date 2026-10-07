import sys
from typing import IO

from rich.console import Console


def _relax_stream_encoding_errors(*streams: object) -> None:
    """Make the given streams degrade gracefully instead of crashing on unencodable output.

    Rich writes CLI output (including emoji like the checkmarks/rockets used across the
    CLI) straight to a text stream. Terminals that default to a legacy codepage --
    notably Windows' cp1252/cp437 consoles, which can't represent most characters
    outside Latin-1 -- raise UnicodeEncodeError on ``console.print()`` and crash the
    CLI outright.

    Reconfiguring the stream's error handler to "replace" keeps the encoding itself
    unchanged (so UTF-8-capable terminals are unaffected) and only swaps unencodable
    characters for "?" instead of raising.
    """
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors="replace")
        except Exception:  # noqa: BLE001,S110 - best-effort safety net; must never block CLI startup.
            # Some streams (e.g. certain test/capture harnesses) don't support
            # reconfiguring even though the attribute exists.
            pass


def make_console(file: IO[str] | None = None) -> Console:
    """Build a rich Console that won't crash on terminals with limited encodings.

    When ``file`` is omitted, Console resolves ``sys.stdout``/``sys.stderr``
    dynamically on each write, so those streams are relaxed here up front. When an
    explicit ``file`` is passed (e.g. in tests), that stream is relaxed instead.
    """
    _relax_stream_encoding_errors(*(sys.stdout, sys.stderr) if file is None else (file,))
    return Console(file=file)


# Global console instance for CLI modules
console = make_console()
