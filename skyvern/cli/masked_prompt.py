"""Masked CLI prompt helpers for secret input."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any, TextIO

from rich.console import Console
from rich.prompt import Prompt
from rich.text import TextType

_BACKSPACE_CHARS = {"\b", "\x7f"}
_CTRL_C = "\x03"
_CTRL_D = "\x04"
_CTRL_U = "\x15"
_ESCAPE = "\x1b"
_OSC_TERMINATOR = "\x07"


def _is_csi_final_byte(char: str) -> bool:
    return "\x40" <= char <= "\x7e"


def _consume_escape_sequence(read_char: Callable[[], str]) -> str | None:
    """Consume a terminal escape sequence and return a newline if one ended it."""
    char = read_char()
    if char == "":
        raise EOFError
    if char in {"\r", "\n"}:
        return char

    if char == "[":
        while True:
            char = read_char()
            if char == "":
                raise EOFError
            if char in {"\r", "\n"}:
                return char
            if _is_csi_final_byte(char):
                return None

    if char == "]":
        previous_char = ""
        while True:
            char = read_char()
            if char == "":
                raise EOFError
            if char in {"\r", "\n"}:
                return char
            if char == _OSC_TERMINATOR or (previous_char == _ESCAPE and char == "\\"):
                return None
            previous_char = char

    return None


def _read_masked_line(
    read_char: Callable[[], str],
    write: Callable[[str], None],
    *,
    mask: str = "*",
) -> str:
    """Read one line while echoing a mask character for each entered character."""
    value: list[str] = []

    while True:
        char = read_char()
        if char == _ESCAPE:
            escaped_char = _consume_escape_sequence(read_char)
            if escaped_char is None:
                continue
            char = escaped_char
        if char == "":
            raise EOFError
        if char in {"\r", "\n"}:
            write("\n")
            return "".join(value)
        if char == _CTRL_C:
            write("\n")
            raise KeyboardInterrupt
        if char == _CTRL_D:
            write("\n")
            raise EOFError
        if char in _BACKSPACE_CHARS:
            if value:
                value.pop()
                write("\b \b")
            continue
        if char == _CTRL_U:
            write("\b \b" * len(value))
            value.clear()
            continue
        if char < " ":
            continue

        value.append(char)
        write(mask)


def _read_masked_tty(output: TextIO, *, mask: str = "*") -> str:
    if os.name == "nt":
        return _read_masked_tty_windows(output, mask=mask)
    return _read_masked_tty_unix(output, mask=mask)


def _read_masked_tty_unix(output: TextIO, *, mask: str) -> str:
    import termios
    import tty

    input_file = sys.stdin
    fd = input_file.fileno()
    old_settings = termios.tcgetattr(fd)  # type: ignore[attr-defined]
    wrote_newline = False

    def write(text: str) -> None:
        nonlocal wrote_newline
        if text.endswith("\n"):
            wrote_newline = True
        output.write(text)
        output.flush()

    try:
        tty.setcbreak(fd)  # type: ignore[attr-defined]
        try:
            return _read_masked_line(lambda: input_file.read(1), write, mask=mask)
        except (EOFError, KeyboardInterrupt):
            if not wrote_newline:
                write("\n")
            raise
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)  # type: ignore[attr-defined]


def _read_masked_tty_windows(output: TextIO, *, mask: str) -> str:
    import msvcrt

    get_wide_char: Callable[[], str] = msvcrt.getwch  # type: ignore[attr-defined]

    def read_char() -> str:
        char = get_wide_char()
        if char in {"\x00", "\xe0"}:
            get_wide_char()
            return "\x00"
        return char

    def write(text: str) -> None:
        output.write(text)
        output.flush()

    return _read_masked_line(read_char, write, mask=mask)


def _read_secret_with_mask(
    console: Console,
    prompt: TextType,
    *,
    stream: TextIO | None = None,
    mask: str = "*",
) -> str:
    console.print(prompt, end="")
    if stream is not None:
        return stream.readline().rstrip("\r\n")
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\r\n")
    return _read_masked_tty(console.file, mask=mask)


class MaskedPrompt(Prompt):
    """Rich prompt that displays ``*`` feedback for secret input."""

    @classmethod
    def get_input(
        cls,
        console: Console,
        prompt: TextType,
        password: bool,
        stream: TextIO | None = None,
    ) -> str:
        if password:
            return _read_secret_with_mask(console, prompt, stream=stream)
        return super().get_input(console, prompt, password, stream=stream)


def ask_secret(prompt: TextType, **kwargs: Any) -> str:
    """Prompt for a secret and echo ``*`` characters as feedback."""
    return MaskedPrompt.ask(prompt, password=True, **kwargs)
