import { describe, expect, it, vi } from "vitest";

import {
  handleVncClipboardPasteShortcut,
  isClipboardPasteShortcut,
  sendVncPasteShortcut,
  type PasteShortcutEvent,
  type VncClipboardRfb,
} from "./browserStreamClipboard";

function pasteEvent(overrides: Partial<PasteShortcutEvent> = {}) {
  return {
    altKey: false,
    ctrlKey: true,
    key: "v",
    metaKey: false,
    preventDefault: vi.fn(),
    shiftKey: false,
    stopImmediatePropagation: vi.fn(),
    stopPropagation: vi.fn(),
    ...overrides,
  } satisfies PasteShortcutEvent;
}

function rfbMock() {
  return {
    clipboardPasteFrom: vi.fn(),
    sendKey: vi.fn(),
  } satisfies VncClipboardRfb;
}

describe("browserStreamClipboard", () => {
  it("detects Ctrl+V and Cmd+V paste shortcuts", () => {
    expect(isClipboardPasteShortcut(pasteEvent({ ctrlKey: true }))).toBe(true);
    expect(
      isClipboardPasteShortcut(pasteEvent({ ctrlKey: false, metaKey: true })),
    ).toBe(true);
  });

  it("ignores non-paste shortcuts", () => {
    expect(isClipboardPasteShortcut(pasteEvent({ ctrlKey: false }))).toBe(
      false,
    );
    expect(isClipboardPasteShortcut(pasteEvent({ altKey: true }))).toBe(false);
    expect(isClipboardPasteShortcut(pasteEvent({ shiftKey: true }))).toBe(true);
    expect(isClipboardPasteShortcut(pasteEvent({ key: "c" }))).toBe(false);
  });

  it("sends a remote Ctrl+V sequence", () => {
    const rfb = rfbMock();

    sendVncPasteShortcut(rfb);

    expect(rfb.sendKey).toHaveBeenNthCalledWith(1, 0xffe3, "ControlLeft", true);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(2, 0x0076, "KeyV", true);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(3, 0x0076, "KeyV", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(
      4,
      0xffe3,
      "ControlLeft",
      false,
    );
  });

  it("syncs local clipboard text to VNC before sending paste", async () => {
    const event = pasteEvent();
    const rfb = rfbMock();

    await handleVncClipboardPasteShortcut(
      event,
      rfb,
      async () => "https://example.test",
      0,
    );

    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(event.stopPropagation).toHaveBeenCalledOnce();
    expect(event.stopImmediatePropagation).toHaveBeenCalledOnce();
    expect(rfb.clipboardPasteFrom).toHaveBeenCalledWith("https://example.test");
    expect(rfb.sendKey).toHaveBeenCalledTimes(4);
  });

  it("does not send stale VNC clipboard contents when browser clipboard read fails", async () => {
    const event = pasteEvent();
    const rfb = rfbMock();

    await expect(
      handleVncClipboardPasteShortcut(
        event,
        rfb,
        async () => {
          throw new Error("denied");
        },
        0,
      ),
    ).resolves.toBe(true);

    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(rfb.clipboardPasteFrom).not.toHaveBeenCalled();
    expect(rfb.sendKey).not.toHaveBeenCalled();
  });

  it("does not intercept non-paste keys", async () => {
    const event = pasteEvent({ key: "x" });
    const rfb = rfbMock();

    await expect(
      handleVncClipboardPasteShortcut(event, rfb, async () => "ignored"),
    ).resolves.toBe(false);

    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(rfb.clipboardPasteFrom).not.toHaveBeenCalled();
    expect(rfb.sendKey).not.toHaveBeenCalled();
  });
});
