"""Virtual key codes for printable characters.

A character's ASCII ordinal is not its Windows virtual key code, and the two collide on exactly the
characters forms are made of. `ord('.')` is 46, which is VK_DELETE: typing a period sent Chrome a
Delete keypress, the character never appeared, and nothing raised. An offline replay caught it as an
email address arriving as `ada.lovelace@examplecom` and a form rejecting it as malformed.

The failure mode is what makes this worth pinning: no exception, no log line, just a wrong value in a
field.

`-` (45/VK_INSERT), `,` (44/VK_SNAPSHOT) and `/` (47/VK_HELP) collide the same way but were measured
against real Chrome NOT to corrupt -- PrintScreen and Help do nothing in a renderer, and Insert does
not toggle overwrite mode. They are still pinned here: a latent collision is one Chrome release away
from mattering, and the reported key is wrong regardless, which a site reading `event.keyCode` sees.
"""

from __future__ import annotations

import string

import pytest

from skyvern.webeye.skycdp.facade.input import key_definition

# Windows virtual key codes that do something other than insert a character. A printable character
# reporting one of these is asking Chrome to perform an edit, not type.
CONTROL_VIRTUAL_KEYS = {
    8: "Backspace",
    9: "Tab",
    13: "Enter",
    16: "Shift",
    17: "Control",
    18: "Alt",
    19: "Pause",
    20: "CapsLock",
    27: "Escape",
    33: "PageUp",
    34: "PageDown",
    35: "End",
    36: "Home",
    37: "ArrowLeft",
    38: "ArrowUp",
    39: "ArrowRight",
    40: "ArrowDown",
    44: "PrintScreen",
    45: "Insert",
    46: "Delete",
    47: "Help",
}

PRINTABLE = string.ascii_letters + string.digits + string.punctuation + " "


@pytest.mark.parametrize("character", list(PRINTABLE))
def test_no_printable_character_reports_a_control_virtual_key(character: str) -> None:
    definition = key_definition(character)
    collision = CONTROL_VIRTUAL_KEYS.get(definition.key_code)
    assert collision is None, (
        f"typing {character!r} reports virtual key {definition.key_code}, which is {collision}. "
        f"Chrome performs that edit instead of inserting the character, silently."
    )


@pytest.mark.parametrize("character", list(PRINTABLE))
def test_every_printable_character_carries_its_text(character: str) -> None:
    """Without `text`, `down()` sends rawKeyDown and Chrome inserts nothing."""
    assert key_definition(character).text == character


def test_the_characters_that_actually_broke_map_to_their_real_keys() -> None:
    """Regression pins for the four collisions found in a replay, by name rather than by ordinal."""
    for character, code, key_code in (
        (".", "Period", 190),
        ("-", "Minus", 189),
        (",", "Comma", 188),
        ("/", "Slash", 191),
    ):
        definition = key_definition(character)
        assert (definition.code, definition.key_code) == (code, key_code), f"{character!r} regressed"


def test_letters_and_digits_still_report_their_own_key() -> None:
    assert (key_definition("a").code, key_definition("a").key_code) == ("KeyA", 65)
    assert (key_definition("Z").code, key_definition("Z").key_code) == ("KeyZ", 90)
    assert (key_definition("7").code, key_definition("7").key_code) == ("Digit7", 55)


def test_a_shifted_digit_reports_the_digit_key_it_sits_on() -> None:
    assert (key_definition("@").code, key_definition("@").key_code) == ("Digit2", 50)
    assert (key_definition("!").code, key_definition("!").key_code) == ("Digit1", 49)


def test_a_character_with_no_us_layout_key_reports_zero_rather_than_its_ordinal() -> None:
    """An ordinal here would be a lottery ticket for a control-key collision."""
    for character in ("é", "中", "→"):
        assert key_definition(character).key_code == 0
        assert key_definition(character).text == character
