from __future__ import annotations

import io

from skyvern.cli.console import make_console


def test_make_console_survives_legacy_codepage_stream() -> None:
    """A terminal that can't represent emoji (e.g. Windows' cp1252) must not crash the CLI.

    Regression test for rich's Console.print() raising UnicodeEncodeError when the
    underlying stream uses a legacy codepage like cp1252/cp437: make_console() must
    relax the stream's error handling so unencodable characters degrade to "?"
    instead of raising.
    """
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")

    console = make_console(file=stream)  # type: ignore[call-arg]

    # Should not raise UnicodeEncodeError.
    console.print("Done! \U0001f389 ✅")

    stream.flush()
    stream.seek(0)
    written = stream.buffer.getvalue().decode("cp1252")
    assert "Done!" in written
    assert "?" in written
