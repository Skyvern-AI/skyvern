ENTER_KEY_ALIASES = ("enter", "return")

_KEY_ALIASES = {
    "space": " ",
    "ctrl": "Control",
    "backspace": "Backspace",
    "pagedown": "PageDown",
    "pageup": "PageUp",
    "tab": "Tab",
    "shift": "Shift",
    "arrowleft": "ArrowLeft",
    "left": "ArrowLeft",
    "arrowright": "ArrowRight",
    "right": "ArrowRight",
    "arrowup": "ArrowUp",
    "up": "ArrowUp",
    "arrowdown": "ArrowDown",
    "down": "ArrowDown",
    "home": "Home",
    "end": "End",
    "delete": "Delete",
    "esc": "Escape",
    "alt": "Alt",
}


def normalize_key_name(key: str) -> str:
    """Map a loosely written key name (any case, common short forms) to the name Playwright accepts."""
    key_lower_case = key.lower()
    if key_lower_case in ENTER_KEY_ALIASES:
        return "Enter"
    if key_lower_case in _KEY_ALIASES:
        return _KEY_ALIASES[key_lower_case]
    if key_lower_case.startswith("f") and key_lower_case[1:].isdigit():
        return key_lower_case.upper()
    return key


def split_key_chord(chord: str) -> list[str]:
    # "+" joins a chord's keys and is also a key itself, so "Control++" is Control then "+".
    if chord == "+":
        return ["+"]
    if chord.endswith("++"):
        return chord[:-2].split("+") + ["+"]
    return chord.split("+")


def normalize_key_chord(chord: str) -> str:
    return "+".join(normalize_key_name(part) for part in split_key_chord(chord))
