"""Trusted input: the reason this engine exists in a form a page cannot distinguish.

Everything here goes through the CDP ``Input`` domain, so the browser itself synthesises the events
and they arrive at page handlers with ``isTrusted === true``. Nothing in this module assigns to
``element.value`` or dispatches a constructed ``Event``. That distinction is not cosmetic: a React
controlled input re-renders from state and silently reverts any write the framework did not observe,
which is precisely why the previous raw-CDP driver was held out of production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from skyvern.webeye.skycdp.connection import CdpSession


@dataclass(frozen=True)
class KeyDefinition:
    key: str
    code: str
    key_code: int
    text: str = ""
    shift_text: str = ""
    location: int = 0


# Keys whose identity Chrome cannot infer from the character alone. Printable characters fall through
# to a generated definition, so this table stays small and only names the special cases.
_NAMED_KEYS: dict[str, KeyDefinition] = {
    "Enter": KeyDefinition("Enter", "Enter", 13, "\r"),
    "Tab": KeyDefinition("Tab", "Tab", 9, "\t"),
    "Backspace": KeyDefinition("Backspace", "Backspace", 8),
    "Delete": KeyDefinition("Delete", "Delete", 46),
    "Escape": KeyDefinition("Escape", "Escape", 27),
    "ArrowLeft": KeyDefinition("ArrowLeft", "ArrowLeft", 37),
    "ArrowUp": KeyDefinition("ArrowUp", "ArrowUp", 38),
    "ArrowRight": KeyDefinition("ArrowRight", "ArrowRight", 39),
    "ArrowDown": KeyDefinition("ArrowDown", "ArrowDown", 40),
    "Home": KeyDefinition("Home", "Home", 36),
    "End": KeyDefinition("End", "End", 35),
    "PageUp": KeyDefinition("PageUp", "PageUp", 33),
    "PageDown": KeyDefinition("PageDown", "PageDown", 34),
    "Space": KeyDefinition(" ", "Space", 32, " "),
    " ": KeyDefinition(" ", "Space", 32, " "),
    "Shift": KeyDefinition("Shift", "ShiftLeft", 16),
    "Control": KeyDefinition("Control", "ControlLeft", 17),
    "Alt": KeyDefinition("Alt", "AltLeft", 18),
    "Meta": KeyDefinition("Meta", "MetaLeft", 91),
}

_MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}

# Chrome does not infer editing intent from modifier flags: a key event carrying Meta+A moves no
# selection on its own. The editing command has to be named explicitly, which is what a real
# keystroke does through the platform's key-binding layer.
_EDITING_COMMANDS = {
    "a": "selectAll",
    "c": "copy",
    "v": "paste",
    "x": "cut",
    "z": "undo",
    "y": "redo",
}


def editing_commands(modifiers: list[str], key: str) -> list[str]:
    """The editing command a modifier+key shortcut stands for, if any."""
    if not any(modifier in ("Control", "Meta") for modifier in modifiers):
        return []
    command = _EDITING_COMMANDS.get(key.lower())
    if command == "undo" and "Shift" in modifiers:
        command = "redo"
    return [command] if command else []


# US-layout punctuation: the physical key each character sits on, as (code, virtual key code).
#
# This table exists because a character's ASCII ordinal is NOT its virtual key code. Measured against
# real Chrome, `.` is the one that corrupts: `ord('.')` is 46, which is VK_DELETE, so typing a period
# sent a Delete keypress and ate the character to its right -- silently, with the run continuing. That
# alone covers every email address, domain and decimal.
#
# `-` (45/VK_INSERT), `,` (44/VK_SNAPSHOT) and `/` (47/VK_HELP) collide too but were measured NOT to
# corrupt: PrintScreen and Help do nothing in a renderer, and Insert does not toggle overwrite mode
# here. They are fixed anyway because a latent collision is one Chrome release away from mattering,
# and because the key and code fields are wrong regardless -- a site reading event.keyCode sees the
# wrong key even when the text lands.
_PUNCTUATION_KEYS: dict[str, tuple[str, int]] = {
    ";": ("Semicolon", 186),
    ":": ("Semicolon", 186),
    "=": ("Equal", 187),
    "+": ("Equal", 187),
    ",": ("Comma", 188),
    "<": ("Comma", 188),
    "-": ("Minus", 189),
    "_": ("Minus", 189),
    ".": ("Period", 190),
    ">": ("Period", 190),
    "/": ("Slash", 191),
    "?": ("Slash", 191),
    "`": ("Backquote", 192),
    "~": ("Backquote", 192),
    "[": ("BracketLeft", 219),
    "{": ("BracketLeft", 219),
    "\\": ("Backslash", 220),
    "|": ("Backslash", 220),
    "]": ("BracketRight", 221),
    "}": ("BracketRight", 221),
    "'": ("Quote", 222),
    '"': ("Quote", 222),
    " ": ("Space", 32),
}

# Shifted digits report the digit's own physical key and virtual key code.
_SHIFTED_DIGITS: dict[str, str] = {
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
}


def key_definition(key: str) -> KeyDefinition:
    named = _NAMED_KEYS.get(key)
    if named is not None:
        return named
    if len(key) == 1:
        if key.isalpha() and key.isascii():
            return KeyDefinition(key=key, code=f"Key{key.upper()}", key_code=ord(key.upper()), text=key)
        if key.isdigit() and key.isascii():
            return KeyDefinition(key=key, code=f"Digit{key}", key_code=ord(key), text=key)
        shifted = _SHIFTED_DIGITS.get(key)
        if shifted is not None:
            return KeyDefinition(key=key, code=f"Digit{shifted}", key_code=ord(shifted), text=key)
        punctuation = _PUNCTUATION_KEYS.get(key)
        if punctuation is not None:
            code, key_code = punctuation
            return KeyDefinition(key=key, code=code, key_code=key_code, text=key)
        # Anything else (accented letters, CJK, emoji) has no US-layout key. Reporting 0 rather than
        # the ordinal keeps it from colliding with a real virtual key; `text` still carries it.
        return KeyDefinition(key=key, code="", key_code=0, text=key)
    return KeyDefinition(key=key, code=key, key_code=0)


def parse_shortcut(shortcut: str) -> tuple[list[str], str]:
    """Split ``"Control+Shift+a"`` into its modifiers and the key they apply to."""
    parts = shortcut.split("+")
    if len(parts) == 1:
        return [], parts[0]
    # A trailing empty part means the shortcut ends in a literal "+".
    key = parts[-1] if parts[-1] else "+"
    modifiers = [part for part in parts[:-1] if part]
    return modifiers, key


def modifier_mask(modifiers: list[str]) -> int:
    mask = 0
    for modifier in modifiers:
        mask |= _MODIFIER_BITS.get(modifier, 0)
    return mask


class Keyboard:
    def __init__(self, session: CdpSession) -> None:
        self._session = session
        self._pressed: list[str] = []

    @property
    def _mask(self) -> int:
        return modifier_mask(self._pressed)

    async def down(self, key: str, commands: list[str] | None = None) -> None:
        definition = key_definition(key)
        if key in _MODIFIER_BITS and key not in self._pressed:
            self._pressed.append(key)
        text = definition.text
        # A character typed while Control or Meta is held is a shortcut, not text.
        if self._mask & (_MODIFIER_BITS["Control"] | _MODIFIER_BITS["Meta"]):
            text = ""
        await self._session.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown" if text else "rawKeyDown",
                "modifiers": self._mask,
                "key": definition.key,
                "code": definition.code,
                "windowsVirtualKeyCode": definition.key_code,
                "nativeVirtualKeyCode": definition.key_code,
                "text": text,
                "unmodifiedText": text,
                "location": definition.location,
                **({"commands": commands} if commands else {}),
            },
        )

    async def up(self, key: str) -> None:
        definition = key_definition(key)
        if key in self._pressed:
            self._pressed.remove(key)
        await self._session.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "modifiers": self._mask,
                "key": definition.key,
                "code": definition.code,
                "windowsVirtualKeyCode": definition.key_code,
                "nativeVirtualKeyCode": definition.key_code,
                "location": definition.location,
            },
        )

    async def press(self, shortcut: str, delay: float | None = None) -> None:
        modifiers, key = parse_shortcut(shortcut)
        for modifier in modifiers:
            await self.down(modifier)
        try:
            await self.down(key, commands=editing_commands(modifiers, key))
            if delay:
                import asyncio

                await asyncio.sleep(delay / 1000)
            await self.up(key)
        finally:
            for modifier in reversed(modifiers):
                await self.up(modifier)

    async def type(self, text: str, delay: float | None = None) -> None:
        import asyncio

        for character in text:
            await self.press(character)
            if delay:
                await asyncio.sleep(delay / 1000)

    async def insert_text(self, text: str) -> None:
        """Commit text in one trusted ``input`` event, the way Playwright's ``fill`` does."""
        await self._session.send("Input.insertText", {"text": text})


class Mouse:
    def __init__(self, session: CdpSession) -> None:
        self._session = session
        self._x = 0.0
        self._y = 0.0
        self._buttons: set[str] = set()

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        start_x, start_y = self._x, self._y
        for step in range(1, max(steps, 1) + 1):
            fraction = step / max(steps, 1)
            self._x = start_x + (x - start_x) * fraction
            self._y = start_y + (y - start_y) * fraction
            await self._dispatch("mouseMoved", button="none")

    async def down(self, button: str = "left", click_count: int = 1) -> None:
        self._buttons.add(button)
        await self._dispatch("mousePressed", button=button, click_count=click_count)

    async def up(self, button: str = "left", click_count: int = 1) -> None:
        self._buttons.discard(button)
        await self._dispatch("mouseReleased", button=button, click_count=click_count)

    async def click(
        self, x: float, y: float, button: str = "left", click_count: int = 1, *, delay: float | None = None
    ) -> None:
        await self.move(x, y)
        await self.down(button=button, click_count=click_count)
        await self.up(button=button, click_count=click_count)

    async def dblclick(self, x: float, y: float, button: str = "left") -> None:
        """Two clicks with an increasing clickCount, which is what makes the page see a dblclick."""
        await self.move(x, y)
        for count in (1, 2):
            await self.down(button=button, click_count=count)
            await self.up(button=button, click_count=count)

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        await self._session.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": self._x,
                "y": self._y,
                "deltaX": delta_x,
                "deltaY": delta_y,
                "modifiers": 0,
            },
        )

    async def _dispatch(self, event_type: str, *, button: str, click_count: int = 0) -> None:
        params: dict[str, Any] = {
            "type": event_type,
            "x": self._x,
            "y": self._y,
            "button": button,
            "modifiers": 0,
            "clickCount": click_count,
            "buttons": sum({"left": 1, "right": 2, "middle": 4}.get(name, 0) for name in self._buttons),
        }
        await self._session.send("Input.dispatchMouseEvent", params)
