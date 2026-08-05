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

  it("releases and restores tracked left Cmd around remote Ctrl+V", async () => {
    const event = pasteEvent({ ctrlKey: false, metaKey: true });
    const rfb = rfbMock();
    const getHeldMetaSides = vi.fn(() => ({ left: true, right: false }));

    await handleVncClipboardPasteShortcut(event, rfb, {
      getHeldMetaSides,
      readClipboardText: async () => "https://example.test",
      syncDelayMs: 0,
    });

    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(event.stopPropagation).toHaveBeenCalledOnce();
    expect(event.stopImmediatePropagation).toHaveBeenCalledOnce();
    expect(rfb.clipboardPasteFrom).toHaveBeenCalledWith("https://example.test");
    expect(getHeldMetaSides).toHaveBeenCalledTimes(2);
    expect(rfb.sendKey).toHaveBeenCalledTimes(6);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(1, 0xffe9, "MetaLeft", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(2, 0xffe3, "ControlLeft", true);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(3, 0x0076, "KeyV", true);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(4, 0x0076, "KeyV", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(
      5,
      0xffe3,
      "ControlLeft",
      false,
    );
    expect(rfb.sendKey).toHaveBeenNthCalledWith(6, 0xffe9, "MetaLeft", true);
    expect(rfb.clipboardPasteFrom.mock.invocationCallOrder[0]).toBeLessThan(
      rfb.sendKey.mock.invocationCallOrder[0]!,
    );
  });

  it("releases and restores tracked right Cmd around remote Ctrl+V", async () => {
    const event = pasteEvent({ ctrlKey: false, metaKey: true });
    const rfb = rfbMock();
    const getHeldMetaSides = vi.fn(() => ({ left: false, right: true }));

    await handleVncClipboardPasteShortcut(event, rfb, {
      getHeldMetaSides,
      readClipboardText: async () => "https://example.test",
      syncDelayMs: 0,
    });

    expect(getHeldMetaSides).toHaveBeenCalledTimes(2);
    expect(rfb.sendKey).toHaveBeenCalledTimes(6);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(1, 0xffeb, "MetaRight", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(2, 0xffe3, "ControlLeft", true);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(3, 0x0076, "KeyV", true);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(4, 0x0076, "KeyV", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(
      5,
      0xffe3,
      "ControlLeft",
      false,
    );
    expect(rfb.sendKey).toHaveBeenNthCalledWith(6, 0xffeb, "MetaRight", true);
  });

  it("does not restore Cmd when it is released during clipboard sync", async () => {
    const event = pasteEvent({ ctrlKey: false, metaKey: true });
    const rfb = rfbMock();
    const getHeldMetaSides = vi.fn(() => ({ left: false, right: false }));

    await handleVncClipboardPasteShortcut(event, rfb, {
      getHeldMetaSides,
      readClipboardText: async () => "https://example.test",
      syncDelayMs: 0,
    });

    expect(getHeldMetaSides).toHaveBeenCalledOnce();
    expect(rfb.sendKey).toHaveBeenCalledTimes(8);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(1, 0xffe9, "AltLeft", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(2, 0xffea, "AltRight", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(3, 0xffeb, "MetaLeft", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(4, 0xffec, "MetaRight", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(5, 0xffe3, "ControlLeft", true);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(6, 0x0076, "KeyV", true);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(7, 0x0076, "KeyV", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(
      8,
      0xffe3,
      "ControlLeft",
      false,
    );
  });

  it("uses the freshly held Cmd side after a side swap during clipboard sync", async () => {
    const event = pasteEvent({ ctrlKey: false, metaKey: true });
    const rfb = rfbMock();
    const getHeldMetaSides = vi.fn(() => ({ left: false, right: true }));

    await handleVncClipboardPasteShortcut(event, rfb, {
      getHeldMetaSides,
      readClipboardText: async () => "https://example.test",
      syncDelayMs: 0,
    });

    expect(getHeldMetaSides).toHaveBeenCalledTimes(2);
    expect(rfb.sendKey).toHaveBeenCalledTimes(6);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(1, 0xffeb, "MetaRight", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(2, 0xffe3, "ControlLeft", true);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(3, 0x0076, "KeyV", true);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(4, 0x0076, "KeyV", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(
      5,
      0xffe3,
      "ControlLeft",
      false,
    );
    expect(rfb.sendKey).toHaveBeenNthCalledWith(6, 0xffeb, "MetaRight", true);
  });

  it("falls back to blanket Cmd release without restoring untracked sides", async () => {
    const event = pasteEvent({ ctrlKey: false, metaKey: true });
    const rfb = rfbMock();

    await handleVncClipboardPasteShortcut(event, rfb, {
      readClipboardText: async () => "https://example.test",
      syncDelayMs: 0,
    });

    expect(rfb.sendKey).toHaveBeenCalledTimes(8);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(1, 0xffe9, "AltLeft", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(2, 0xffea, "AltRight", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(3, 0xffeb, "MetaLeft", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(4, 0xffec, "MetaRight", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(5, 0xffe3, "ControlLeft", true);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(6, 0x0076, "KeyV", true);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(7, 0x0076, "KeyV", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(
      8,
      0xffe3,
      "ControlLeft",
      false,
    );
  });

  it("preserves the remote Ctrl+V sequence for Ctrl-only paste", async () => {
    const event = pasteEvent();
    const rfb = rfbMock();
    const getHeldMetaSides = vi.fn(() => ({ left: true, right: false }));

    await handleVncClipboardPasteShortcut(event, rfb, {
      getHeldMetaSides,
      readClipboardText: async () => "https://example.test",
      syncDelayMs: 0,
    });

    expect(getHeldMetaSides).not.toHaveBeenCalled();
    expect(rfb.sendKey).toHaveBeenCalledTimes(4);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(1, 0xffe3, "ControlLeft", true);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(2, 0x0076, "KeyV", true);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(3, 0x0076, "KeyV", false);
    expect(rfb.sendKey).toHaveBeenNthCalledWith(
      4,
      0xffe3,
      "ControlLeft",
      false,
    );
    expect(rfb.sendKey).not.toHaveBeenCalledWith(0xffe9, "AltLeft", false);
    expect(rfb.sendKey).not.toHaveBeenCalledWith(0xffea, "AltRight", false);
    expect(rfb.sendKey).not.toHaveBeenCalledWith(0xffeb, "MetaLeft", false);
    expect(rfb.sendKey).not.toHaveBeenCalledWith(0xffec, "MetaRight", false);
  });

  it("does not send stale VNC clipboard contents when browser clipboard read fails", async () => {
    const event = pasteEvent();
    const rfb = rfbMock();
    const error = new Error("denied");
    const onPasteError = vi.fn();

    await expect(
      handleVncClipboardPasteShortcut(event, rfb, {
        onPasteError,
        readClipboardText: async () => {
          throw error;
        },
        syncDelayMs: 0,
      }),
    ).resolves.toBe(true);

    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(onPasteError).toHaveBeenCalledWith(error);
    expect(onPasteError).toHaveBeenCalledTimes(1);
    expect(rfb.clipboardPasteFrom).not.toHaveBeenCalled();
    expect(rfb.sendKey).not.toHaveBeenCalled();
  });

  it("does not intercept non-paste keys", async () => {
    const event = pasteEvent({ key: "x" });
    const rfb = rfbMock();

    await expect(
      handleVncClipboardPasteShortcut(event, rfb, {
        readClipboardText: async () => "ignored",
      }),
    ).resolves.toBe(false);

    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(rfb.clipboardPasteFrom).not.toHaveBeenCalled();
    expect(rfb.sendKey).not.toHaveBeenCalled();
  });
});
