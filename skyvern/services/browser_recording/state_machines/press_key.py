import typing as t

import structlog

from skyvern.services.browser_recording.redact import is_character_key
from skyvern.services.browser_recording.types import (
    Action,
    ActionKind,
    ActionPressKey,
    ActionTarget,
    EventModifiers,
    ExfiltratedEvent,
    Mouse,
)

from .state_machine import StateMachine

LOG = structlog.get_logger()

# Named keys that change page state on their own. Text keys are already covered by
# input_text, and Tab only moves focus, which Playwright locators do not need.
STANDALONE_KEYS = frozenset({"Enter", "Escape"})

# A keydown for the modifier itself carries no gesture.
MODIFIER_KEYS = frozenset({"Alt", "AltGraph", "Control", "Meta", "OS", "Shift"})


def playwright_key(key: str | None, modifiers: EventModifiers) -> str | None:
    """The Playwright key expression for a keydown, or None if it is not a recordable gesture."""
    key = (key or "").strip()
    if not key or key in MODIFIER_KEYS:
        return None

    # Alt composes characters -- macOS Option, and Windows/Linux AltGr, which reports as
    # ctrl+alt. KeyboardEvent.key is then the composed output rather than the base key, so an
    # Alt-held single character is typed text, not a shortcut. Layouts that need AltGr to reach
    # "@" would otherwise lose every email field to a bogus Control+Alt+@ press.
    if modifiers.alt and is_character_key(key):
        return None

    commanded = modifiers.ctrl or modifiers.alt or modifiers.meta
    if not commanded and key not in STANDALONE_KEYS:
        return None

    # Shift never makes a key a gesture by itself: a shifted character already arrives
    # shifted in KeyboardEvent.key, and Shift+Tab / Shift+ArrowLeft are the same focus and
    # selection noise as their bare forms. It does qualify a gesture already being recorded,
    # where it changes what the key means -- Shift+Enter is a newline, not a submit.
    shifted = modifiers.shift and not is_character_key(key)

    held = [
        name
        for name, down in (
            ("Control", modifiers.ctrl),
            ("Alt", modifiers.alt),
            ("Shift", shifted),
            ("Meta", modifiers.meta),
        )
        if down
    ]

    return "+".join([*held, key])


class StateMachinePressKey(StateMachine):
    state: t.Literal["void"] = "void"

    def tick(self, event: ExfiltratedEvent, current_actions: list[Action]) -> ActionPressKey | None:
        if event.source != "console":
            return None

        if event.params.type != "keydown":
            return None

        key = playwright_key(event.params.key, event.params.modifiers)

        if not key:
            return None

        target = event.params.target

        LOG.debug(f"~ emitting press key action '{key}'")

        return ActionPressKey(
            kind=ActionKind.PRESS_KEY.value,
            target=ActionTarget(
                class_name=target.className,
                id=target.id,
                mouse=Mouse(xp=None, yp=None),
                sky_id=target.skyId,
                tag_name=target.tagName,
                texts=target.text,
                selector=target.selector,
                role=target.role,
                accessible_name=target.accessibleName,
                input_type=target.inputType,
                autocomplete=target.autocomplete,
            ),
            timestamp_start=event.params.timestamp,
            timestamp_end=event.params.timestamp,
            url=event.params.url,
            key=key,
        )

    def on_action(self, action: Action, current_actions: list[Action]) -> bool:
        # Stateless: nothing to reset, and nothing another machine's emission invalidates.
        return True
