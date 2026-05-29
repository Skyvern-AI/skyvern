import { describe, expect, test } from "vitest";
import { buildKeyDownPayload, buildKeyUpPayload } from "./cdpInputUtils";

function makeKeyboardEvent(
  key: string,
  code: string,
  modifiers: Partial<KeyboardEvent> = {},
): KeyboardEvent {
  return new KeyboardEvent("keydown", {
    key,
    code,
    altKey: Boolean(modifiers.altKey),
    ctrlKey: Boolean(modifiers.ctrlKey),
    metaKey: Boolean(modifiers.metaKey),
    shiftKey: Boolean(modifiers.shiftKey),
  });
}

describe("CDP keyboard payloads", () => {
  test("sends non-printable keys as rawKeyDown with a Windows virtual key code", () => {
    expect(buildKeyDownPayload(makeKeyboardEvent("Enter", "Enter"))).toEqual({
      type: "keyEvent",
      eventType: "rawKeyDown",
      key: "Enter",
      code: "Enter",
      windowsVirtualKeyCode: 13,
      modifiers: 0,
    });

    expect(
      buildKeyDownPayload(makeKeyboardEvent("ArrowLeft", "ArrowLeft")),
    ).toMatchObject({
      eventType: "rawKeyDown",
      windowsVirtualKeyCode: 37,
    });
  });

  test("includes the Windows virtual key code on keyUp for non-printable keys", () => {
    expect(
      buildKeyUpPayload(makeKeyboardEvent("Backspace", "Backspace")),
    ).toEqual({
      type: "keyEvent",
      eventType: "keyUp",
      key: "Backspace",
      code: "Backspace",
      windowsVirtualKeyCode: 8,
      modifiers: 0,
    });
  });

  test("keeps printable characters as keyDown with text", () => {
    expect(buildKeyDownPayload(makeKeyboardEvent("a", "KeyA"))).toEqual({
      type: "keyEvent",
      eventType: "keyDown",
      key: "a",
      code: "KeyA",
      text: "a",
      windowsVirtualKeyCode: 65,
      modifiers: 0,
    });
  });

  test("keeps unmapped multi-character keys as keyDown", () => {
    expect(buildKeyDownPayload(makeKeyboardEvent("Dead", "Quote"))).toEqual({
      type: "keyEvent",
      eventType: "keyDown",
      key: "Dead",
      code: "Quote",
      modifiers: 0,
    });
  });

  test("preserves modifier bitmask", () => {
    expect(
      buildKeyDownPayload(
        makeKeyboardEvent("Tab", "Tab", { ctrlKey: true, shiftKey: true }),
      ),
    ).toMatchObject({
      modifiers: 10,
      windowsVirtualKeyCode: 9,
    });
  });
});
