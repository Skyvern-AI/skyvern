"""Find a secret in text that writes it with HTML character references, percent escapes, or backslash escapes. Each
character may be written as itself or with one escape, in any mix, under up to two more escape layers; a secret with a
backslash or an escape of its own is found under those layers only when it is written plainly."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from html.entities import html5

# Every escape form below, and a no-break space written for a space, starts with one of these characters, so text
# without them can hold none.
ESCAPE_MARKERS = ("&", "%", "\\", "+", "\u00a0")
# A decoded secret shorter than this is not searched for alone, like SECRET_SUBSTRING_MIN_LENGTH: it would match
# unrelated text.
_DECODED_MIN_LENGTH = 5
_DECODING_ROUNDS = 4

_NAMED_REFERENCES: dict[str, list[str]] = {}
for _name, _value in html5.items():
    if len(_value) == 1:
        _NAMED_REFERENCES.setdefault(_value, []).append(_name)

_CHARACTER_REFERENCE = re.compile(r"&(?:#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);")
_PERCENT_RUN = re.compile(r"(?:%[0-9A-Fa-f]{2})+")
_UNDECODED_BYTE = re.compile("[\udc80-\udcff]")
_BACKSLASH_ESCAPE = re.compile(r"\\(?:u\{([0-9A-Fa-f]{1,6})\}|u([0-9A-Fa-f]{4})|x([0-9A-Fa-f]{2})|([^0-9A-Za-z]))")
_SURROGATE = re.compile("[\ud800-\udfff]")


def _hex(number: int, width: int = 0) -> str:
    digits = format(number, f"0{width}x")
    return "".join(f"[{digit}{digit.upper()}]" if digit.isalpha() else digit for digit in digits)


def _character_forms(char: str) -> str:
    point = ord(char)
    forms = [re.escape(char)]
    forms += [re.escape("&" + name) for name in sorted(_NAMED_REFERENCES.get(char, ()), key=len, reverse=True)]
    forms.append(f"&#0*{point};?")
    forms.append(f"&#[xX]0*{_hex(point)};?")
    forms.append("".join(f"%{_hex(byte, 2)}" for byte in char.encode("utf-8")))
    if char == " ":
        forms.append(r"\+")
        forms.append(_character_forms("\u00a0"))
    if point <= 0xFFFF:
        forms.append(r"\\u" + _hex(point, 4))
    else:
        high, low = divmod(point - 0x10000, 0x400)
        forms.append(r"\\u" + _hex(0xD800 + high, 4) + r"\\u" + _hex(0xDC00 + low, 4))
    forms.append(r"\\u\{0*" + _hex(point) + r"\}")
    if point <= 0xFF:
        forms.append(r"\\x" + _hex(point, 2))
    if not char.isalnum():
        forms.append(r"\\" + re.escape(char))
    return "(?:" + "|".join(forms) + ")"


def _unescape_references(text: str) -> str:
    # Only a reference closed by its ';'. html.unescape also takes one without it, which breaks a reference whose ';'
    # an outer escape still hides.
    return _CHARACTER_REFERENCE.sub(lambda match: html.unescape(match.group(0)), text)


def _unescape_percents(text: str) -> str:
    def run(match: re.Match[str]) -> str:
        decoded = bytes.fromhex(match.group(0).replace("%", "")).decode("utf-8", "surrogateescape")
        # A byte that is not valid UTF-8 here keeps its escape: an outer escape can hide the rest of its character.
        return _UNDECODED_BYTE.sub(lambda byte: f"%{ord(byte.group(0)) - 0xDC00:02X}", decoded)

    return _PERCENT_RUN.sub(run, text)


def _unescape_backslashes(text: str) -> str:
    def one(match: re.Match[str]) -> str:
        if match.group(4) is not None:
            return match.group(4)
        point = int(match.group(1) or match.group(2) or match.group(3), 16)
        return chr(point) if point <= 0x10FFFF else match.group(0)

    decoded = _BACKSLASH_ESCAPE.sub(one, text)
    if _SURROGATE.search(decoded):
        # Two \u escapes can write one character as a UTF-16 surrogate pair.
        decoded = decoded.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return decoded


def decoding_stages(text: str) -> Iterator[str]:
    """``text`` after each step that undoes one kind of escape everywhere in it, round after round, until a round
    changes nothing."""
    for _ in range(_DECODING_ROUNDS):
        changed = False
        for decode in (_unescape_references, _unescape_percents, _unescape_backslashes):
            decoded = decode(text)
            if decoded != text:
                changed = True
                text = decoded
                yield text
        if not changed:
            return


def _fully_decoded(text: str) -> str:
    for stage in decoding_stages(text):
        text = stage
    return text


@dataclass(frozen=True)
class EscapedSecret:
    # Matches the secret with each character as it is or written with one escape (any mix).
    pattern: re.Pattern[str]
    # The secret with its own escape-like characters undone, when that differs, for the backstop: a decoding step
    # that undoes an outer escape also undoes such characters, in the text and here alike.
    decoded: str | None


def escaped_secret(secret: str) -> EscapedSecret:
    """``secret`` as text can write it: each character as it is, or as an HTML character reference (named, decimal or
    hex), a percent escape of its UTF-8 bytes ('+' or a no-break space for a space), or a backslash escape (\\u, a
    \\u surrogate pair, \\u{}, \\x, or before a symbol)."""
    decoded = _fully_decoded(secret)
    return EscapedSecret(
        pattern=re.compile("".join(_character_forms(char) for char in secret)),
        decoded=decoded if decoded != secret and len(decoded) >= _DECODED_MIN_LENGTH else None,
    )


def shows_escaped_secret(text: str, secrets: Iterable[EscapedSecret]) -> bool:
    """Whether a decoding stage of ``text`` still shows a secret: with at most one escape per character, or as the
    decoded secret."""
    if not any(marker in text for marker in ESCAPE_MARKERS):
        return False
    held = list(secrets)
    for stage in decoding_stages(text):
        for secret in held:
            if secret.pattern.search(stage) or (secret.decoded is not None and secret.decoded in stage):
                return True
    return False
