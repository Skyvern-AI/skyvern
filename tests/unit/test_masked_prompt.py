from __future__ import annotations

from io import StringIO

from rich.console import Console

from skyvern.cli.masked_prompt import _read_masked_line, ask_secret


def test_read_masked_line_echoes_one_mask_per_pasted_character() -> None:
    secret = "sk-test-secret"
    chars = iter(f"{secret}\n")
    output: list[str] = []

    result = _read_masked_line(lambda: next(chars), output.append)

    rendered = "".join(output)
    assert result == secret
    assert rendered == "*" * len(secret) + "\n"
    assert secret not in rendered


def test_read_masked_line_backspace_removes_last_character() -> None:
    chars = iter("abc\x7fd\n")
    output: list[str] = []

    result = _read_masked_line(lambda: next(chars), output.append)

    assert result == "abd"
    assert "".join(output) == "***\b \b*\n"


def test_read_masked_line_ignores_bracketed_paste_markers() -> None:
    chars = iter("\x1b[200~sk-abc\x1b[201~\n")
    output: list[str] = []

    result = _read_masked_line(lambda: next(chars), output.append)

    assert result == "sk-abc"
    assert "".join(output) == "******\n"


def test_read_masked_line_ignores_arrow_key_sequences() -> None:
    chars = iter("ab\x1b[D\n")
    output: list[str] = []

    result = _read_masked_line(lambda: next(chars), output.append)

    assert result == "ab"
    assert "".join(output) == "**\n"


def test_ask_secret_reads_stream_without_echoing_secret() -> None:
    stream = StringIO("secret-value\n")
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)

    result = ask_secret("Enter API key", console=console, stream=stream)

    rendered = output.getvalue()
    assert result == "secret-value"
    assert "Enter API key:" in rendered
    assert "secret-value" not in rendered
